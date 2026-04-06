import re
import json
import time
import hmac
import hashlib
from datetime import datetime
from typing import Optional

import boto3
import requests
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
from fastapi import HTTPException, Response

from app.constants import AWS_REGION, SECRET_PREFIX, DDB_TABLE, SESSIONS_TABLE, SLACK_API_BASE
from app.logger import logger

# ── AWS CLIENTS ───────────────────────────────────────────────────────────────
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb       = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table      = dynamodb.Table(DDB_TABLE)      if DDB_TABLE      else None
sessions_table = dynamodb.Table(SESSIONS_TABLE) if SESSIONS_TABLE else None


# ── HTTP HELPERS ──────────────────────────────────────────────────────────────

def no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response


# ── SECRET MANAGEMENT ─────────────────────────────────────────────────────────

def secret_name(team_id: str) -> str:
    return f"{SECRET_PREFIX}/{team_id}"


def upsert_secret(name: str, payload: dict) -> None:
    body = json.dumps(payload)
    try:
        secrets_client.create_secret(Name=name, SecretString=body)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            secrets_client.put_secret_value(SecretId=name, SecretString=body)
        else:
            raise


def read_secret(name: str) -> Optional[dict]:
    try:
        resp = secrets_client.get_secret_value(SecretId=name)
        return json.loads(resp.get("SecretString", "{}"))
    except ClientError as e:
        return {"_error": e.response["Error"]["Code"], "_message": str(e)}


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return token[:2] + "..." + token[-2:]
    return token[:4] + "..." + token[-4:]


# ── SLACK SIGNATURE VERIFICATION ──────────────────────────────────────────────

def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    base   = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v0=" + digest, signature)


# ── DDB GUARD ─────────────────────────────────────────────────────────────────

def require_ddb():
    if ddb_table is None:
        raise HTTPException(500, "DDB_TABLE environment variable is not set")


# ── DATE / TIMESTAMP HELPERS ──────────────────────────────────────────────────

def _date_to_sk(date_str: str, end_of_day: bool = False) -> str:
    epoch = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    return str(epoch + 86399 if end_of_day else epoch)


def _ts_human(ts: str) -> str:
    try:
        return datetime.utcfromtimestamp(float(str(ts).split(".")[0])).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


# ── INPUT VALIDATORS ──────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(v: Optional[str]) -> Optional[str]:
    if v is not None and not _DATE_RE.match(v):
        raise ValueError("Date must be in YYYY-MM-DD format")
    return v


def _validate_team_id(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("team_id cannot be empty")
    if not re.match(r"^[A-Z0-9]{1,20}$", v):
        raise ValueError("team_id must be alphanumeric (Slack workspace ID)")
    return v


def _validate_channel_id(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("channel_id cannot be empty")
    if not re.match(r"^[A-Z0-9]{1,20}$", v):
        raise ValueError("channel_id must be a valid Slack channel ID")
    return v


# ── USER CACHE ────────────────────────────────────────────────────────────────
# pk = "{team_id}#__users__",  sk = user_id
# Stores display_name + real_name so we can look up user_id by username.

def _user_pk(team_id: str) -> str:
    return f"{team_id}#__users__"


def get_cached_user(team_id: str, user_id: str) -> Optional[dict]:
    if ddb_table is None:
        return None
    try:
        resp = ddb_table.get_item(Key={"pk": _user_pk(team_id), "sk": user_id})
        return resp.get("Item")
    except Exception:
        return None


def upsert_cached_user(team_id: str, user_id: str, display_name: str, real_name: str) -> None:
    if ddb_table is None:
        return
    try:
        ddb_table.put_item(Item={
            "pk":           _user_pk(team_id),
            "sk":           user_id,
            "user_id":      user_id,
            "display_name": display_name,
            "real_name":    real_name,
            "cached_at":    datetime.utcnow().isoformat() + "Z",
        })
    except Exception as e:
        logger.warning(f"[user-cache] upsert failed for {user_id}: {e}")


def resolve_user_id(team_id: str, username: str, bot_token: str) -> Optional[str]:
    """
    Given a display name / real name (e.g. 'vrisha'), return the matching
    Slack user_id. Checks the DynamoDB cache first; falls back to the
    Slack users.list API and populates the cache.
    Returns None if no match is found.
    """
    if not username or not bot_token:
        return None

    needle = username.strip().lower()

    # ── 1. Check cache ────────────────────────────────────────────────────────
    if ddb_table is not None:
        try:
            resp = ddb_table.query(KeyConditionExpression=Key("pk").eq(_user_pk(team_id)))
            for item in resp.get("Items", []):
                dn = (item.get("display_name") or "").lower()
                rn = (item.get("real_name")    or "").lower()
                if needle in dn or needle in rn or dn.startswith(needle) or rn.startswith(needle):
                    logger.info(f"[user-cache] resolved '{username}' → {item['user_id']} (cache hit)")
                    return item["user_id"]
        except Exception as e:
            logger.warning(f"[user-cache] cache query failed: {e}")

    # ── 2. Fetch from Slack and populate cache ────────────────────────────────
    logger.info(f"[user-cache] cache miss for '{username}', fetching users.list from Slack")
    cursor = None
    matched_id: Optional[str] = None

    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r    = requests.get(
                f"{SLACK_API_BASE}/users.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params=params,
                timeout=20,
            )
            data = r.json()
        except Exception as e:
            logger.warning(f"[user-cache] users.list request failed: {e}")
            break

        if not data.get("ok"):
            logger.warning(f"[user-cache] users.list error: {data.get('error')}")
            break

        for member in data.get("members", []):
            uid     = member.get("id", "")
            profile = member.get("profile") or {}
            dn      = (profile.get("display_name") or member.get("name") or "").strip()
            rn      = (profile.get("real_name")    or "").strip()
            if uid:
                upsert_cached_user(team_id, uid, dn, rn)
                if matched_id is None:
                    dn_l, rn_l = dn.lower(), rn.lower()
                    if needle in dn_l or needle in rn_l or dn_l.startswith(needle) or rn_l.startswith(needle):
                        matched_id = uid
                        logger.info(f"[user-cache] resolved '{username}' → {uid} (display='{dn}', real='{rn}')")

        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break

    return matched_id


def resolve_username_for_message(team_id: str, user_id: str, bot_token: str) -> str:
    """Return display_name for a user_id. Uses cache; falls back to users.info API."""
    if not user_id:
        return ""
    cached = get_cached_user(team_id, user_id)
    if cached:
        return cached.get("display_name") or cached.get("real_name") or user_id

    try:
        r    = requests.get(
            f"{SLACK_API_BASE}/users.info",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"user": user_id},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            profile = (data.get("user") or {}).get("profile") or {}
            dn = (profile.get("display_name") or data["user"].get("name") or "").strip()
            rn = (profile.get("real_name")    or "").strip()
            upsert_cached_user(team_id, user_id, dn, rn)
            return dn or rn or user_id
    except Exception:
        pass
    return user_id


# ── USERNAME EXTRACTION FROM QUESTION ─────────────────────────────────────────

_AT_MENTION = re.compile(r"@([A-Za-z][A-Za-z0-9._-]{1,30})")


def extract_username_from_question(question: str) -> Optional[str]:
    """
    Extract a username only if the user explicitly typed @name in the question.
    Returns the name without @, or None.
    """
    m = _AT_MENTION.search(question)
    if m:
        name = m.group(1).strip()
        logger.info(f"[name-extract] @mention extracted '{name}' from question: {question!r}")
        return name
    return None
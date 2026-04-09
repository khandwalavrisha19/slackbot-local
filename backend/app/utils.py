import re
import time
import hmac
import hashlib
from datetime import datetime
from typing import Optional

import requests
from fastapi import Response

from app.constants import SLACK_API_BASE
from app.logger import logger
from app.db import get_conn


# ── HTTP HELPERS ──────────────────────────────────────────────────────────────

def no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response


# ── SECRET MANAGEMENT (SQLite-backed) ─────────────────────────────────────────

def secret_name(team_id: str) -> str:
    """Returns the team_id itself as the lookup key."""
    return team_id


def upsert_secret(team_id: str, payload: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO workspace_tokens(team_id, team_name, bot_user_id, bot_token, scope, updated_at)
            VALUES(:team_id, :team_name, :bot_user_id, :bot_token, :scope, :updated_at)
            ON CONFLICT(team_id) DO UPDATE SET
                team_name   = excluded.team_name,
                bot_user_id = excluded.bot_user_id,
                bot_token   = excluded.bot_token,
                scope       = excluded.scope,
                updated_at  = excluded.updated_at
            """,
            {**payload, "updated_at": datetime.utcnow().isoformat() + "Z"},
        )


def read_secret(team_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM workspace_tokens WHERE team_id = ?", (team_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_secret(team_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM workspace_tokens WHERE team_id = ?", (team_id,))


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

def _user_pk(team_id: str) -> str:
    return f"{team_id}#__users__"


def get_cached_user(team_id: str, user_id: str) -> Optional[dict]:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_cache WHERE pk = ? AND sk = ?",
                (_user_pk(team_id), user_id),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def upsert_cached_user(team_id: str, user_id: str, display_name: str, real_name: str) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_cache(pk, sk, user_id, display_name, real_name, cached_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(pk, sk) DO UPDATE SET
                    display_name = excluded.display_name,
                    real_name    = excluded.real_name,
                    cached_at    = excluded.cached_at
                """,
                (
                    _user_pk(team_id), user_id, user_id,
                    display_name, real_name,
                    datetime.utcnow().isoformat() + "Z",
                ),
            )
    except Exception as e:
        logger.warning(f"[user-cache] upsert failed for {user_id}: {e}")


def resolve_user_id(team_id: str, username: str, bot_token: str) -> Optional[str]:
    """
    Given a display name / real name (e.g. 'vrisha'), return the matching
    Slack user_id. Checks the SQLite cache first; falls back to the
    Slack users.list API and populates the cache.
    Returns None if no match is found.
    """
    if not username or not bot_token:
        return None

    needle = username.strip().lower()

    # ── 1. Check cache ────────────────────────────────────────────────────────
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_cache WHERE pk = ?", (_user_pk(team_id),)
            ).fetchall()
        for row in rows:
            dn = (row["display_name"] or "").lower()
            rn = (row["real_name"]    or "").lower()
            if needle in dn or needle in rn or dn.startswith(needle) or rn.startswith(needle):
                logger.info(f"[user-cache] resolved '{username}' → {row['user_id']} (cache hit)")
                return row["user_id"]
    except Exception as e:
        logger.warning(f"[user-cache] cache query failed: {e}")

    # ── 2. Fetch from Slack and populate cache ────────────────────────────────
    logger.info(f"[user-cache] cache miss for '{username}', fetching users.list from Slack")
    cursor: Optional[str] = None
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
                        logger.info(
                            f"[user-cache] resolved '{username}' → {uid} "
                            f"(display='{dn}', real='{rn}')"
                        )

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
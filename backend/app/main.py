import os
import re
import json
import uuid
import time
import hmac
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import boto3
import requests
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
from fastapi import FastAPI, Request, Query, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from mangum import Mangum
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1").strip()
SECRET_PREFIX = os.getenv("SECRET_PREFIX", "slackbot").strip()
CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI", "").strip()
SLACK_SCOPES = os.getenv(
    "SLACK_SCOPES",
    "channels:history,chat:write,users:read,groups:history,channels:read,groups:read,channels:join",
).strip()
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
DDB_TABLE = os.getenv("DDB_TABLE", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
UI_BASE_URL = os.getenv("UI_BASE_URL", "").rstrip("/")

origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()] or ["*"]

app = FastAPI(title="Slackbot Full MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

secrets = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DDB_TABLE) if DDB_TABLE else None

frontend_default = Path(__file__).with_name("index.html")
FRONTEND_PATH = Path(os.getenv("FRONTEND_PATH", str(frontend_default)))


def no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def secret_name(team_id: str) -> str:
    return f"{SECRET_PREFIX}/{team_id}"


def upsert_secret(name: str, payload: dict) -> None:
    body = json.dumps(payload)
    try:
        secrets.create_secret(Name=name, SecretString=body)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            secrets.put_secret_value(SecretId=name, SecretString=body)
        else:
            raise


def read_secret(name: str) -> Optional[dict]:
    try:
        resp = secrets.get_secret_value(SecretId=name)
        return json.loads(resp.get("SecretString", "{}"))
    except ClientError as e:
        return {"_error": e.response["Error"]["Code"], "_message": str(e)}


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return token[:2] + "..." + token[-2:]
    return token[:4] + "..." + token[-4:]


def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v0=" + digest, signature)


def require_ddb():
    if ddb_table is None:
        raise HTTPException(500, "DDB_TABLE environment variable is not set")


def _date_to_sk(date_str: str, end_of_day: bool = False) -> str:
    epoch = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    return str(epoch + 86399 if end_of_day else epoch)


def _ts_human(ts: str) -> str:
    try:
        return datetime.utcfromtimestamp(float(str(ts).split(".")[0])).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def _score_messages(items: list[dict], q: str) -> list[dict]:
    keywords = re.findall(r"\w+", q.lower())
    if not keywords:
        return items
    # Filter to ONLY messages that actually contain at least one keyword
    scored = []
    for item in items:
        text = (item.get("text") or "").lower()
        # Skip system messages like "has joined the channel"
        if re.search(r"<@\w+> has (joined|left)", text):
            continue
        score = sum(text.count(kw) for kw in keywords)
        score += sum(2 for kw in keywords if kw in text[:80])
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored]


def _format_messages(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        text = (item.get("text") or "").strip()
        out.append({
            "message_ts": item.get("ts") or item.get("sk", ""),
            "user_id": item.get("user_id", "unknown"),
            "username": item.get("username", ""),
            "text": text,
            "snippet": text[:400] + ("…" if len(text) > 400 else ""),
            "channel_id": item.get("channel_id", ""),
            "team_id": item.get("team_id", ""),
            "timestamp_human": _ts_human(item.get("ts") or item.get("sk", "")),
        })
    return out


def retrieve_messages(team_id: str, channel_id: str, q: Optional[str] = None, from_date: Optional[str] = None,
                      to_date: Optional[str] = None, user_id: Optional[str] = None,
                      limit: int = 200, top_k: int = 10) -> list[dict]:
    require_ddb()
    pk = f"{team_id}#{channel_id}"
    key_expr = Key("pk").eq(pk)
    if from_date and to_date:
        key_expr = key_expr & Key("sk").between(_date_to_sk(from_date), _date_to_sk(to_date, end_of_day=True))
    elif from_date:
        key_expr = key_expr & Key("sk").gte(_date_to_sk(from_date))
    elif to_date:
        key_expr = key_expr & Key("sk").lte(_date_to_sk(to_date, end_of_day=True))
    kwargs = {"KeyConditionExpression": key_expr, "Limit": limit, "ScanIndexForward": False}
    if user_id:
        kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
    try:
        response = ddb_table.query(**kwargs)
        items = response.get("Items", [])
    except Exception as e:
        raise RuntimeError(f"DynamoDB query failed: {e}")
    # Always strip system/bot join-leave messages
    items = [i for i in items if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]
    if not q or not q.strip():
        return _format_messages(items[:top_k])
    matched = _score_messages(items, q)
    return _format_messages(matched[:top_k])


@app.get("/", response_class=HTMLResponse)
def home():
    if FRONTEND_PATH.exists():
        response = FileResponse(str(FRONTEND_PATH))
        return no_cache(response)
    return HTMLResponse(f"<h3>UI not found</h3><p>Expected at: <b>{FRONTEND_PATH}</b></p>", status_code=500)


@app.get("/health")
def health(response: Response):
    no_cache(response)
    return {
        "status": "ok",
        "region": AWS_REGION,
        "secret_prefix": SECRET_PREFIX,
        "redirect_uri": REDIRECT_URI,
        "frontend_path": str(FRONTEND_PATH),
        "frontend_path_exists": FRONTEND_PATH.exists(),
        "ddb_table": DDB_TABLE,
        "client_id_present": bool(CLIENT_ID),
        "client_secret_present": bool(CLIENT_SECRET),
    }


@app.get("/install")
def install():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return HTMLResponse("<h3>Missing ENV</h3><p>Set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_REDIRECT_URI</p>", status_code=500)
    params = {
        "client_id": CLIENT_ID,
        "scope": SLACK_SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": "slackbot_mvp",
    }
    return RedirectResponse("https://slack.com/oauth/v2/authorize?" + urlencode(params))


@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, error: str | None = None, state: str | None = None):
    if error:
        return HTMLResponse(f"""
        <html><body><script>
        if (window.opener) window.opener.postMessage({{"type":"slack_oauth_error","error":{json.dumps(error)}}}, "*");
        window.close();
        </script><p>Slack install failed. You can close this window.</p></body></html>
        """, status_code=400)
    if not code:
        return HTMLResponse("""
        <html><body><script>
        if (window.opener) window.opener.postMessage({"type":"slack_oauth_error","error":"missing_code"}, "*");
        window.close();
        </script><p>Missing code. You can close this window.</p></body></html>
        """, status_code=400)

    r = requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code, "redirect_uri": REDIRECT_URI},
        timeout=20,
    )
    data = r.json()
    if not data.get("ok"):
        err_text = json.dumps(data)
        return HTMLResponse(f"""
        <html><body><script>
        if (window.opener) window.opener.postMessage({{"type":"slack_oauth_error","error":{json.dumps(err_text)}}}, "*");
        window.close();
        </script><p>Slack install failed. You can close this window.</p></body></html>
        """, status_code=400)

    team = data.get("team") or {}
    team_id = team.get("id")
    team_name = team.get("name")
    bot_token = data.get("access_token")
    bot_user_id = data.get("bot_user_id")
    scope = data.get("scope")

    if not team_id or not bot_token:
        return HTMLResponse("""
        <html><body><script>
        if (window.opener) window.opener.postMessage({"type":"slack_oauth_error","error":"missing_team_or_token"}, "*");
        window.close();
        </script><p>Install failed. You can close this window.</p></body></html>
        """, status_code=500)

    try:
        upsert_secret(secret_name(team_id), {
            "team_id": team_id,
            "team_name": team_name,
            "bot_user_id": bot_user_id,
            "bot_token": bot_token,
            "scope": scope,
        })
    except Exception as e:
        return HTMLResponse(f"""
        <html><body><script>
        if (window.opener) window.opener.postMessage({{"type":"slack_oauth_error","error":{json.dumps(str(e))}}}, "*");
        window.close();
        </script><p>Install failed while saving token. You can close this window.</p></body></html>
        """, status_code=500)

    return HTMLResponse(f"""
    <html><body><script>
    if (window.opener) window.opener.postMessage({{"type":"slack_oauth_success","team_id":{json.dumps(team_id)},"team_name":{json.dumps(team_name or "")}}}, "*");
    window.close();
    </script><p>Slack workspace connected. You can close this window.</p></body></html>
    """)


@app.get("/token/status")
def token_status(team_id: str, response: Response):
    no_cache(response)
    s = read_secret(secret_name(team_id))
    if not s or "_error" in s:
        return {"ok": True, "team_id": team_id, "has_token": False, "error": (s or {}).get("_error")}
    token = s.get("bot_token", "")
    return {
        "ok": True,
        "team_id": team_id,
        "team_name": s.get("team_name"),
        "bot_user_id": s.get("bot_user_id"),
        "has_token": bool(token),
        "bot_token_masked": mask_token(token),
        "scope": s.get("scope"),
    }


@app.get("/workspaces")
def list_workspaces(response: Response):
    no_cache(response)
    workspaces = []
    seen = set()
    for page in secrets.get_paginator("list_secrets").paginate():
        for s in page.get("SecretList", []):
            name = s.get("Name", "")
            if not name.startswith(f"{SECRET_PREFIX}/"):
                continue
            if s.get("DeletedDate"):
                continue
            team_id = name.split(f"{SECRET_PREFIX}/")[-1]
            if not team_id or team_id in seen:
                continue
            sec = read_secret(name)
            if not sec or "_error" in sec:
                continue
            if not sec.get("bot_token"):
                continue
            workspaces.append({"team_id": team_id, "team_name": sec.get("team_name")})
            seen.add(team_id)
    workspaces.sort(key=lambda x: ((x.get("team_name") or "").lower(), x["team_id"].lower()))
    return {"ok": True, "workspaces": workspaces}


@app.delete("/workspaces/{team_id}")
def disconnect_workspace(team_id: str, response: Response):
    no_cache(response)
    name = secret_name(team_id)
    sec = read_secret(name)
    if not sec or "_error" in sec:
        return {"ok": False, "team_id": team_id, "message": "Secret not found"}
    bot_token = sec.get("bot_token")
    revoke_data = None
    if bot_token:
        try:
            revoke_data = requests.post(
                "https://slack.com/api/auth.revoke",
                headers={"Authorization": f"Bearer {bot_token}"},
                data={"test": "false"},
                timeout=20,
            ).json()
        except Exception as e:
            revoke_data = {"ok": False, "error": str(e)}
    try:
        secrets.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    except Exception as e:
        return {"ok": False, "team_id": team_id, "message": "Failed to delete secret", "detail": str(e), "revoked": revoke_data}
    return {"ok": True, "team_id": team_id, "revoked": revoke_data}


@app.get("/channels")
def list_channels(team_id: str, response: Response):
    no_cache(response)
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    r = requests.get(
        "https://slack.com/api/conversations.list",
        headers={"Authorization": f"Bearer {bot_token}"},
        params={"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "true"},
        timeout=20,
    )
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    channels = [{"id": c["id"], "name": c["name"]} for c in data.get("channels", [])]
    channels.sort(key=lambda c: c["name"].lower())
    return {"ok": True, "channels": channels}


@app.get("/fetch-messages")
def fetch_messages(team_id: str, channel_id: str, response: Response):
    no_cache(response)
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    r = requests.get(
        "https://slack.com/api/conversations.history",
        headers={"Authorization": f"Bearer {bot_token}"},
        params={"channel": channel_id, "limit": 50},
        timeout=20,
    )
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    return {"ok": True, "messages": [{"ts": m.get("ts"), "text": m.get("text"), "user": m.get("user")} for m in data.get("messages", [])]}


@app.post("/join-channel")
def join_channel(team_id: str, channel_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    data = requests.post(
        "https://slack.com/api/conversations.join",
        headers={"Authorization": f"Bearer {bot_token}"},
        data={"channel": channel_id},
        timeout=20,
    ).json()
    if not data.get("ok") and data.get("error") != "already_in_channel":
        return {"ok": False, "slack_error": data}
    return {"ok": True, "joined": True, "channel_id": channel_id}


@app.post("/join-all-public")
def join_all_public(team_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    joined, failed, cursor = [], [], None
    while True:
        params = {"limit": 200, "types": "public_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {bot_token}"},
            params=params,
            timeout=20,
        ).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            ch_id = ch["id"]
            j = requests.post(
                "https://slack.com/api/conversations.join",
                headers={"Authorization": f"Bearer {bot_token}"},
                data={"channel": ch_id},
                timeout=20,
            ).json()
            if j.get("ok") or j.get("error") == "already_in_channel":
                joined.append(ch_id)
            else:
                failed.append({"channel": ch_id, "error": j.get("error")})
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return {"ok": True, "joined_count": len(joined), "failed_count": len(failed), "failed": failed}


@app.post("/backfill-channel")
def backfill_channel(team_id: str, channel_id: str, limit: int = 200, cursor: str | None = None):
    require_ddb()
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    params = {"channel": channel_id, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = requests.get(
        "https://slack.com/api/conversations.history",
        headers={"Authorization": f"Bearer {bot_token}"},
        params=params,
        timeout=20,
    ).json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    msgs = data.get("messages", []) or []
    pk = f"{team_id}#{channel_id}"
    stored = 0
    for m in msgs:
        ts_msg = str(m.get("ts"))
        if not ts_msg:
            continue
        item = {
            "pk": pk,
            "sk": ts_msg,
            "team_id": team_id,
            "channel_id": channel_id,
            "ts": ts_msg,
            "user_id": m.get("user"),
            "text": m.get("text", ""),
            "thread_ts": m.get("thread_ts"),
            "reply_count": m.get("reply_count", 0),
            "subtype": m.get("subtype"),
            "type": m.get("type"),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        try:
            ddb_table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
            stored += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
    return {"ok": True, "channel_id": channel_id, "fetched": len(msgs), "stored_new": stored, "next_cursor": next_cursor, "has_more": bool(next_cursor)}


@app.post("/slack/events")
async def slack_events(request: Request):
    require_ddb()
    raw_body = await request.body()
    payload = json.loads(raw_body.decode("utf-8"))
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)
    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})
    event = payload.get("event") or {}
    if event.get("type") != "message":
        return JSONResponse({"ok": True})
    if event.get("bot_id") or event.get("subtype") in {"message_changed", "message_deleted"}:
        return JSONResponse({"ok": True})
    team_id = payload.get("team_id")
    channel_id = event.get("channel")
    ts_msg = event.get("ts")
    if not team_id or not channel_id or not ts_msg:
        return JSONResponse({"ok": True})
    item = {
        "pk": f"{team_id}#{channel_id}",
        "sk": str(ts_msg),
        "team_id": team_id,
        "channel_id": channel_id,
        "ts": str(ts_msg),
        "user_id": event.get("user"),
        "text": event.get("text", ""),
        "thread_ts": event.get("thread_ts"),
        "subtype": event.get("subtype"),
        "type": event.get("type"),
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        ddb_table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return JSONResponse({"ok": True})


@app.get("/db-messages")
def db_messages(team_id: str, channel_id: str, limit: int = 50, response: Response = None):
    if response is not None:
        no_cache(response)
    require_ddb()
    try:
        resp = ddb_table.query(KeyConditionExpression=Key("pk").eq(f"{team_id}#{channel_id}"), Limit=limit, ScanIndexForward=False)
        return {"ok": True, "source": "dynamodb", "count": resp.get("Count", 0), "items": resp.get("Items", [])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/search")
def api_search(team_id: str = Query(...), channel_id: str = Query(...), q: str | None = Query(None),
               from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to"),
               user_id: str | None = Query(None), limit: int = Query(200, ge=1, le=1000), top_k: int = Query(10, ge=1, le=12), response: Response = None):
    if response is not None:
        no_cache(response)
    request_id = str(uuid.uuid4())[:8]
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "'from' date must be before 'to' date")
    try:
        messages = retrieve_messages(team_id, channel_id, q, from_date, to_date, user_id, limit, top_k)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not messages:
        return {"ok": True, "request_id": request_id, "query": q, "count": 0, "messages": [], "note": "No messages found."}
    return {"ok": True, "request_id": request_id, "query": q, "filters": {"from": from_date, "to": to_date, "user_id": user_id}, "count": len(messages), "messages": messages}


class ChatRequest(BaseModel):
    team_id: str
    channel_id: str
    question: str
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    user_id: Optional[str] = None
    top_k: int = 10


class MultiChatRequest(BaseModel):
    team_id: str
    channel_ids: list[str]
    question: str
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    user_id: Optional[str] = None
    top_k: int = 10


def retrieve_messages_multi(
    team_id: str,
    channel_ids: list[str],
    q: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 200,
    top_k: int = 10,
) -> list[dict]:
    """Retrieve and merge messages from multiple channels, then rank by relevance."""
    all_items: list[dict] = []
    for channel_id in channel_ids:
        try:
            msgs = retrieve_messages(team_id, channel_id, q, from_date, to_date, user_id, limit, top_k * 3)
            all_items.extend(msgs)
        except Exception:
            pass  # skip channels that error
    if not all_items:
        return []
    if q and q.strip():
        keywords = re.findall(r"\w+", q.lower())
        def score(m):
            text = (m.get("text") or "").lower()
            s = sum(text.count(kw) for kw in keywords)
            s += sum(2 for kw in keywords if kw in text[:80])
            return s
        all_items.sort(key=score, reverse=True)
    else:
        all_items.sort(key=lambda m: m.get("message_ts", ""), reverse=True)
    return all_items[:top_k]


@app.get("/api/search/multi")
def api_search_multi(
    team_id: str = Query(...),
    channel_ids: str = Query(..., description="Comma-separated channel IDs"),
    q: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    user_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    top_k: int = Query(10, ge=1, le=50),
    response: Response = None,
):
    """Search messages across multiple channels."""
    if response is not None:
        no_cache(response)
    request_id = str(uuid.uuid4())[:8]
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "'from' date must be before 'to' date")
    ids = [c.strip() for c in channel_ids.split(",") if c.strip()]
    if not ids:
        raise HTTPException(400, "channel_ids must be a non-empty comma-separated list")
    messages = retrieve_messages_multi(team_id, ids, q, from_date, to_date, user_id, limit, top_k)
    if not messages:
        return {"ok": True, "request_id": request_id, "query": q, "count": 0, "messages": [], "channels_searched": len(ids), "note": "No messages found."}
    return {
        "ok": True,
        "request_id": request_id,
        "query": q,
        "filters": {"from": from_date, "to": to_date, "user_id": user_id},
        "channels_searched": len(ids),
        "count": len(messages),
        "messages": messages,
    }


@app.post("/api/chat/multi")
def api_chat_multi(body: MultiChatRequest, response: Response):
    """Ask AI a question using messages from multiple channels."""
    no_cache(response)
    if not body.question.strip():
        raise HTTPException(400, "question cannot be empty")
    if not body.channel_ids:
        raise HTTPException(400, "channel_ids must be non-empty")
    messages = retrieve_messages_multi(
        body.team_id, body.channel_ids, body.question,
        body.from_date, body.to_date, body.user_id,
        200, min(body.top_k, 20),
    )
    if not messages:
        return {"ok": True, "answer": "No relevant messages found across the selected channels for your question.", "citations": [], "channels_searched": len(body.channel_ids)}
    context_lines = "\n".join([
        f"[{i+1}] {m['timestamp_human']} | #{m.get('channel_id','')} | {m['username'] or m['user_id']}: {m['snippet']}"
        for i, m in enumerate(messages)
    ])
    prompt = f"""You are a helpful assistant that answers questions about Slack conversations spanning multiple channels.
Answer ONLY using the Slack messages provided below. Do NOT use outside knowledge.
Each message is labeled with a channel ID so you can reference which channel it came from.
If the answer is not in the messages, say: "I couldn't find that in the available messages."

SLACK MESSAGES (from {len(body.channel_ids)} channels):
{context_lines}

QUESTION: {body.question}

Respond in this format:
Answer: <your answer here>
Key points: <bullet points if relevant, otherwise skip>
Action items: <any action items mentioned, or 'None'>
Citations: <list message numbers like [1], [3]>"""
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not set")
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 1500},
        timeout=45,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise HTTPException(502, f"Groq error: {data}")
    answer_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    cited_indices = [int(n) - 1 for n in re.findall(r"\[(\d+)\]", answer_text) if n.isdigit() and 0 < int(n) <= len(messages)]
    citations = [messages[i] for i in dict.fromkeys(cited_indices)]
    return {
        "ok": True,
        "question": body.question,
        "answer": answer_text,
        "citations": citations,
        "retrieved_count": len(messages),
        "channels_searched": len(body.channel_ids),
    }


@app.post("/api/chat")
def api_chat(body: ChatRequest, response: Response):
    no_cache(response)
    if not body.question.strip():
        raise HTTPException(400, "question cannot be empty")
    messages = retrieve_messages(body.team_id, body.channel_id, body.question, body.from_date, body.to_date, body.user_id, 200, min(body.top_k, 12))
    if not messages:
        return {"ok": True, "answer": "No relevant messages found in this channel for your question.", "citations": []}
    context_lines = "\n".join([f"[{i+1}] {m['timestamp_human']} | {m['username'] or m['user_id']}: {m['snippet']}" for i, m in enumerate(messages)])
    prompt = f"""You are a helpful assistant that answers questions about Slack conversations.
Answer ONLY using the Slack messages provided below. Do NOT use outside knowledge.
If the answer is not in the messages, say: \"I couldn't find that in the available messages.\"

SLACK MESSAGES:
{context_lines}

QUESTION: {body.question}

Respond in this format:
Answer: <your answer here>
Key points: <bullet points if relevant, otherwise skip>
Action items: <any action items mentioned, or 'None'>
Citations: <list message numbers like [1], [3]>"""
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not set")
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 1024},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise HTTPException(502, f"Groq error: {data}")
    answer_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    cited_indices = [int(n) - 1 for n in re.findall(r"\[(\d+)\]", answer_text) if n.isdigit() and 0 < int(n) <= len(messages)]
    citations = [messages[i] for i in dict.fromkeys(cited_indices)]
    return {"ok": True, "question": body.question, "answer": answer_text, "citations": citations, "retrieved_count": len(messages)}


@app.get("/api/health")
def api_health(response: Response):
    return health(response)


@app.get("/api/install")
def api_install():
    return install()


@app.get("/api/oauth/callback")
def api_oauth_callback(code: str | None = None, error: str | None = None, state: str | None = None):
    return oauth_callback(code, error, state)


@app.get("/api/token/status")
def api_token_status(team_id: str, response: Response):
    return token_status(team_id, response)


@app.get("/api/workspaces")
def api_workspaces(response: Response):
    return list_workspaces(response)


@app.delete("/api/workspaces/{team_id}")
def api_disconnect_workspace(team_id: str, response: Response):
    return disconnect_workspace(team_id, response)


@app.get("/api/channels")
def api_channels(team_id: str, response: Response):
    return list_channels(team_id, response)


@app.get("/api/fetch-messages")
def api_fetch_messages(team_id: str, channel_id: str, response: Response):
    return fetch_messages(team_id, channel_id, response)


@app.post("/api/slack/events")
async def api_slack_events(request: Request):
    return await slack_events(request)


@app.get("/api/db-messages")
def api_db_messages(team_id: str, channel_id: str, limit: int = 50, response: Response = None):
    return db_messages(team_id, channel_id, limit, response)


@app.post("/api/join-channel")
def api_join_channel(team_id: str, channel_id: str):
    return join_channel(team_id, channel_id)


@app.post("/api/join-all-public")
def api_join_all_public(team_id: str):
    return join_all_public(team_id)


@app.post("/api/backfill-channel")
def api_backfill_channel(team_id: str, channel_id: str, limit: int = 200, cursor: str | None = None):
    return backfill_channel(team_id, channel_id, limit, cursor)


handler = Mangum(app)
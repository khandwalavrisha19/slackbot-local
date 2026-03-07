"""
app.py — Slackbot Backend (Full MVP) + serves frontend/index.html

✅ Works locally (uvicorn)
✅ Works on AWS Lambda + API Gateway (Mangum)
✅ UI can be served from this backend (/) OR you can host UI separately on S3/CloudFront

Required ENV:
- SLACK_CLIENT_ID
- SLACK_CLIENT_SECRET
- SLACK_REDIRECT_URI   (must exactly match Slack app redirect URL)
Optional ENV:
- AWS_REGION           (default: ap-south-1)
- SECRET_PREFIX        (default: slackbot)  -> secrets stored as slackbot/<TEAM_ID>
- FRONTEND_PATH        (default: ./frontend/index.html relative to this file)
- CORS_ORIGINS         (default: "*" for local dev; set your CloudFront domain in prod)
- SLACK_SCOPES         (default includes channels:read,channels:history,users:read,chat:write)
- GEMINI_API_KEY       (get free key at aistudio.google.com — used by /api/chat)
"""

import os
import re
import json
import uuid
import logging
from typing import Optional
from pathlib import Path
import requests
import boto3
from botocore.exceptions import ClientError
import time
import hmac
import hashlib
from datetime import datetime
from fastapi import Request, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from boto3.dynamodb.conditions import Key, Attr
from mangum import Mangum
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------- Env ----------------------
AWS_REGION     = os.getenv("AWS_REGION", "ap-south-1")
SECRET_PREFIX  = os.getenv("SECRET_PREFIX")
CLIENT_ID      = os.getenv("SLACK_CLIENT_ID")
CLIENT_SECRET  = os.getenv("SLACK_CLIENT_SECRET")
REDIRECT_URI   = os.getenv("SLACK_REDIRECT_URI")

SLACK_SCOPES = os.getenv(
    "SLACK_SCOPES",
    "scope: channels:history,chat:write,users:read,groups:history,channels:read,groups:read,channels:join"
)

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
origins = [o.strip() for o in CORS_ORIGINS.split(",")] if CORS_ORIGINS else ["*"]


# ---------------------- App ----------------------
app = FastAPI(title="Slackbot Full MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------- AWS Clients ----------------------
secrets   = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb  = boto3.resource("dynamodb", region_name=AWS_REGION)
DDB_TABLE = os.getenv("DDB_TABLE", "")
ddb_table = dynamodb.Table(DDB_TABLE)

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

# Groq — free tier, no region issues, works from India, no credit card needed.
# Get your free key at console.groq.com → API Keys → Create API Key
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")  # free, fast, good quality
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"


# ══════════════════════════════════════════════════════════════════
#  RETRIEVAL HELPER  (reused by /api/search AND /api/chat)
# ══════════════════════════════════════════════════════════════════

def retrieve_messages(
    team_id: str,
    channel_id: str,
    q: Optional[str]     = None,
    from_date: Optional[str] = None,   # "YYYY-MM-DD"
    to_date: Optional[str]   = None,   # "YYYY-MM-DD"
    user_id: Optional[str]   = None,
    limit: int = 200,
    top_k: int = 10,
) -> list[dict]:
    """
    Pull messages from DynamoDB, filter by date/user, rank by keyword relevance.
    Returns top_k clean message dicts.
    NOTE: matches your actual DynamoDB key names — lowercase pk and sk.
    """
    pk       = f"{team_id}#{channel_id}"
    key_expr = Key("pk").eq(pk)

    # Date range filter on sort key
    if from_date and to_date:
        key_expr = key_expr & Key("sk").between(
            _date_to_sk(from_date), _date_to_sk(to_date, end_of_day=True)
        )
    elif from_date:
        key_expr = key_expr & Key("sk").gte(_date_to_sk(from_date))
    elif to_date:
        key_expr = key_expr & Key("sk").lte(_date_to_sk(to_date, end_of_day=True))

    # Optional user filter
    filter_expr = Attr("user_id").eq(user_id) if user_id else None

    kwargs: dict = {
        "KeyConditionExpression": key_expr,
        "Limit": limit,
        "ScanIndexForward": False,   # newest first
    }
    if filter_expr:
        kwargs["FilterExpression"] = filter_expr

    try:
        response = ddb_table.query(**kwargs)
        items    = response.get("Items", [])
    except Exception as e:
        raise RuntimeError(f"DynamoDB query failed: {e}")

    if not q or not q.strip():
        return _format_messages(items[:top_k])

    return _format_messages(_score_messages(items, q)[:top_k])


def _date_to_sk(date_str: str, end_of_day: bool = False) -> str:
    epoch = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    return str(epoch + 86399 if end_of_day else epoch)


def _score_messages(items: list[dict], q: str) -> list[dict]:
    """Rank messages by keyword frequency — no embeddings needed."""
    keywords = re.findall(r"\w+", q.lower())
    if not keywords:
        return items
    scored = []
    for item in items:
        text  = (item.get("text") or "").lower()
        score = sum(text.count(kw) for kw in keywords)
        score += sum(2 for kw in keywords if kw in text[:80])  # early-match bonus
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    matched = [item for s, item in scored if s > 0]
    return matched if matched else [item for _, item in scored]


def _format_messages(items: list[dict]) -> list[dict]:
    results = []
    for item in items:
        text = (item.get("text") or "").strip()
        results.append({
            "message_ts":      item.get("ts") or item.get("sk", ""),
            "user_id":         item.get("user_id", "unknown"),
            "username":        item.get("username", ""),
            "text":            text,
            "snippet":         text[:400] + ("…" if len(text) > 400 else ""),
            "channel_id":      item.get("channel_id", ""),
            "team_id":         item.get("team_id", ""),
            "timestamp_human": _ts_human(item.get("ts") or item.get("sk", "")),
        })
    return results


def _ts_human(ts: str) -> str:
    try:
        return datetime.utcfromtimestamp(float(str(ts).split(".")[0])).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


# ══════════════════════════════════════════════════════════════════
#  /api/search  — Day 1
# ══════════════════════════════════════════════════════════════════

@app.get("/api/search")
def api_search(
    team_id:    str        = Query(...),
    channel_id: str        = Query(...),
    q:          str | None = Query(None),
    from_date:  str | None = Query(None, alias="from"),
    to_date:    str | None = Query(None, alias="to"),
    user_id:    str | None = Query(None),
    limit:      int        = Query(200, ge=1, le=1000),
    top_k:      int        = Query(10,  ge=1, le=12),
):
    """
    GET /api/search?team_id=T123&channel_id=C456&q=deployment&from=2024-01-01
    Returns top matching messages with metadata.
    """
    request_id = str(uuid.uuid4())[:8]
    logger.info(f"[{request_id}] /api/search team={team_id} channel={channel_id} q={repr(q)}")

    if not team_id.strip() or not channel_id.strip():
        raise HTTPException(400, "team_id and channel_id are required")
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "'from' date must be before 'to' date")

    try:
        messages = retrieve_messages(
            team_id=team_id, channel_id=channel_id,
            q=q, from_date=from_date, to_date=to_date,
            user_id=user_id, limit=limit, top_k=top_k,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    logger.info(f"[{request_id}] Returned {len(messages)} messages")

    if not messages:
        return {"ok": True, "request_id": request_id, "query": q,
                "count": 0, "messages": [],
                "note": "No messages found. Try a different keyword or date range."}

    return {
        "ok": True, "request_id": request_id, "query": q,
        "filters": {"from": from_date, "to": to_date, "user_id": user_id},
        "count": len(messages), "messages": messages,
    }


# ══════════════════════════════════════════════════════════════════
#  /api/chat  — Day 2 
# ══════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    team_id:    str
    channel_id: str
    question:   str
    from_date:  Optional[str] = None
    to_date:    Optional[str] = None
    user_id:    Optional[str] = None
    top_k:      int = 10


@app.post("/api/chat")
def api_chat(body: ChatRequest):
    """
    POST /api/chat
    { "team_id": "T123", "channel_id": "C456", "question": "What did we decide about X?" }

    Uses Gemini Flash (free). Get your key at aistudio.google.com → set GEMINI_API_KEY env var.
    """
    request_id = str(uuid.uuid4())[:8]

    try:
        return _api_chat_inner(body, request_id)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"[{request_id}] Unhandled error in /api/chat: {tb}")
        raise HTTPException(500, f"Unhandled error: {type(e).__name__}: {e}\n\nTraceback:\n{tb}")


def _api_chat_inner(body: ChatRequest, request_id: str):
    if not body.question.strip():
        raise HTTPException(400, "question cannot be empty")
    if len(body.question) > 500:
        raise HTTPException(400, "question too long (max 500 chars)")


    # Step 1: Retrieve relevant messages
    try:
        messages = retrieve_messages(
            team_id=body.team_id, channel_id=body.channel_id,
            q=body.question, from_date=body.from_date,
            to_date=body.to_date, user_id=body.user_id,
            top_k=min(body.top_k, 12),
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    logger.info(f"[{request_id}] /api/chat retrieved {len(messages)} msgs for q={repr(body.question)}")

    if not messages:
        return {
            "ok": True, "request_id": request_id,
            "answer": "No relevant messages found in this channel for your question.",
            "citations": [],
        }

    # Step 2: Build prompt
    context_lines = "\n".join([
        f"[{i+1}] {m['timestamp_human']} | {m['username'] or m['user_id']}: {m['snippet']}"
        for i, m in enumerate(messages)
    ])

    prompt = f"""You are a helpful assistant that answers questions about Slack conversations.
Answer ONLY using the Slack messages provided below. Do NOT use outside knowledge.
If the answer is not in the messages, say: "I couldn't find that in the available messages."

SLACK MESSAGES:
{context_lines}

QUESTION: {body.question}

Respond in this format:
Answer: <your answer here>
Key points: <bullet points if relevant, otherwise skip>
Action items: <any action items mentioned, or "None">
Citations: <list the message numbers you used, e.g. [1], [3]>"""

    # Step 3: Call Groq (free, works from India, no region issues, OpenAI-compatible)
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not set. Get free key at console.groq.com → API Keys")

    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       GROQ_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens":  1024,
            },
            timeout=30,
        )

        groq_data = resp.json()
        logger.info(f"[{request_id}] Groq status={resp.status_code} raw={json.dumps(groq_data)[:300]}")

        # Check for API errors
        if resp.status_code != 200:
            err = groq_data.get("error", {})
            raise HTTPException(502, f"Groq error {resp.status_code}: {err.get('message', groq_data)}")

        answer_text = (
            groq_data
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

        if not answer_text:
            raise HTTPException(502, f"Groq returned empty response: {groq_data}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{request_id}] Groq call failed: {e}")
        raise HTTPException(502, f"Groq error: {type(e).__name__}: {e}")

    # Step 4: Pull cited message numbers from answer text e.g. [1], [3]
    cited_indices = [
        int(n) - 1 for n in re.findall(r"\[(\d+)\]", answer_text)
        if n.isdigit() and 0 < int(n) <= len(messages)
    ]
    citations = [messages[i] for i in dict.fromkeys(cited_indices)]

    return {
        "ok":              True,
        "request_id":      request_id,
        "question":        body.question,
        "answer":          answer_text,
        "citations":       citations,
        "retrieved_count": len(messages),
    }


# ══════════════════════════════════════════════════════════════════
#  ALL EXISTING ROUTES (unchanged)
# ══════════════════════════════════════════════════════════════════

# ---------------------- Helpers ----------------------
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
    if abs(int(time.time()) - ts) > 60 * 5:
        return False
    base     = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest   = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature)


BASE_DIR      = Path(__file__).resolve().parents[2]
FRONTEND_PATH = BASE_DIR / "frontend" / "index.html"


@app.get("/", response_class=HTMLResponse)
def home():
    if FRONTEND_PATH.exists():
        return FileResponse(str(FRONTEND_PATH))
    return HTMLResponse(f"<h3>UI not found</h3><p>Expected at: <b>{FRONTEND_PATH}</b></p>", status_code=500)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "region": AWS_REGION,
        "secret_prefix": SECRET_PREFIX,
        "redirect_uri": REDIRECT_URI,
        "frontend_path_exists": FRONTEND_PATH.exists()
    }

from urllib.parse import urlencode

@app.get("/install")
def install():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return HTMLResponse("<h3>Missing ENV</h3><p>Set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_REDIRECT_URI</p>", status_code=500)
    params = {"client_id": CLIENT_ID, "scope": SLACK_SCOPES, "redirect_uri": REDIRECT_URI, "state": "slackbot_mvp"}
    return RedirectResponse("https://slack.com/oauth/v2/authorize?" + urlencode(params))

@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, error: str | None = None, state: str | None = None):
    if error:
        return HTMLResponse(f"<h3>Slack install failed</h3><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Slack install failed</h3><p>Missing code</p>", status_code=400)
    r    = requests.post("https://slack.com/api/oauth.v2.access",
                         data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                               "code": code, "redirect_uri": REDIRECT_URI}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return HTMLResponse(f"<h3>Slack install failed</h3><pre>{json.dumps(data, indent=2)}</pre>", status_code=400)
    team        = data.get("team") or {}
    team_id     = team.get("id")
    team_name   = team.get("name")
    bot_token   = data.get("access_token")
    bot_user_id = data.get("bot_user_id")
    scope       = data.get("scope")
    if not team_id or not bot_token:
        return HTMLResponse("<h3>Install failed</h3><p>Missing team_id or token</p>", status_code=500)
    try:
        upsert_secret(secret_name(team_id),
                      {"team_id": team_id, "team_name": team_name,
                       "bot_user_id": bot_user_id, "bot_token": bot_token, "scope": scope})
    except Exception as e:
        return HTMLResponse(f"<h3>Install failed while saving token</h3><pre>{str(e)}</pre>", status_code=500)
    UI_BASE = os.getenv("UI_BASE_URL","").rstrip("/")
    return RedirectResponse(url=f"{UI_BASE}/?team_id={team_id}", status_code=302)

@app.get("/token/status")
def token_status(team_id: str):
    s = read_secret(secret_name(team_id))
    if not s:
        return {"ok": False, "team_id": team_id, "has_token": False}
    if "_error" in s:
        return {"ok": True, "team_id": team_id, "has_token": False, "error": s["_error"]}
    token = s.get("bot_token", "")
    return {"ok": True, "team_id": team_id, "team_name": s.get("team_name"),
            "bot_user_id": s.get("bot_user_id"), "has_token": bool(token),
            "bot_token_masked": mask_token(token), "scope": s.get("scope")}

@app.get("/workspaces")
def list_workspaces():
    workspaces = []
    for page in secrets.get_paginator("list_secrets").paginate():
        for s in page.get("SecretList", []):
            name = s.get("Name", "")

            # skip secrets already scheduled/deleted
            if s.get("DeletedDate"):
                continue

            if name.startswith(f"{SECRET_PREFIX}/"):
                team_id = name.split(f"{SECRET_PREFIX}/")[-1]
                sec = read_secret(name)

                # skip unreadable/deleted/errored secrets
                if not sec or "_error" in sec:
                    continue

                # skip secrets with no bot_token — these are disconnected workspaces
                # that AWS hasn't fully purged yet (Secrets Manager has propagation delay)
                if not sec.get("bot_token"):
                    continue

                workspaces.append({
                    "team_id": team_id,
                    "team_name": sec.get("team_name")
                })

    return {"ok": True, "workspaces": workspaces}

@app.delete("/workspaces/{team_id}")
def disconnect_workspace(team_id: str):
    name = secret_name(team_id)
    sec  = read_secret(name)
    if not sec or "_error" in sec:
        return {"ok": False, "team_id": team_id, "message": "Secret not found"}
    bot_token   = sec.get("bot_token")
    revoke_data = None
    if bot_token:
        revoke_data = requests.post("https://slack.com/api/auth.revoke",
                                    headers={"Authorization": f"Bearer {bot_token}"},
                                    data={"test": "false"}, timeout=20).json()
    try:
        secrets.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    except Exception as e:
        return {"ok": False, "team_id": team_id, "message": "Failed to delete secret",
                "detail": str(e), "revoked": revoke_data}
    return {"ok": True, "team_id": team_id, "revoked": revoke_data}

@app.get("/channels")
def list_channels(team_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    r    = requests.get("https://slack.com/api/conversations.list",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        params={"limit": 200, "types": "public_channel,private_channel",
                                "exclude_archived": "true"}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    return {"ok": True, "channels": [{"id": c["id"], "name": c["name"]} for c in data.get("channels", [])]}

@app.get("/fetch-messages")
def fetch_messages(team_id: str, channel_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}
    r    = requests.get("https://slack.com/api/conversations.history",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        params={"channel": channel_id, "limit": 50}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    return {"ok": True, "messages": [{"ts": m.get("ts"), "text": m.get("text"), "user": m.get("user")}
                                      for m in data.get("messages", [])]}

@app.post("/slack/events")
async def slack_events(request: Request):
    raw_body = await request.body()
    payload  = json.loads(raw_body.decode("utf-8"))
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)
    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})
    event = payload.get("event") or {}
    if event.get("type") != "message": return JSONResponse({"ok": True})
    if event.get("bot_id"):            return JSONResponse({"ok": True})
    if event.get("subtype") in {"message_changed", "message_deleted"}: return JSONResponse({"ok": True})
    team_id    = payload.get("team_id")
    channel_id = event.get("channel")
    ts_msg     = event.get("ts")
    if not team_id or not channel_id or not ts_msg: return JSONResponse({"ok": True})
    item = {"pk": f"{team_id}#{channel_id}", "sk": str(ts_msg), "team_id": team_id,
            "channel_id": channel_id, "ts": str(ts_msg), "user_id": event.get("user"),
            "text": event.get("text", ""), "thread_ts": event.get("thread_ts"),
            "subtype": event.get("subtype"), "type": event.get("type"),
            "fetched_at": datetime.utcnow().isoformat() + "Z"}
    try:
        ddb_table.put_item(Item=item,
                           ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException": raise
    return JSONResponse({"ok": True})

@app.get("/db-messages")
def db_messages(team_id: str, channel_id: str, limit: int = 50):
    try:
        resp = ddb_table.query(KeyConditionExpression=Key("pk").eq(f"{team_id}#{channel_id}"),
                               Limit=limit, ScanIndexForward=False)
        return {"ok": True, "source": "dynamodb", "count": resp.get("Count", 0), "items": resp.get("Items", [])}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/join-channel")
def join_channel(team_id: str, channel_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec: return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token: return {"ok": False, "message": "bot_token missing"}
    data = requests.post("https://slack.com/api/conversations.join",
                         headers={"Authorization": f"Bearer {bot_token}"},
                         data={"channel": channel_id}, timeout=20).json()
    if not data.get("ok") and data.get("error") != "already_in_channel":
        return {"ok": False, "slack_error": data}
    return {"ok": True, "joined": True, "channel_id": channel_id}

@app.post("/join-all-public")
def join_all_public(team_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec: return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token: return {"ok": False, "message": "bot_token missing"}
    joined, failed, cursor = [], [], None
    while True:
        params = {"limit": 200, "types": "public_channel", "exclude_archived": "true"}
        if cursor: params["cursor"] = cursor
        lst = requests.get("https://slack.com/api/conversations.list",
                           headers={"Authorization": f"Bearer {bot_token}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"): return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            ch_id = ch["id"]
            j = requests.post("https://slack.com/api/conversations.join",
                              headers={"Authorization": f"Bearer {bot_token}"},
                              data={"channel": ch_id}, timeout=20).json()
            (joined if j.get("ok") or j.get("error") == "already_in_channel"
             else failed).append(ch_id if j.get("ok") or j.get("error") == "already_in_channel"
                                 else {"channel": ch_id, "error": j.get("error")})
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor: break
    return {"ok": True, "joined_count": len(joined), "failed_count": len(failed), "failed": failed}

@app.post("/backfill-channel")
def backfill_channel(team_id: str, channel_id: str, limit: int = 200, cursor: str | None = None):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec: return {"ok": False, "message": "Secret not found", "detail": sec}
    bot_token = sec.get("bot_token")
    if not bot_token: return {"ok": False, "message": "bot_token missing"}
    params = {"channel": channel_id, "limit": limit}
    if cursor: params["cursor"] = cursor
    data = requests.get("https://slack.com/api/conversations.history",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        params=params, timeout=20).json()
    if not data.get("ok"): return {"ok": False, "slack_error": data}
    msgs   = data.get("messages", []) or []
    pk     = f"{team_id}#{channel_id}"
    stored = 0
    for m in msgs:
        ts_msg = str(m.get("ts"))
        if not ts_msg: continue
        item = {"pk": pk, "sk": ts_msg, "team_id": team_id, "channel_id": channel_id,
                "ts": ts_msg, "user_id": m.get("user"), "text": m.get("text", ""),
                "thread_ts": m.get("thread_ts"), "reply_count": m.get("reply_count", 0),
                "subtype": m.get("subtype"), "type": m.get("type"),
                "fetched_at": datetime.utcnow().isoformat() + "Z"}
        try:
            ddb_table.put_item(Item=item,
                               ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
            stored += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException": raise
    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
    return {"ok": True, "channel_id": channel_id, "fetched": len(msgs),
            "stored_new": stored, "next_cursor": next_cursor, "has_more": bool(next_cursor)}


# ── CloudFront /api/* alias routes ────────────────────────────────
@app.get("/api/health")
def api_health(): return health()

@app.get("/api/install")
def api_install(): return install()

@app.get("/api/oauth/callback")
def api_oauth_callback(code: str | None = None, error: str | None = None, state: str | None = None):
    return oauth_callback(code=code, error=error, state=state)

@app.get("/api/token/status")
def api_token_status(team_id: str): return token_status(team_id=team_id)

@app.get("/api/workspaces")
def api_workspaces(): return list_workspaces()

@app.delete("/api/workspaces/{team_id}")
def api_disconnect_workspace(team_id: str): return disconnect_workspace(team_id=team_id)

@app.get("/api/channels")
def api_channels(team_id: str): return list_channels(team_id=team_id)

@app.get("/api/fetch-messages")
def api_fetch_messages(team_id: str, channel_id: str): return fetch_messages(team_id=team_id, channel_id=channel_id)

@app.post("/api/slack/events")
async def api_slack_events(request: Request): return await slack_events(request)

@app.get("/api/db-messages")
def api_db_messages(team_id: str, channel_id: str, limit: int = 50):
    return db_messages(team_id=team_id, channel_id=channel_id, limit=limit)

@app.post("/api/join-channel")
def api_join_channel(team_id: str, channel_id: str): return join_channel(team_id=team_id, channel_id=channel_id)

@app.post("/api/join-all-public")
def api_join_all_public(team_id: str): return join_all_public(team_id=team_id)

@app.post("/api/backfill-channel")
def api_backfill_channel(team_id: str, channel_id: str, limit: int = 200, cursor: str | None = None):
    return backfill_channel(team_id=team_id, channel_id=channel_id, limit=limit, cursor=cursor)


# ── Lambda handler ────────────────────────────────────────────────
handler = Mangum(app)
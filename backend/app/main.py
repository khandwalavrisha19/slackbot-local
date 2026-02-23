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
"""

import os
import json
from typing import Optional
from pathlib import Path
import requests
import boto3
from botocore.exceptions import ClientError
import time
import hmac
import hashlib
from datetime import datetime
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from boto3.dynamodb.conditions import Key
from mangum import Mangum


# ---------------------- Env ----------------------
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
SECRET_PREFIX = os.getenv("SECRET_PREFIX", "slackbot")
CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI")  # MUST match Slack app redirect URL exactly

# Slack scopes (add more later if you need)
SLACK_SCOPES = os.getenv(
    "SLACK_SCOPES",
    "channels:read,channels:history,users:read,chat:write"
)

# CORS (for when UI is hosted separately)
# In production, set this to your CloudFront domain(s), e.g. "https://bot.example.com,https://app.example.com"
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
secrets = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
DDB_TABLE = os.getenv("DDB_TABLE", "SlackMessagesV2")
ddb_table = dynamodb.Table(DDB_TABLE)

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")



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

    # Prevent replay attacks (5 minutes)
    try:
        ts = int(timestamp)
    except ValueError:
        return False

    if abs(int(time.time()) - ts) > 60 * 5:
        return False

    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = "v0=" + digest

    return hmac.compare_digest(expected, signature)


BASE_DIR = Path(__file__).resolve().parents[2]   # goes to "SLACK BOT" root
FRONTEND_PATH = BASE_DIR / "frontend" / "index.html"


@app.get("/", response_class=HTMLResponse)
def home():
    if FRONTEND_PATH.exists():
        return FileResponse(str(FRONTEND_PATH))
    return HTMLResponse(
        f"<h3>UI not found</h3><p>Expected at: <b>{FRONTEND_PATH}</b></p>",
        status_code=500
    )


# ---------------------- Core ----------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "region": AWS_REGION,
        "secret_prefix": SECRET_PREFIX,
        "redirect_uri": REDIRECT_URI,
        "frontend_path_exists": FRONTEND_PATH.exists(),
    }

# @app.get("/install")
# def install():
#     if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
#         return HTMLResponse(
#             "<h3>Missing ENV</h3><p>Set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_REDIRECT_URI</p>",
#             status_code=500
#         )

#     url = (
#         "https://slack.com/oauth/v2/authorize"
#         f"?client_id={CLIENT_ID}"
#         f"&scope={SLACK_SCOPES}"
#         f"&redirect_uri={REDIRECT_URI}"
#     )
#     return RedirectResponse(url)
from urllib.parse import urlencode

@app.get("/install")
def install():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return HTMLResponse(
            "<h3>Missing ENV</h3><p>Set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_REDIRECT_URI</p>",
            status_code=500
        )

    params = {
    "client_id": CLIENT_ID,
    "scope": SLACK_SCOPES,
    "redirect_uri": REDIRECT_URI,
    "state": "slackbot_mvp"
    }

    url = "https://slack.com/oauth/v2/authorize?" + urlencode(params)
    return RedirectResponse(url)

@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, error: str | None = None, state: str | None = None):
    if error:
        return HTMLResponse(f"<h3>Slack install failed</h3><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Slack install failed</h3><p>Missing code</p>", status_code=400)
        

    # Exchange code for token
    r = requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20,
    )
    data = r.json()

    if not data.get("ok"):
        return HTMLResponse(
            "<h3>Slack install failed</h3>"
            f"<pre>{json.dumps(data, indent=2)}</pre>",
            status_code=400,
        )


    team = data.get("team") or {}
    team_id = team.get("id")
    team_name = team.get("name")

    bot_token = data.get("access_token")
    bot_user_id = data.get("bot_user_id")
    scope = data.get("scope")

    if not team_id or not bot_token:
        return HTMLResponse("<h3>Install failed</h3><p>Missing team_id or token</p>", status_code=500)

    try:
        upsert_secret(
        secret_name(team_id),
        {
            "team_id": team_id,
            "team_name": team_name,
            "bot_user_id": bot_user_id,
            "bot_token": bot_token,
            "scope": scope,
        },
    )
    except Exception as e:
        return HTMLResponse(
            "<h3>Install failed while saving token</h3>"
            f"<pre>{str(e)}</pre>",
            status_code=500,
        )

    #https://fcemnui289.execute-api.ap-south-1.amazonaws.com

    UI_BASE = os.getenv("UI_BASE_URL", "https://d2bl75rwuudy2k.cloudfront.net")
    UI_BASE = UI_BASE.rstrip("/")
    return RedirectResponse(url=f"{UI_BASE}/?team_id={team_id}", status_code=302)


@app.get("/token/status")
def token_status(team_id: str):
    s = read_secret(secret_name(team_id))
    if not s:
        return {"ok": False, "team_id": team_id, "has_token": False}
    if "_error" in s:
        return {"ok": True, "team_id": team_id, "has_token": False, "error": s["_error"]}

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


# ---------------------- Full Dashboard Endpoints ----------------------
@app.get("/workspaces")
def list_workspaces():
    workspaces = []
    paginator = secrets.get_paginator("list_secrets")

    for page in paginator.paginate():
        for s in page.get("SecretList", []):
            name = s.get("Name", "")
            if name.startswith(f"{SECRET_PREFIX}/"):
                team_id = name.split(f"{SECRET_PREFIX}/")[-1]
                sec = read_secret(name)
                team_name = None
                if sec and "_error" not in sec:
                    team_name = sec.get("team_name")
                workspaces.append({"team_id": team_id, "team_name": team_name})

    return {"ok": True, "workspaces": workspaces}

@app.delete("/workspaces/{team_id}")
def disconnect_workspace(team_id: str):
    name = secret_name(team_id)
    sec = read_secret(name)
    if not sec or "_error" in sec:
        return {"ok": False, "team_id": team_id, "message": "Secret not found"}

    bot_token = sec.get("bot_token")
    revoke_data = None

    # Revoke token on Slack
    if bot_token:
        r = requests.post(
            "https://slack.com/api/auth.revoke",
            headers={"Authorization": f"Bearer {bot_token}"},
            data={"test": "false"},
            timeout=20,
        )
        revoke_data = r.json()

    # Delete secret
    try:
        secrets.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    except Exception as e:
        return {
            "ok": False,
            "team_id": team_id,
            "message": "Failed to delete secret",
            "detail": str(e),
            "revoked": revoke_data,
        }

    return {"ok": True, "team_id": team_id, "revoked": revoke_data}

@app.get("/channels")
def list_channels(team_id: str):
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found", "detail": sec}

    bot_token = sec.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}

    r = requests.get(
        "https://slack.com/api/conversations.list",
        headers={"Authorization": f"Bearer {bot_token}"},
        params={
            "limit": 200,
            "types": "public_channel,private_channel",
            "exclude_archived": "true",
        },
        timeout=20,
    )
    data = r.json()

    if not data.get("ok"):
        return {"ok": False, "slack_error": data}

    channels = [{"id": c["id"], "name": c["name"]} for c in data.get("channels", [])]
    return {"ok": True, "channels": channels}

@app.get("/fetch-messages")
def fetch_messages(team_id: str, channel_id: str):
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

    messages = [{
        "ts": m.get("ts"),
        "text": m.get("text"),
        "user": m.get("user"),
    } for m in data.get("messages", [])]

    return {"ok": True, "messages": messages}

@app.post("/slack/events")
async def slack_events(request: Request):
    raw_body = await request.body()
    payload = json.loads(raw_body.decode("utf-8"))

    # FIRST handle URL verification WITHOUT signature check
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    # Now verify signature for real events
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)

    payload = await request.json()

    # Slack URL verification handshake
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    event = payload.get("event") or {}

    # We only care about message events
    if event.get("type") != "message":
        return JSONResponse({"ok": True})

    # Ignore bot messages and edits/deletes
    if event.get("bot_id"):
        return JSONResponse({"ok": True})
    if event.get("subtype") in {"message_changed", "message_deleted"}:
        return JSONResponse({"ok": True})

    team_id = payload.get("team_id")
    channel_id = event.get("channel")
    ts_msg = event.get("ts")
    text = event.get("text", "")
    user_id = event.get("user")

    if not team_id or not channel_id or not ts_msg:
        return JSONResponse({"ok": True})

    item = {
        # V2 key design
        "pk": f"{team_id}#{channel_id}",
        "sk": str(ts_msg),

        "team_id": team_id,
        "channel_id": channel_id,
        "ts": str(ts_msg),
        "user_id": user_id,
        "text": text,

        "thread_ts": event.get("thread_ts"),
        "subtype": event.get("subtype"),
        "type": event.get("type"),
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }

    # Slack retries deliveries; avoid duplicates
    try:
        ddb_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    return JSONResponse({"ok": True})

@app.get("/db-messages")
def db_messages(team_id: str, channel_id: str, limit: int = 50):
    try:
        pk = f"{team_id}#{channel_id}"

        resp = ddb_table.query(
            KeyConditionExpression=Key("pk").eq(pk),
            Limit=limit,
            ScanIndexForward=False  # newest first
        )

        return {
            "ok": True,
            "source": "dynamodb",
            "count": resp.get("Count", 0),
            "items": resp.get("Items", [])
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }

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

    # already_in_channel is fine
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

    joined, failed = [], []
    cursor = None

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
            ddb_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)"
            )
            stored += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""

    return {
        "ok": True,
        "channel_id": channel_id,
        "fetched": len(msgs),
        "stored_new": stored,
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }

# ---------------------- CloudFront "/api/*" ALIAS ROUTES ----------------------
# Your CloudFront routes use /api/*, but your app currently exposes non-/api routes.
# These aliases keep old URLs working AND make CloudFront /api URLs work.

@app.get("/api/health")
def api_health():
    return health()

@app.get("/api/install")
def api_install():
    return install()


@app.get("/api/oauth/callback")
def api_oauth_callback(code: str | None = None, error: str | None = None, state: str | None = None):
    return oauth_callback(code=code, error=error, state=state)


@app.get("/api/token/status")
def api_token_status(team_id: str):
    return token_status(team_id=team_id)

@app.get("/api/workspaces")
def api_workspaces():
    return list_workspaces()

@app.delete("/api/workspaces/{team_id}")
def api_disconnect_workspace(team_id: str):
    return disconnect_workspace(team_id=team_id)

@app.get("/api/channels")
def api_channels(team_id: str):
    return list_channels(team_id=team_id)

@app.get("/api/fetch-messages")
def api_fetch_messages(team_id: str, channel_id: str):
    return fetch_messages(team_id=team_id, channel_id=channel_id)

@app.post("/api/slack/events")
async def api_slack_events(request: Request):
    return await slack_events(request)

@app.get("/api/db-messages")
def api_db_messages(team_id: str, channel_id: str, limit: int = 50):
    return db_messages(team_id=team_id, channel_id=channel_id, limit=limit)

@app.post("/api/join-channel")
def api_join_channel(team_id: str, channel_id: str):
    return join_channel(team_id=team_id, channel_id=channel_id)

@app.post("/api/join-all-public")
def api_join_all_public(team_id: str):
    return join_all_public(team_id=team_id)

@app.post("/api/backfill-channel")
def api_backfill_channel(team_id: str, channel_id: str, limit: int = 200, cursor: str | None = None):
    return backfill_channel(team_id=team_id, channel_id=channel_id, limit=limit, cursor=cursor)

# ---------------------- Lambda Handler ----------------------
handler = Mangum(app)




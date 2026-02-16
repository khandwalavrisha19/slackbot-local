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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse

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

# ---------------------- Lambda Handler ----------------------
handler = Mangum(app)




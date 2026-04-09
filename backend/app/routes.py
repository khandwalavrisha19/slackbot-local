import re
import json
import uuid
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Request, Query, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from urllib.parse import urlencode

from app.constants import (
    CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SLACK_SCOPES, SLACK_SIGNING_SECRET,
    FRONTEND_PATH, SESSION_COOKIE_NAME,
    MAX_BODY_BYTES, MAX_TOKENS_SINGLE, MAX_TOKENS_MULTI, SLACK_API_BASE, SLACK_OAUTH_BASE,
)
from app.logger import logger
from app.utils import (
    no_cache, secret_name, read_secret, upsert_secret, delete_secret, mask_token,
    verify_slack_signature, resolve_username_for_message, extract_username_from_question,
)
from app.session import (
    get_or_create_session, require_team_access, create_session,
    get_session, bind_team_to_session, unbind_team_from_session, _set_session_cookie,
)
from app.retrieval import (
    retrieve_messages, retrieve_messages_multi,
    _build_context, _augment_question_with_senders,
)
from app.groq_client import _groq_complete
from app.models import ChatRequest, MultiChatRequest

router = APIRouter()


# ── FRONTEND ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def home():
    if FRONTEND_PATH.exists():
        return no_cache(FileResponse(str(FRONTEND_PATH)))
    return HTMLResponse(f"<h3>UI not found at {FRONTEND_PATH}</h3>", status_code=500)


# ── HEALTH ────────────────────────────────────────────────────────────────────

@router.get("/health")
@router.get("/api/health")
def health(response: Response):
    no_cache(response)
    return {
        "status": "ok",
        "client_id_present": bool(CLIENT_ID),
    }


# ── SESSION ───────────────────────────────────────────────────────────────────

@router.get("/api/session")
def api_get_session(request: Request, response: Response):
    no_cache(response)
    session_id, sess = get_or_create_session(request, response)
    return {"ok": True, "session_id": session_id, "team_ids": sess.get("team_ids", [])}


@router.post("/api/logout")
def api_logout(request: Request, response: Response):
    no_cache(response)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


# ── OAUTH ─────────────────────────────────────────────────────────────────────

@router.get("/install")
@router.get("/api/install")
def install():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return HTMLResponse("<h3>Missing ENV</h3>", status_code=500)
    params = {"client_id": CLIENT_ID, "scope": SLACK_SCOPES,
              "redirect_uri": REDIRECT_URI, "state": "slackbot_mvp"}
    return RedirectResponse(f"{SLACK_OAUTH_BASE}/authorize?" + urlencode(params))


@router.get("/oauth/callback")
@router.get("/api/oauth/callback")
def oauth_callback(
    request: Request, response: Response,
    code: str | None = None, error: str | None = None, state: str | None = None,
):
    def _err(msg):
        return HTMLResponse(f"""<html><body><script>
        if(window.opener)window.opener.postMessage({{"type":"slack_oauth_error","error":{json.dumps(msg)}}},"*");
        window.close();</script><p>Failed.</p></body></html>""", status_code=400)

    if error:    return _err(error)
    if not code: return _err("missing_code")

    r    = requests.post(f"{SLACK_API_BASE}/oauth.v2.access",
                         data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                               "code": code, "redirect_uri": REDIRECT_URI}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return _err(json.dumps(data))

    team      = data.get("team") or {}
    team_id   = team.get("id")
    team_name = team.get("name")
    bot_token = data.get("access_token")

    if not team_id or not bot_token:
        return _err("missing_team_or_token")

    try:
        upsert_secret(secret_name(team_id), {
            "team_id": team_id, "team_name": team_name,
            "bot_user_id": data.get("bot_user_id"),
            "bot_token": bot_token, "scope": data.get("scope"),
        })
    except Exception as e:
        return _err(str(e))

    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val and get_session(cookie_val):
        bind_team_to_session(cookie_val, team_id)
    else:
        new_sid = create_session()
        bind_team_to_session(new_sid, team_id)
        _set_session_cookie(response, new_sid)

    return HTMLResponse(f"""<html><body><script>
    if(window.opener)window.opener.postMessage({{"type":"slack_oauth_success","team_id":{json.dumps(team_id)},"team_name":{json.dumps(team_name or "")}}},"*");
    window.close();</script><p>Connected.</p></body></html>""")


# ── WORKSPACES ────────────────────────────────────────────────────────────────

@router.get("/workspaces")
@router.get("/api/workspaces")
def list_workspaces(request: Request, response: Response):
    no_cache(response)
    session_id, sess = get_or_create_session(request, response)
    allowed          = sess.get("team_ids", [])
    workspaces       = []
    for team_id in allowed:
        sec = read_secret(secret_name(team_id))
        if not sec or "_error" in sec or not sec.get("bot_token"):
            continue
        workspaces.append({"team_id": team_id, "team_name": sec.get("team_name")})
    workspaces.sort(key=lambda x: ((x.get("team_name") or "").lower(), x["team_id"].lower()))
    return {"ok": True, "workspaces": workspaces}


@router.delete("/workspaces/{team_id}")
@router.delete("/api/workspaces/{team_id}")
def disconnect_workspace(team_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    name = secret_name(team_id)
    sec  = read_secret(name)
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found"}
    revoke_data = None
    if sec.get("bot_token"):
        try:
            revoke_data = requests.post(f"{SLACK_API_BASE}/auth.revoke",
                headers={"Authorization": f"Bearer {sec['bot_token']}"},
                data={"test": "false"}, timeout=20).json()
        except Exception as e:
            revoke_data = {"ok": False, "error": str(e)}
    try:
        delete_secret(team_id)
    except Exception as e:
        return {"ok": False, "detail": str(e), "revoked": revoke_data}
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        unbind_team_from_session(cookie_val, team_id)
    return {"ok": True, "team_id": team_id, "revoked": revoke_data}


# ── TOKEN STATUS ──────────────────────────────────────────────────────────────

@router.get("/token/status")
@router.get("/api/token/status")
def token_status(team_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    s = read_secret(secret_name(team_id))
    if not s or "_error" in s:
        return {"ok": True, "team_id": team_id, "has_token": False}
    token = s.get("bot_token", "")
    return {"ok": True, "team_id": team_id, "team_name": s.get("team_name"),
            "has_token": bool(token), "bot_token_masked": mask_token(token), "scope": s.get("scope")}


# ── CHANNELS ──────────────────────────────────────────────────────────────────

@router.get("/channels")
@router.get("/api/channels")
def list_channels(team_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    r    = requests.get(f"{SLACK_API_BASE}/conversations.list",
                        headers={"Authorization": f"Bearer {sec['bot_token']}"},
                        params={"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "true"},
                        timeout=20)
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    channels = sorted([{"id": c["id"], "name": c["name"]} for c in data.get("channels", [])],
                      key=lambda c: c["name"].lower())
    return {"ok": True, "channels": channels}


# ── FETCH MESSAGES ────────────────────────────────────────────────────────────

@router.get("/fetch-messages")
@router.get("/api/fetch-messages")
def fetch_messages(team_id: str, channel_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    r    = requests.get(f"{SLACK_API_BASE}/conversations.history",
                        headers={"Authorization": f"Bearer {sec['bot_token']}"},
                        params={"channel": channel_id, "limit": 50}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    return {"ok": True, "messages": [{"ts": m.get("ts"), "text": m.get("text"), "user": m.get("user")}
                                      for m in data.get("messages", [])]}


# ── JOIN CHANNEL ──────────────────────────────────────────────────────────────

@router.post("/join-channel")
@router.post("/api/join-channel")
def join_channel(team_id: str, channel_id: str, request: Request):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    data = requests.post(f"{SLACK_API_BASE}/conversations.join",
                         headers={"Authorization": f"Bearer {sec['bot_token']}"},
                         data={"channel": channel_id}, timeout=20).json()
    if not data.get("ok") and data.get("error") != "already_in_channel":
        return {"ok": False, "slack_error": data}
    return {"ok": True, "joined": True, "channel_id": channel_id}


# ── JOIN ALL PUBLIC ───────────────────────────────────────────────────────────

@router.post("/join-all-public")
@router.post("/api/join-all-public")
def join_all_public(team_id: str, request: Request):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    joined, failed, cursor = [], [], None
    while True:
        params = {"limit": 200, "types": "public_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get(f"{SLACK_API_BASE}/conversations.list",
                           headers={"Authorization": f"Bearer {sec['bot_token']}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            ch_id = ch["id"]
            j = requests.post(f"{SLACK_API_BASE}/conversations.join",
                              headers={"Authorization": f"Bearer {sec['bot_token']}"},
                              data={"channel": ch_id}, timeout=20).json()
            if j.get("ok") or j.get("error") == "already_in_channel":
                joined.append(ch_id)
            else:
                failed.append({"channel": ch_id, "error": j.get("error")})
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return {"ok": True, "joined_count": len(joined), "failed_count": len(failed), "failed": failed}


# ── BACKFILL CHANNEL ──────────────────────────────────────────────────────────

@router.post("/backfill-channel")
@router.post("/api/backfill-channel")
def backfill_channel(team_id: str, channel_id: str, request: Request, limit: int = 200, cursor: str | None = None):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    params = {"channel": channel_id, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = requests.get(f"{SLACK_API_BASE}/conversations.history",
                        headers={"Authorization": f"Bearer {sec['bot_token']}"},
                        params=params, timeout=20).json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    msgs = data.get("messages", []) or []
    pk   = f"{team_id}#{channel_id}"
    stored = 0
    for m in msgs:
        ts_msg = str(m.get("ts"))
        if not ts_msg:
            continue
        uid      = m.get("user")
        username = resolve_username_for_message(team_id, uid, sec["bot_token"]) if uid else ""
        item = {
            "pk": pk, "sk": ts_msg,
            "team_id": team_id, "channel_id": channel_id, "ts": ts_msg,
            "user_id": uid, "username": username, "text": m.get("text", ""),
            "thread_ts": m.get("thread_ts"), "reply_count": m.get("reply_count", 0),
            "subtype": m.get("subtype"), "type": m.get("type"),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        from app.db import get_conn
        with get_conn() as conn:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO messages
                    (pk,sk,team_id,channel_id,ts,user_id,username,text,thread_ts,reply_count,subtype,type,fetched_at)
                    VALUES(:pk,:sk,:team_id,:channel_id,:ts,:user_id,:username,:text,:thread_ts,:reply_count,:subtype,:type,:fetched_at)
                """, item)
                stored += conn.rowcount
            except Exception as e:
                raise
    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
    return {"ok": True, "channel_id": channel_id, "fetched": len(msgs),
            "stored_new": stored, "next_cursor": next_cursor, "has_more": bool(next_cursor)}


# ── BACKFILL ALL PUBLIC ───────────────────────────────────────────────────────

@router.post("/backfill-all-public")
@router.post("/api/backfill-all-public")
def backfill_all_public(team_id: str, request: Request):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    all_channels, cursor = [], None
    while True:
        params = {"limit": 200, "types": "public_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get(f"{SLACK_API_BASE}/conversations.list",
                           headers={"Authorization": f"Bearer {sec['bot_token']}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            if ch.get("is_member"):
                all_channels.append(ch["id"])
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    total_stored, results = 0, []
    for ch_id in all_channels:
        bf_cursor, stored, ok = "", 0, True
        while True:
            bf = backfill_channel(team_id=team_id, channel_id=ch_id, request=request,
                                   limit=200, cursor=bf_cursor if bf_cursor else None)
            if not bf.get("ok"):
                ok = False
                break
            stored += bf.get("stored_new", 0)
            if not bf.get("has_more"):
                break
            bf_cursor = bf.get("next_cursor", "")
        results.append({"channel": ch_id, "ok": ok, "stored": stored})
        if ok:
            total_stored += stored
    return {"ok": True, "total_stored": total_stored, "results": results}


# ── BACKFILL ALL PRIVATE ──────────────────────────────────────────────────────

@router.post("/backfill-all-private")
@router.post("/api/backfill-all-private")
def backfill_all_private(team_id: str, request: Request):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    all_channels, cursor = [], None
    while True:
        params = {"limit": 200, "types": "private_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get(f"{SLACK_API_BASE}/conversations.list",
                           headers={"Authorization": f"Bearer {sec['bot_token']}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            if ch.get("is_member"):
                all_channels.append(ch["id"])
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    total_stored, results = 0, []
    for ch_id in all_channels:
        bf_cursor, stored, ok = "", 0, True
        while True:
            bf = backfill_channel(team_id=team_id, channel_id=ch_id, request=request,
                                   limit=200, cursor=bf_cursor if bf_cursor else None)
            if not bf.get("ok"):
                ok = False
                break
            stored += bf.get("stored_new", 0)
            if not bf.get("has_more"):
                break
            bf_cursor = bf.get("next_cursor", "")
        results.append({"channel": ch_id, "ok": ok, "stored": stored})
        if ok:
            total_stored += stored
    return {"ok": True, "total_stored": total_stored, "results": results}


# ── SLACK EVENTS WEBHOOK ──────────────────────────────────────────────────────

@router.post("/slack/events")
@router.post("/api/slack/events")
async def slack_events(request: Request):
    raw_body = await request.body()

    if len(raw_body) > MAX_BODY_BYTES:
        logger.warning("Slack event payload too large", extra={"size_bytes": len(raw_body)})
        return JSONResponse({"ok": False, "error": "payload_too_large"}, status_code=413)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("Slack event: invalid JSON body")
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        logger.warning("Slack event: invalid signature")
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)

    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    event = payload.get("event") or {}
    if event.get("type") != "message":
        return JSONResponse({"ok": True})
    if event.get("bot_id") or event.get("subtype") in {"message_changed", "message_deleted"}:
        return JSONResponse({"ok": True})

    team_id    = payload.get("team_id")
    channel_id = event.get("channel")
    ts_msg     = event.get("ts")
    if not team_id or not channel_id or not ts_msg:
        return JSONResponse({"ok": True})

    uid = event.get("user")
    event_username = ""
    if uid:
        try:
            sec = read_secret(secret_name(team_id))
            if sec and not sec.get("_error") and sec.get("bot_token"):
                event_username = resolve_username_for_message(team_id, uid, sec["bot_token"])
        except Exception:
            pass

    item = {
        "pk": f"{team_id}#{channel_id}", "sk": str(ts_msg),
        "team_id": team_id, "channel_id": channel_id, "ts": str(ts_msg),
        "user_id": uid, "username": event_username, "text": event.get("text", ""),
        "thread_ts": event.get("thread_ts"), "subtype": event.get("subtype"),
        "type": event.get("type"), "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    from app.db import get_conn
    with get_conn() as conn:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO messages
                (pk,sk,team_id,channel_id,ts,user_id,username,text,thread_ts,subtype,type,fetched_at)
                VALUES(:pk,:sk,:team_id,:channel_id,:ts,:user_id,:username,:text,:thread_ts,:subtype,:type,:fetched_at)
            """, item)
        except Exception as e:
            logger.error("DB insert failed for event", extra={
                "team_id": team_id, "ts": ts_msg, "error": str(e),
            })
            raise
    logger.info("Slack event stored", extra={
        "team_id": team_id, "channel_id": channel_id, "ts": ts_msg, "user_id": uid,
    })
    return JSONResponse({"ok": True})


# ── DB MESSAGES ───────────────────────────────────────────────────────────────

@router.get("/db-messages")
@router.get("/api/db-messages")
def db_messages(team_id: str, channel_id: str, request: Request, limit: int = 50, response: Response = None):
    if response is not None:
        no_cache(response)
    require_team_access(request, team_id)
    from app.db import get_conn
    try:
        with get_conn() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT * FROM messages WHERE pk=? ORDER BY sk DESC LIMIT ?",
                (f"{team_id}#{channel_id}", limit)
            ).fetchall()]
        return {"ok": True, "count": len(items), "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── SEARCH ────────────────────────────────────────────────────────────────────

@router.get("/api/search")
def api_search(
    team_id:    str       = Query(...),
    channel_id: str       = Query(...),
    q:          str | None = Query(None),
    from_date:  str | None = Query(None, alias="from"),
    to_date:    str | None = Query(None, alias="to"),
    user_id:    str | None = Query(None),
    username:   str | None = Query(None),
    limit:      int        = Query(200, ge=1, le=1000),
    top_k:      int        = Query(10, ge=1, le=12),
    request:    Request    = None,
    response:   Response   = None,
):
    if response is not None:
        no_cache(response)
    require_team_access(request, team_id)
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "'from' must be before 'to'")
    sec       = read_secret(secret_name(team_id))
    bot_token = (sec or {}).get("bot_token") if sec and not sec.get("_error") else None
    request_id = str(uuid.uuid4())[:8]
    try:
        messages = retrieve_messages(team_id, channel_id, q, from_date, to_date, user_id, limit, top_k,
                                     username=username, bot_token=bot_token)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not messages:
        note = f"No messages found from user '{username}'." if username else "No messages found."
        return {"ok": True, "request_id": request_id, "query": q, "count": 0, "messages": [], "note": note}
    return {"ok": True, "request_id": request_id, "query": q,
            "filters": {"from": from_date, "to": to_date, "user_id": user_id, "username": username},
            "count": len(messages), "messages": messages}


@router.get("/api/search/multi")
def api_search_multi(
    team_id:     str       = Query(...),
    channel_ids: str       = Query(...),
    q:           str | None = Query(None),
    from_date:   str | None = Query(None, alias="from"),
    to_date:     str | None = Query(None, alias="to"),
    user_id:     str | None = Query(None),
    username:    str | None = Query(None),
    limit:       int        = Query(200, ge=1, le=1000),
    top_k:       int        = Query(10, ge=1, le=50),
    request:     Request    = None,
    response:    Response   = None,
):
    if response is not None:
        no_cache(response)
    require_team_access(request, team_id)
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "'from' must be before 'to'")
    ids = [c.strip() for c in channel_ids.split(",") if c.strip()]
    if not ids:
        raise HTTPException(400, "channel_ids must be non-empty")
    sec       = read_secret(secret_name(team_id))
    bot_token = (sec or {}).get("bot_token") if sec and not sec.get("_error") else None
    request_id = str(uuid.uuid4())[:8]
    messages   = retrieve_messages_multi(team_id, ids, q, from_date, to_date, user_id, limit, top_k,
                                         username=username, bot_token=bot_token)
    if not messages:
        note = f"No messages found from user '{username}'." if username else "No messages found."
        return {"ok": True, "request_id": request_id, "query": q, "count": 0,
                "messages": [], "channels_searched": len(ids), "note": note}
    return {"ok": True, "request_id": request_id, "query": q,
            "filters": {"from": from_date, "to": to_date, "user_id": user_id, "username": username},
            "channels_searched": len(ids), "count": len(messages), "messages": messages}


# ── CHAT ──────────────────────────────────────────────────────────────────────

@router.post("/api/chat")
def api_chat(body: ChatRequest, request: Request, response: Response):
    no_cache(response)
    request_id = str(uuid.uuid4())[:8]
    require_team_access(request, body.team_id)
    if not body.question.strip():
        raise HTTPException(400, "question cannot be empty")

    logger.info("Chat request started", extra={
        "request_id": request_id, "team_id": body.team_id,
        "channel_id": body.channel_id, "question_len": len(body.question), "top_k": body.top_k,
    })

    sec       = read_secret(secret_name(body.team_id))
    bot_token = (sec or {}).get("bot_token") if sec and not sec.get("_error") else None
    active_username = extract_username_from_question(body.question)

    try:
        messages = retrieve_messages(body.team_id, body.channel_id, body.question,
                                      body.from_date, body.to_date, body.user_id, 200, min(body.top_k, 12),
                                      username=active_username, bot_token=bot_token)
    except RuntimeError as e:
        logger.error("Message retrieval failed", extra={"request_id": request_id, "error": str(e)})
        raise HTTPException(500, str(e))

    if not messages:
        note = (f"No relevant messages found from '{active_username}'."
                if active_username else "No relevant messages found in this channel.")
        logger.info("Chat: no messages found", extra={"request_id": request_id, "username": active_username})
        return {"ok": True, "request_id": request_id, "answer": note, "citations": [],
                "resolved_username": active_username}

    context, ctx_count = _build_context(messages, channel_prefix=False)
    logger.info("Context built", extra={"request_id": request_id,
                "ctx_messages": ctx_count, "ctx_chars": len(context)})

    system_prompt = (
        "You are a precise assistant answering questions ONLY from the Slack messages provided.\n"
        "Rules:\n"
        "1. Read each message IN FULL — important content often appears at the END of a message.\n"
        "2. If the answer is not present say: I couldn't find that in the available messages.\n"
        "3. Never use outside knowledge or guess.\n"
        "4. Cite message numbers like [1] or [2] for every claim.\n"
        "5. Be concise and direct.\n"
        "6. CRITICAL: sender name is between | and : in each line. When asked WHO, start your answer with their name (e.g. stuti said...).\n"
        "Output format:\n"
        "Answer: <direct answer>\n"
        "Key points: <bullets or None>\n"
        "Action items: <list or None>\n"
        "Citations: <[1], [2] etc>"
    )
    augmented_q = _augment_question_with_senders(body.question, messages)
    user_prompt  = f"SLACK MESSAGES:\n{context}\n\nQUESTION: {augmented_q}"
    answer_text  = _groq_complete(user_prompt, MAX_TOKENS_SINGLE, system=system_prompt)
    cited_indices = [int(n)-1 for n in re.findall(r"\[(\d+)\]", answer_text)
                     if n.isdigit() and 0 < int(n) <= len(messages)]
    citations     = [messages[i] for i in dict.fromkeys(cited_indices)]

    logger.info("Chat request completed", extra={
        "request_id": request_id, "retrieved_count": len(messages),
        "citations_count": len(citations), "is_fallback": answer_text.startswith("⚠️"),
    })
    return {"ok": True, "request_id": request_id, "question": body.question, "answer": answer_text,
            "citations": citations, "retrieved_count": len(messages),
            "resolved_username": active_username}


@router.post("/api/chat/multi")
def api_chat_multi(body: MultiChatRequest, request: Request, response: Response):
    no_cache(response)
    request_id = str(uuid.uuid4())[:8]
    require_team_access(request, body.team_id)
    if not body.question.strip():
        raise HTTPException(400, "question cannot be empty")
    if not body.channel_ids:
        raise HTTPException(400, "channel_ids must be non-empty")

    logger.info("Multi-chat request started", extra={
        "request_id": request_id, "team_id": body.team_id,
        "channel_count": len(body.channel_ids), "question_len": len(body.question), "top_k": body.top_k,
    })

    sec       = read_secret(secret_name(body.team_id))
    bot_token = (sec or {}).get("bot_token") if sec and not sec.get("_error") else None
    active_username = extract_username_from_question(body.question)

    messages = retrieve_messages_multi(body.team_id, body.channel_ids, body.question,
                                        body.from_date, body.to_date, body.user_id, 200, min(body.top_k, 20),
                                        username=active_username, bot_token=bot_token)
    if not messages:
        note = (f"No relevant messages found from '{active_username}'."
                if active_username else "No relevant messages found across selected channels.")
        logger.info("Multi-chat: no messages found", extra={"request_id": request_id, "username": active_username})
        return {"ok": True, "request_id": request_id, "answer": note, "citations": [],
                "channels_searched": len(body.channel_ids), "resolved_username": active_username}

    context, ctx_count = _build_context(messages, channel_prefix=True)
    logger.info("Multi context built", extra={"request_id": request_id,
                "ctx_messages": ctx_count, "ctx_chars": len(context)})

    system_prompt = (
        f"You are a precise assistant answering questions ONLY from Slack messages across {len(body.channel_ids)} channels.\n"
        "Rules:\n"
        "1. Read each message IN FULL — important content often appears at the END of a message.\n"
        "2. If the answer is not present say: I couldn't find that in the available messages.\n"
        "3. Never use outside knowledge or guess.\n"
        "4. Cite message numbers like [1] or [2] for every claim.\n"
        "5. Note the channel when relevant.\n"
        "6. CRITICAL: sender name is between | and : in each line. When asked WHO, start your answer with their name (e.g. stuti said...).\n"
        "Output format:\n"
        "Answer: <direct answer>\n"
        "Key points: <bullets or None>\n"
        "Action items: <list or None>\n"
        "Citations: <[1], [2] etc>"
    )
    augmented_q = _augment_question_with_senders(body.question, messages)
    user_prompt  = f"SLACK MESSAGES:\n{context}\n\nQUESTION: {augmented_q}"
    answer_text  = _groq_complete(user_prompt, MAX_TOKENS_MULTI, system=system_prompt)
    cited_indices = [int(n)-1 for n in re.findall(r"\[(\d+)\]", answer_text)
                     if n.isdigit() and 0 < int(n) <= len(messages)]
    citations     = [messages[i] for i in dict.fromkeys(cited_indices)]

    logger.info("Multi-chat request completed", extra={
        "request_id": request_id, "retrieved_count": len(messages),
        "channels_searched": len(body.channel_ids), "citations_count": len(citations),
        "is_fallback": answer_text.startswith("⚠️"),
    })
    return {"ok": True, "request_id": request_id, "question": body.question, "answer": answer_text,
            "citations": citations, "retrieved_count": len(messages),
            "channels_searched": len(body.channel_ids), "resolved_username": active_username}
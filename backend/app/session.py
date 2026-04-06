import uuid
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request, Response

from app.constants import SESSION_COOKIE_NAME, SESSION_TTL_HOURS, IS_PROD
from app.logger import logger
from app.utils import sessions_table


# ── INTERNAL GUARD ────────────────────────────────────────────────────────────

def _require_sessions_table():
    if sessions_table is None:
        raise HTTPException(500, "SESSIONS_TABLE not configured")


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_session() -> str:
    _require_sessions_table()
    session_id = str(uuid.uuid4())
    expires_at = int((datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)).timestamp())
    sessions_table.put_item(Item={
        "session_id": session_id,
        "team_ids":   [],
        "created_at": datetime.utcnow().isoformat() + "Z",
        "expires_at": expires_at,
    })
    logger.info(f"[session] created {session_id}")
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    if not session_id or sessions_table is None:
        return None
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if not item:
            return None
        if item.get("expires_at", 0) < int(time.time()):
            return None
        return item
    except Exception as e:
        logger.warning(f"[session] get error: {e}")
        return None


def bind_team_to_session(session_id: str, team_id: str) -> None:
    _require_sessions_table()
    sess = get_session(session_id)
    if not sess:
        return
    current = sess.get("team_ids", [])
    if team_id not in current:
        current.append(team_id)
    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="SET team_ids = :tids",
        ExpressionAttributeValues={":tids": current},
    )
    logger.info(f"[session] bound team {team_id} to session {session_id}")


def unbind_team_from_session(session_id: str, team_id: str) -> None:
    if not session_id or sessions_table is None:
        return
    sess = get_session(session_id)
    if not sess:
        return
    updated = [t for t in sess.get("team_ids", []) if t != team_id]
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET team_ids = :tids",
            ExpressionAttributeValues={":tids": updated},
        )
    except Exception as e:
        logger.warning(f"[session] unbind error: {e}")


# ── COOKIE HELPERS ────────────────────────────────────────────────────────────

def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key      = SESSION_COOKIE_NAME,
        value    = session_id,
        httponly = True,
        secure   = IS_PROD,
        samesite = "lax",
        max_age  = SESSION_TTL_HOURS * 3600,
        path     = "/",
    )


def get_or_create_session(request: Request, response: Response) -> tuple[str, dict]:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    sess       = get_session(cookie_val) if cookie_val else None
    if not sess:
        session_id = create_session()
        sess       = get_session(session_id) or {}
        _set_session_cookie(response, session_id)
        return session_id, sess
    return cookie_val, sess


# ── AUTH GUARDS ───────────────────────────────────────────────────────────────

def require_session(request: Request) -> dict:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_val:
        raise HTTPException(401, "No session — connect a Slack workspace first")
    sess = get_session(cookie_val)
    if not sess:
        raise HTTPException(401, "Session expired — please reconnect")
    return sess


def require_team_access(request: Request, team_id: str) -> dict:
    sess    = require_session(request)
    allowed = sess.get("team_ids", [])
    if team_id not in allowed:
        logger.warning(f"[auth] DENIED team={team_id} session={sess.get('session_id')} allowed={allowed}")
        raise HTTPException(403, "Access denied — this workspace does not belong to your session")
    return sess
import uuid
import json
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request, Response

from app.constants import SESSION_COOKIE_NAME, SESSION_TTL_HOURS, IS_PROD
from app.logger import logger
from app.db import get_conn


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_session() -> str:
    session_id = str(uuid.uuid4())
    expires_at = int((datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)).timestamp())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions(session_id, team_ids, created_at, expires_at) VALUES(?, ?, ?, ?)",
            (session_id, "[]", datetime.utcnow().isoformat() + "Z", expires_at),
        )
    logger.info(f"[session] created {session_id}")
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] < int(time.time()):
            return None
        d = dict(row)
        d["team_ids"] = json.loads(d["team_ids"] or "[]")
        return d
    except Exception as e:
        logger.warning(f"[session] get error: {e}")
        return None


def bind_team_to_session(session_id: str, team_id: str) -> None:
    sess = get_session(session_id)
    if not sess:
        return
    current = sess.get("team_ids", [])
    if team_id not in current:
        current.append(team_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET team_ids = ? WHERE session_id = ?",
            (json.dumps(current), session_id),
        )
    logger.info(f"[session] bound team {team_id} to session {session_id}")


def unbind_team_from_session(session_id: str, team_id: str) -> None:
    if not session_id:
        return
    sess = get_session(session_id)
    if not sess:
        return
    updated = [t for t in sess.get("team_ids", []) if t != team_id]
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET team_ids = ? WHERE session_id = ?",
                (json.dumps(updated), session_id),
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
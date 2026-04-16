# app/db.py  — PostgreSQL backend (Supabase / Neon / any Postgres)
import contextlib
import psycopg2
import psycopg2.extras   # RealDictCursor
from app.constants import DATABASE_URL


class _ConnWrapper:
    """
    Thin wrapper around a psycopg2 connection that exposes the same
    .execute() / .executescript() interface the rest of the code expects.
    Row results behave like dicts (RealDictRow).
    """

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql: str, params=None):
        cur = self._conn.cursor()
        cur.execute(sql, params or ())
        return cur           # caller can call .fetchall() / .fetchone() on it

    def executescript(self, script: str):
        """Execute multiple semicolon-separated statements (used only in init_db)."""
        cur = self._conn.cursor()
        cur.execute(script)
        return cur

    # forward attribute access (commit, rollback, close …)
    def __getattr__(self, name):
        return getattr(self._conn, name)


def _connect() -> _ConnWrapper:
    raw = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    raw.autocommit = False
    return _ConnWrapper(raw)


@contextlib.contextmanager
def get_conn():
    """Context manager — yields a wrapped connection, commits on exit."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                pk          TEXT NOT NULL,
                sk          TEXT NOT NULL,
                team_id     TEXT,
                channel_id  TEXT,
                ts          TEXT,
                user_id     TEXT,
                username    TEXT,
                text        TEXT,
                thread_ts   TEXT,
                reply_count INTEGER DEFAULT 0,
                subtype     TEXT,
                type        TEXT,
                fetched_at  TEXT,
                PRIMARY KEY (pk, sk)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_pk ON messages(pk);
            CREATE INDEX IF NOT EXISTS idx_messages_sk ON messages(sk);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                team_ids    TEXT DEFAULT '[]',
                created_at  TEXT,
                expires_at  BIGINT
            );

            CREATE TABLE IF NOT EXISTS workspace_tokens (
                team_id     TEXT PRIMARY KEY,
                team_name   TEXT,
                bot_user_id TEXT,
                bot_token   TEXT,
                scope       TEXT,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS user_cache (
                pk           TEXT NOT NULL,
                sk           TEXT NOT NULL,
                user_id      TEXT,
                display_name TEXT,
                real_name    TEXT,
                cached_at    TEXT,
                PRIMARY KEY (pk, sk)
            );
        """)
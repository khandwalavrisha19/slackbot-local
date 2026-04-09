# app/db.py
import sqlite3
import os
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "slackbot.db"))

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            pk          TEXT NOT NULL,   -- "{team_id}#{channel_id}"
            sk          TEXT NOT NULL,   -- unix timestamp string (Slack ts)
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
            team_ids    TEXT DEFAULT '[]',  -- JSON array
            created_at  TEXT,
            expires_at  INTEGER
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
            pk           TEXT NOT NULL,   -- "{team_id}#__users__"
            sk           TEXT NOT NULL,   -- user_id
            user_id      TEXT,
            display_name TEXT,
            real_name    TEXT,
            cached_at    TEXT,
            PRIMARY KEY (pk, sk)
        );
        """)
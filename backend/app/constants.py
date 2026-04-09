import os
from pathlib import Path

# ── SLACK BASE URLS ───────────────────────────────────────────────────────────
# All Web API calls  → https://slack.com/api/<method>
# OAuth authorize    → https://slack.com/oauth/v2/authorize  (different path root)
SLACK_API_BASE   = "https://slack.com/api"
SLACK_OAUTH_BASE = "https://slack.com/oauth/v2"

# ── REQUEST SIZE LIMITS ───────────────────────────────────────────────────────
MAX_BODY_BYTES   = 64 * 1024   # 64 KB hard limit for all POST bodies
MAX_QUESTION_LEN = 1_000       # chars
MAX_CHANNEL_IDS  = 20          # max channels in multi-chat/search

# ── ENV CONFIG ────────────────────────────────────────────────────────────────

CLIENT_ID            = os.getenv("SLACK_CLIENT_ID", "").strip()
CLIENT_SECRET        = os.getenv("SLACK_CLIENT_SECRET", "").strip()
REDIRECT_URI         = os.getenv("SLACK_REDIRECT_URI", "").strip()
SLACK_SCOPES         = os.getenv(
    "SLACK_SCOPES",
    "channels:history,chat:write,users:read,groups:history,channels:read,groups:read,channels:join",
).strip()
CORS_ORIGINS         = os.getenv("CORS_ORIGINS", "*")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
DB_PATH = os.getenv("DB_PATH", "slackbot.db").strip()
GROQ_API_KEY         = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL           = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_URL             = "https://api.groq.com/openai/v1/chat/completions"
UI_BASE_URL          = os.getenv("UI_BASE_URL", "").rstrip("/")
SESSION_COOKIE_NAME  = "sb_session"
SESSION_TTL_HOURS    = 72
IS_PROD              = os.getenv("ENV", "dev").strip().lower() == "prod"

# ── FRONTEND ──────────────────────────────────────────────────────────────────
_frontend_default = Path(__file__).parent.parent / "frontend" / "index.html"
FRONTEND_PATH     = Path(os.getenv("FRONTEND_PATH", str(_frontend_default)))

# ── GROQ TIMEOUTS & TOKEN LIMITS ─────────────────────────────────────────────
GROQ_TIMEOUT_CONNECT = 5    # seconds to establish connection
GROQ_TIMEOUT_READ    = 30   # seconds to read response
CONTEXT_MAX_CHARS    = 8_000
MAX_TOKENS_SINGLE    = 768
MAX_TOKENS_MULTI     = 900

# ── CORS ORIGINS (parsed) ─────────────────────────────────────────────────────
PARSED_CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()] or ["*"]
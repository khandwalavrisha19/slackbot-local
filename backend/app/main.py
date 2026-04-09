"""
main.py  —  Entry point for Render / uvicorn.

Module layout
─────────────
  constants.py    → all env vars, limits, config values
  logger.py       → StructuredLogger (JSON structured logs)
  exceptions.py   → request-size middleware + global exception handlers
  utils.py        → secret helpers (SQLite-backed), Slack sig verify,
                    validators, user-cache, token masking
  session.py      → SQLite session CRUD + auth guards
  groq_client.py  → Groq LLM wrapper
  retrieval.py    → SQLite message fetch, scoring, context building
  models.py       → Pydantic request schemas (ChatRequest, MultiChatRequest)
  routes.py       → All FastAPI route handlers (APIRouter)
  main.py         → App factory, middleware/CORS wiring
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.constants import PARSED_CORS_ORIGINS, FRONTEND_PATH
from app.exceptions import register_exception_handlers
from app.routes import router
from app.db import init_db

init_db()   # creates tables on first boot

app = FastAPI(title="Slackbot")
register_exception_handlers(app)
app.add_middleware(CORSMiddleware,
    allow_origins=PARSED_CORS_ORIGINS if PARSED_CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)

# Serve static files (CSS/JS) alongside index.html
if FRONTEND_PATH.parent.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_PATH.parent)), name="static")
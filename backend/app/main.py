"""
main.py  —  Entry point (deploy this file only).

Module layout
─────────────
  constants.py    → all env vars, limits, config values
  logger.py       → StructuredLogger (JSON → CloudWatch)
  exceptions.py   → request-size middleware + global exception handlers
  utils.py        → AWS clients, secret helpers, Slack sig verify,
                    validators, user-cache, token masking
  session.py      → DynamoDB session CRUD + auth guards
  groq_client.py  → Groq LLM wrapper
  retrieval.py    → DynamoDB message fetch, scoring, context building
  models.py       → Pydantic request schemas (ChatRequest, MultiChatRequest)
  routes.py       → All FastAPI route handlers (APIRouter)
  main.py         → App factory, middleware/CORS wiring, Mangum handler
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from app.constants import PARSED_CORS_ORIGINS
from app.exceptions import register_exception_handlers
from app.routes import router

# ── APP FACTORY ───────────────────────────────────────────────────────────────
app = FastAPI(title="Slackbot Full MVP")

# Order matters: register middleware + handlers before including router
register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = PARSED_CORS_ORIGINS if PARSED_CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

app.include_router(router)

# ── AWS LAMBDA HANDLER ────────────────────────────────────────────────────────
handler = Mangum(app)
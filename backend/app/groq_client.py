import uuid
import time
from typing import Optional

import requests
from fastapi import HTTPException

from app.constants import GROQ_API_KEY, GROQ_MODEL, GROQ_URL, GROQ_TIMEOUT_CONNECT, GROQ_TIMEOUT_READ
from app.logger import logger


def _groq_complete(prompt: str, max_tokens: int = 1024, system: Optional[str] = None) -> str:
    """
    Call Groq API with explicit connect + read timeouts.
    Accepts an optional system prompt for grounding rules.
    Returns a safe fallback message instead of raising on timeout/5xx.
    """
    request_id = str(uuid.uuid4())[:8]
    if not GROQ_API_KEY:
        logger.error("Groq API key missing", extra={"request_id": request_id})
        raise HTTPException(500, "GROQ_API_KEY not set")

    messages_payload: list[dict] = []
    if system:
        messages_payload.append({"role": "system", "content": system})
    messages_payload.append({"role": "user", "content": prompt})

    payload = {
        "model":       GROQ_MODEL,
        "messages":    messages_payload,
        "temperature": 0.2,
        "max_tokens":  max_tokens,
    }

    start = time.time()
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=(GROQ_TIMEOUT_CONNECT, GROQ_TIMEOUT_READ),
        )
    except requests.exceptions.ConnectTimeout:
        elapsed = round(time.time() - start, 2)
        logger.error("Groq connect timeout", extra={"request_id": request_id, "elapsed_s": elapsed})
        return "⚠️ The AI service took too long to connect. Please try again in a moment."
    except requests.exceptions.ReadTimeout:
        elapsed = round(time.time() - start, 2)
        logger.error("Groq read timeout", extra={"request_id": request_id, "elapsed_s": elapsed})
        return "⚠️ The AI service timed out while generating a response. Try a shorter question or smaller date range."
    except requests.exceptions.RequestException as exc:
        logger.error("Groq network error", extra={"request_id": request_id, "error": str(exc)})
        return "⚠️ Could not reach the AI service due to a network error. Please try again."

    elapsed = round(time.time() - start, 2)

    try:
        data = resp.json()
    except ValueError:
        logger.error("Groq non-JSON response", extra={"request_id": request_id, "status": resp.status_code})
        return "⚠️ Received an unexpected response from the AI service."

    if resp.status_code == 429:
        logger.warning("Groq rate limited", extra={"request_id": request_id})
        return "⚠️ The AI service is currently rate-limited. Please wait a few seconds and try again."

    if resp.status_code >= 500:
        logger.error("Groq 5xx error", extra={"request_id": request_id, "status": resp.status_code})
        return "⚠️ The AI service returned a server error. Please try again shortly."

    if resp.status_code != 200:
        logger.error("Groq unexpected status", extra={
            "request_id": request_id, "status": resp.status_code, "body": str(data)[:200],
        })
        raise HTTPException(502, f"Groq error {resp.status_code}: {data}")

    answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    logger.info("Groq call succeeded", extra={
        "request_id": request_id,
        "elapsed_s":  elapsed,
        "tokens":     data.get("usage", {}).get("total_tokens"),
    })
    return answer
import re
from typing import Optional

from app.utils import _date_to_sk, _ts_human, resolve_user_id
from app.constants import CONTEXT_MAX_CHARS
from app.logger import logger


# ── RECENCY / KEYWORD HELPERS ─────────────────────────────────────────────────

_RECENCY_WORDS = frozenset([
    # temporal / recency
    "last", "latest", "recent", "newest", "today", "yesterday",
    "just", "now", "current", "recently", "new",
    "next", "week", "soon", "tomorrow", "upcoming", "future",
    # question words
    "what", "who", "whose", "whom", "where", "when", "why", "how",
    # stop words
    "about", "said", "say", "says", "did", "does", "from",
    "the", "and", "for", "with", "its", "this", "that", "tell",
])


def _is_recency_query(q: str) -> bool:
    words = set(re.findall(r"\w+", q.lower()))
    return bool(words & _RECENCY_WORDS)


def _content_keywords(q: str) -> list[str]:
    return [w for w in re.findall(r"\w+", q.lower())
            if w not in _RECENCY_WORDS and len(w) > 2]


# ── SCORING ───────────────────────────────────────────────────────────────────

def _score_messages(items: list[dict], q: str) -> list[dict]:
    keywords = _content_keywords(q)

    if not keywords:
        return [i for i in items
                if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]

    scored = []
    for item in items:
        text = (item.get("text") or "").lower()
        if re.search(r"<@\w+> has (joined|left)", text):
            continue
        score  = sum(text.count(kw) for kw in keywords)
        score += sum(2 for kw in keywords if kw in text[:80])
        if len(keywords) > 1 and " ".join(keywords) in text:
            score += 5
        if len(text) > 800:
            score = score * 800 / len(text)
        if len(text) < 20:
            score *= 0.5
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    result = [item for score, item in scored if score > 0]

    if _is_recency_query(q) and result:
        result.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)

    return result


# ── FORMATTING ────────────────────────────────────────────────────────────────

def _format_messages(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        text = (item.get("text") or "").strip()
        out.append({
            "message_ts":      item.get("ts") or item.get("sk", ""),
            "user_id":         item.get("user_id", "unknown"),
            "username":        item.get("username", ""),
            "text":            text,
            "snippet":         text[:1200] + ("…" if len(text) > 1200 else ""),
            "channel_id":      item.get("channel_id", ""),
            "team_id":         item.get("team_id", ""),
            "timestamp_human": _ts_human(item.get("ts") or item.get("sk", "")),
        })
    return out


# ── RETRIEVAL ─────────────────────────────────────────────────────────────────

def retrieve_messages(team_id, channel_id, q=None, from_date=None, to_date=None,
                      user_id=None, limit=200, top_k=10, username=None, bot_token=None):
    from app.utils import resolve_user_id  # avoid circular at top level
    if username and not user_id and bot_token:
        resolved = resolve_user_id(team_id, username, bot_token)
        if resolved: user_id = resolved
        else: return []

    pk = f"{team_id}#{channel_id}"
    sql = "SELECT * FROM messages WHERE pk=%s"
    params: list = [pk]
    if from_date: sql += " AND sk >= %s"; params.append(_date_to_sk(from_date))
    if to_date:   sql += " AND sk <= %s"; params.append(_date_to_sk(to_date, end_of_day=True))
    if user_id:   sql += " AND user_id = %s"; params.append(user_id)
    sql += " ORDER BY sk DESC LIMIT %s"
    params.append(limit)

    from app.db import get_conn
    with get_conn() as conn:
        items = [dict(r) for r in conn.execute(sql, params).fetchall()]

    items = [i for i in items if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]
    if not q or not q.strip(): return _format_messages(items[:top_k])
    if not _content_keywords(q): return _format_messages(items[:top_k])
    if user_id: return _format_messages(items[:top_k])
    return _format_messages(_score_messages(items, q)[:top_k])
def retrieve_messages_multi(
    team_id: str, channel_ids: list[str],
    q: Optional[str] = None, from_date: Optional[str] = None,
    to_date: Optional[str] = None, user_id: Optional[str] = None,
    limit: int = 200, top_k: int = 10,
    username: Optional[str] = None, bot_token: Optional[str] = None,
) -> list[dict]:
    if username and not user_id and bot_token:
        resolved = resolve_user_id(team_id, username, bot_token)
        if resolved:
            user_id = resolved
        else:
            logger.info(f"[retrieve_multi] username '{username}' not found in workspace {team_id}")
            return []

    from app.db import get_conn
    all_raw: list[dict] = []
    for channel_id in channel_ids:
        pk = f"{team_id}#{channel_id}"
        sql = "SELECT * FROM messages WHERE pk=%s"
        params: list = [pk]
        if from_date: sql += " AND sk >= %s"; params.append(_date_to_sk(from_date))
        if to_date:   sql += " AND sk <= %s"; params.append(_date_to_sk(to_date, end_of_day=True))
        if user_id:   sql += " AND user_id = %s"; params.append(user_id)
        sql += " ORDER BY sk DESC LIMIT %s"
        params.append(limit)

        try:
            with get_conn() as conn:
                items = [dict(r) for r in conn.execute(sql, params).fetchall()]
            items = [i for i in items if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]
            all_raw.extend(items)
        except Exception as e:
            logger.warning(f"[retrieve_multi] DB query failed for {channel_id}: {e}")

    if not all_raw:
        return []

    if not q or not q.strip():
        all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        return _format_messages(all_raw[:top_k])
    if not _content_keywords(q):
        all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        return _format_messages(all_raw[:top_k])
    if user_id:
        all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        return _format_messages(all_raw[:top_k])
    return _format_messages(_score_messages(all_raw, q)[:top_k])


# ── CONTEXT / PROMPT BUILDERS ─────────────────────────────────────────────────

def _build_context(messages: list[dict], channel_prefix: bool = False) -> tuple[str, int]:
    """
    Build the LLM context string from retrieved messages.
    Returns (context_string, messages_included_count).
    """
    lines: list[str] = []
    total = 0
    for i, m in enumerate(messages):
        text = (m.get("text") or "").strip()
        who  = m.get("username") or m.get("user_id") or "unknown"
        ch   = f" | #{m.get('channel_id','')}" if channel_prefix and m.get("channel_id") else ""
        line = f"[{i+1}] {m.get('timestamp_human','')} | {who}{ch}: {text}"
        if total + len(line) > CONTEXT_MAX_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines), len(lines)


def _augment_question_with_senders(question: str, messages: list[dict]) -> str:
    """
    If the question asks WHO, inject the sender names directly into the question
    so the LLM cannot miss them.
    """
    who_words = {"who", "whose", "whom"}
    if not (set(question.lower().split()) & who_words):
        return question

    senders, seen = [], set()
    for m in messages:
        name = (m.get("username") or m.get("user_id") or "").strip()
        if name and name not in seen:
            senders.append(name)
            seen.add(name)

    if not senders:
        return question

    sender_str = ", ".join(senders)
    return f"{question} [NOTE: The message(s) were sent by: {sender_str}. You MUST name them in your answer.]"
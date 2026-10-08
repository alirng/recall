"""Condense a chat into what matters for memory: the person's words, and how each
exchange ended. Assistant turns are cut to their opening and closing lines, which is
where agents state what they will do and what they did. This keeps a typical chat
to a few thousand tokens, so extraction is cheap enough to run locally.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

USER_CHARS = 2000
ASSISTANT_HEAD, ASSISTANT_TAIL = 500, 1000
CHUNK_CHARS = 24_000  # ~6k tokens per extraction call


@dataclass
class Digest:
    session_id: str
    chunks: list[str]
    hash: str


def _squash(text: str) -> str:
    return " ".join(text.split())


def _trim(text: str, limit: int) -> str:
    text = _squash(text)
    return text if len(text) <= limit else text[:limit] + f" …[+{len(text) - limit} chars]"


def _assistant(text: str) -> str:
    text = _squash(text)
    if len(text) <= ASSISTANT_HEAD + ASSISTANT_TAIL + 20:
        return text
    return f"{text[:ASSISTANT_HEAD]} … {text[-ASSISTANT_TAIL:]}"


def build(session, messages) -> Digest | None:
    """session: a sessions row; messages: (seq, role, text) rows in order."""
    turns = []
    n_user = 0
    for _seq, role, text in messages:
        if not text.strip():
            continue
        if role == "user":
            n_user += 1
            turns.append(f"[U{n_user}] {_trim(text, USER_CHARS)}")
        else:
            turns.append(f"[A{n_user}] {_assistant(text)}")
    user_chars = sum(len(t) for t in turns if t.startswith("[U"))
    if n_user == 0 or user_chars < 60:
        return None
    date = datetime.fromtimestamp(session["updated_at"] or 0).strftime("%Y-%m-%d")
    header = (
        f"Chat {session['id']} | agent: {session['agent']} | date: {date}\n"
        f"Project: {session['project_name'] or 'none (scratch folder; only global memories apply)'}"
        f"{' (' + session['project_key'] + ')' if session['project_key'] else ''}\n"
        f"Title: {session['title']}\n"
    )
    chunks, current = [], []
    size = 0
    for t in turns:
        if current and size + len(t) > CHUNK_CHARS:
            chunks.append(header + "\n".join(current))
            current, size = [], 0
        current.append(t)
        size += len(t) + 1
    chunks.append(header + "\n".join(current))
    if len(chunks) > 1:
        chunks = [c.replace("\n", f" (part {i + 1} of {len(chunks)})\n", 1) for i, c in enumerate(chunks)]
    h = hashlib.sha1("\x00".join(chunks).encode()).hexdigest()[:16]
    return Digest(session["id"], chunks, h)

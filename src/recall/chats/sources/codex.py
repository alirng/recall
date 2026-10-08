"""Codex history: rollout JSONL files under ~/.codex/sessions, titled from the Codex app's thread catalogue."""

import glob
import os
import sqlite3

from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

ROOT = os.path.expanduser("~/.codex")
CATALOG = os.path.join(ROOT, "sqlite", "codex-dev.db")


def _catalog() -> dict[str, dict]:
    """Thread titles, cwd and branch from the Codex app's own thread catalogue."""
    try:
        con = sqlite3.connect(f"file:{CATALOG}?mode=ro", uri=True)
        rows = con.execute("select thread_id, display_title, cwd, git_branch from local_thread_catalog").fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: {"title": r[1], "cwd": r[2], "branch": r[3]} for r in rows}


def detect() -> int | None:
    if not os.path.isdir(os.path.join(ROOT, "sessions")):
        return None
    return sum(
        len(glob.glob(os.path.join(ROOT, d, "**", "*.jsonl"), recursive=True))
        for d in ("sessions", "archived_sessions")
    )


def items():
    catalog = None
    paths = glob.glob(os.path.join(ROOT, "sessions", "**", "*.jsonl"), recursive=True)
    paths += glob.glob(os.path.join(ROOT, "archived_sessions", "**", "*.jsonl"), recursive=True)
    for path in paths:
        st = os.stat(path)

        def load_one(p=path):
            nonlocal catalog
            if catalog is None:
                catalog = _catalog()
            return load(p, catalog)

        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", load_one)


def load(path: str, catalog: dict[str, dict]) -> Session | None:
    s = None
    messages: list[Message] = []
    for d in read_jsonl(path):
        payload = d.get("payload") or {}
        ts = iso_ts(d.get("timestamp"))
        if d.get("type") == "session_meta" and s is None:
            s = Session("codex", payload.get("id") or os.path.basename(path), path)
            s.project = payload.get("cwd")
            s.branch = (payload.get("git") or {}).get("branch")
            s.started_at = iso_ts(payload.get("timestamp")) or ts
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                s.origin = "subagent"
            elif source == "exec":
                s.origin = "headless"
            meta = catalog.get(s.native_id) or {}
            s.title = meta.get("title")
            s.project = meta.get("cwd") or s.project
            s.branch = meta.get("branch") or s.branch
            continue
        if d.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = text_blocks(payload.get("content"))
        text = clean_user_text(text) if role == "user" else clean_assistant_text(text)
        append(messages, role, text, ts)
        if s and ts:
            s.updated_at = ts
    if s is None:
        return None
    s.messages = messages
    return s

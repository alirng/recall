"""Crush history (Charm): one SQLite file per project, `<project>/.crush/crush.db`, found through the registry
`<data>/crush/projects.json` (`{"projects": [{"path", "data_dir", "last_accessed"}]}`); a project whose folder
is gone is skipped. `messages.parts` is a JSON array of `{type, data}`: the `text` parts are kept, tool calls,
reasoning and `finish` markers are not. A summary message (`is_summary_message`) is a compaction, not a turn.
Format knowledge: agentsview internal/parser/crush.go (MIT) and deja-vu docs/registry/crush.md (MIT).
"""

import contextlib
import json
import os
import sqlite3

from ... import roots
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, open_ro, table_names

CHAT_SCHEMA = "2026-11-03 new source: Crush"

KEY = "crush"


def _projects() -> list[tuple[str, str]]:
    """(project folder, crush.db) for each registered project whose database exists."""
    out = []
    for folder in roots.other_dirs(KEY):
        try:
            with open(os.path.join(folder, "projects.json"), encoding="utf-8") as f:
                registry = json.load(f)
        except (OSError, ValueError):
            continue
        for p in registry.get("projects") or [] if isinstance(registry, dict) else []:
            if not isinstance(p, dict) or not isinstance(p.get("path"), str):
                continue
            data_dir = p.get("data_dir") or ".crush"
            db = (
                os.path.join(p["path"], data_dir, "crush.db")
                if not os.path.isabs(data_dir)
                else os.path.join(data_dir, "crush.db")
            )
            if os.path.isfile(db) and (p["path"], db) not in out:
                out.append((p["path"], db))
    return out


def _sessions(con: sqlite3.Connection) -> list[tuple]:
    if not {"sessions", "messages"} <= table_names(con):
        return []
    return con.execute(
        "select s.id, coalesce(s.updated_at, 0), coalesce(m.n, 0), coalesce(m.t, 0) from sessions s "
        "left join (select session_id, count(*) n, max(created_at) t from messages group by 1) m "
        "on m.session_id = s.id"
    ).fetchall()


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    total = 0
    for _, db in _projects():
        if con := open_ro(db):
            with contextlib.suppress(sqlite3.Error):
                total += len(_sessions(con))
            con.close()
    return total


def items():
    for folder, db in _projects():
        con = open_ro(db)
        if con is None:
            continue
        try:
            rows = _sessions(con)
        except sqlite3.Error:
            continue
        finally:
            con.close()
        for sid, updated, n, last in rows:
            yield Item(f"{db}#{sid}", f"{updated}:{n}:{last}", lambda d=db, f=folder, i=sid: load(d, f, i))


def load(db: str, folder: str, sid: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        row = con.execute(
            "select coalesce(title, ''), coalesce(parent_session_id, ''), created_at, updated_at "
            "from sessions where id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        title, parent, created, updated = row
        s = Session(KEY, sid, f"{db}#{sid}")
        s.root, s.project = KEY, folder
        s.title = title.strip() or None
        s.started_at, s.updated_at = iso_ts(created), iso_ts(updated)  # Unix seconds, whatever the schema comment says
        if parent:  # a subagent (an agent tool call) runs in a child session
            s.origin, s.parent = "subagent", f"{KEY}:{parent}"
        summary = "is_summary_message" in {r[1] for r in con.execute("pragma table_info(messages)")}
        query = "select role, parts, created_at from messages where session_id = ?"
        if summary:
            query += " and coalesce(is_summary_message, 0) = 0"
        messages: list[Message] = []
        for role, parts, made in con.execute(query + " order by created_at, id", (sid,)):
            if role in ("user", "assistant"):
                text = "\n\n".join(_texts(parts))
                append(
                    messages,
                    role,
                    clean_user_text(text) if role == "user" else clean_assistant_text(text),
                    iso_ts(made),
                )
        s.messages = messages
        return s
    finally:
        con.close()


def _texts(parts) -> list[str]:
    try:
        parts = json.loads(parts or "[]")
    except ValueError:
        return []
    out = []
    for p in parts if isinstance(parts, list) else []:
        data = p.get("data") if isinstance(p, dict) else None
        if isinstance(data, dict) and p.get("type") == "text" and (data.get("text") or "").strip():
            out.append(data["text"].strip())
    return out


AGENTS = {KEY: ("Crush", detect, items)}

"""Goose history: one SQLite file, `<data>/goose/sessions/sessions.db` (Goose 1.10 and later), read with mode=ro.

`sessions` holds the chat (id, name, description, working_dir, created_at, updated_at, session_type,
parent_session_id) and `messages` its turns: `content_json` is a JSON array of blocks, of which only `text` is
kept, and `metadata_json` marks what the person was never shown (`userVisible: false`: injected context).
Goose before 1.10 wrote JSONL files and migrated them into this database, so they are not read separately.
Format knowledge: agentsview internal/parser/goose.go (MIT) and deja-vu docs/registry/goose.md (MIT).
"""

import json
import os
import sqlite3

from ... import roots
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, open_ro, table_names

CHAT_SCHEMA = "2026-11-03 new source: Goose"

KEY = "goose"
# session_type values that are a person's own chat; a sub_agent's is a subagent, anything else a run nobody typed
_USER = ("user", "")


def _databases() -> list[str]:
    out = []
    for folder in roots.other_dirs(KEY):
        for rel in (("sessions", "sessions.db"), ("data", "sessions", "sessions.db")):
            path = os.path.join(folder, *rel)
            if os.path.isfile(path) and path not in out:
                out.append(path)
    return out


def _sessions(con: sqlite3.Connection) -> list[tuple]:
    if not {"sessions", "messages"} <= table_names(con):
        return []
    return con.execute(
        "select s.id, coalesce(s.updated_at, ''), coalesce(m.n, 0), coalesce(m.t, 0) from sessions s "
        "left join (select session_id, count(*) n, max(created_timestamp) t from messages group by 1) m "
        "on m.session_id = s.id"
    ).fetchall()


def detect() -> int | None:
    dbs = _databases()
    if not dbs:
        return None
    total = 0
    for db in dbs:
        if con := open_ro(db):
            try:
                total += len(_sessions(con))
            except sqlite3.Error:
                pass
            finally:
                con.close()
    return total


def items():
    for db in _databases():
        con = open_ro(db)
        if con is None:
            continue
        try:
            rows = _sessions(con)
        except sqlite3.Error:  # an unknown schema, or a database busy beyond the timeout
            continue
        finally:
            con.close()
        for sid, updated, n, last in rows:
            yield Item(f"{db}#{sid}", f"{updated}:{n}:{last}", lambda d=db, i=sid: load(d, i))


def load(db: str, sid: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        row = con.execute(
            "select coalesce(name, ''), coalesce(description, ''), coalesce(working_dir, ''), created_at, updated_at, "
            "coalesce(session_type, ''), coalesce(parent_session_id, '') from sessions where id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        name, description, folder, created, updated, kind, parent = row
        s = Session(KEY, sid, f"{db}#{sid}")
        s.root = KEY
        s.project = folder or None
        s.title = (description or name).strip() or None
        s.started_at, s.updated_at = iso_ts(created), iso_ts(updated)
        if kind == "sub_agent":
            s.origin, s.parent = "subagent", f"{KEY}:{parent}" if parent else None
        elif kind not in _USER:
            s.origin = "headless"
        messages: list[Message] = []
        for role, blocks, meta, made in con.execute(
            "select role, content_json, metadata_json, created_timestamp from messages where session_id = ? "
            "order by created_timestamp, id",
            (sid,),
        ):
            if role not in ("user", "assistant") or _hidden(meta):
                continue
            text = "\n\n".join(_blocks(blocks))
            append(
                messages, role, clean_user_text(text) if role == "user" else clean_assistant_text(text), iso_ts(made)
            )
        s.messages = messages
        return s
    finally:
        con.close()


def _hidden(meta) -> bool:
    try:
        return json.loads(meta or "{}").get("userVisible") is False
    except (ValueError, AttributeError):
        return False


def _blocks(content_json) -> list[str]:
    try:
        blocks = json.loads(content_json or "[]")
    except ValueError:
        return []
    return [
        b["text"].strip()
        for b in blocks
        if isinstance(blocks, list) and isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    ]


AGENTS = {KEY: ("Goose", detect, items)}

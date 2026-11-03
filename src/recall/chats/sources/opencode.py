"""OpenCode and Kilo Code history: one SQLite file each (`opencode.db`, `kilo.db`), WAL mode, read with
mode=ro. Kilo Code 7 is a fork of OpenCode and keeps the same tables.

OpenCode has two live schema families in one file:
  v1  `session`, `message`, `part`: a message's role is in message.data, its words are the `text` parts.
  v2  `session_v2`, `session_message`: a message is one row (`user`, `assistant`, `synthetic`, `system`...),
      and an assistant row's `content` list holds `text`, `reasoning` and `tool` items.
v2 copies each v1 session into its own tables on first start and leaves the v1 rows, so a session in both is
read from v2. Format knowledge: OpenCode's own source (anomalyco/opencode, MIT) and SpecStory's
OPENCODE-FORMAT.md (Apache-2.0); see docs/agents.md.
"""

import json
import os
import re
import sqlite3

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, open_ro, table_names

CHAT_SCHEMA = "2026-11-03 new source: OpenCode and Kilo Code"

# The titles OpenCode gives a session before it has named it ("New session - 2026-10-12T10:31:44.000Z").
_PLACEHOLDER = re.compile(r"^(New session|Child session) - \d{4}-\d\d-\d\d")


def _databases(key: str, prefix: str) -> list[str]:
    out = []
    for folder in roots.other_dirs(key):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        out += [
            os.path.join(folder, n)
            for n in names
            if n.endswith(".db") and (n == f"{prefix}.db" or n.startswith(f"{prefix}-"))
        ]
    return out


def _sessions(con: sqlite3.Connection) -> dict[str, tuple[str, int, int]]:
    """session id -> (family, time_updated, messages) for every session, v2 first."""
    tables = table_names(con)
    found: dict[str, tuple[str, int, int]] = {}
    if {"session_v2", "session_message"} <= tables:
        for sid, updated, n in con.execute(
            "select s.id, max(s.time_updated, coalesce(m.t, 0)), coalesce(m.n, 0) from session_v2 s "
            "left join (select session_id, max(time_updated) t, count(*) n from session_message group by 1) m "
            "on m.session_id = s.id"
        ):
            found[sid] = ("v2", updated or 0, n)
    if {"session", "message"} <= tables:
        for sid, updated, n in con.execute(
            "select s.id, max(s.time_updated, coalesce(m.t, 0)), coalesce(m.n, 0) from session s "
            "left join (select session_id, max(time_updated) t, count(*) n from message group by 1) m "
            "on m.session_id = s.id"
        ):
            found.setdefault(sid, ("v1", updated or 0, n))
    return found


def detect_agent(key: str, prefix: str) -> int | None:
    dbs = _databases(key, prefix)
    if not dbs:
        return None
    total = 0
    for db in dbs:
        con = open_ro(db)
        if con is not None:
            try:
                total += len(_sessions(con))
            except sqlite3.Error:
                pass
            finally:
                con.close()
    return total


def items_agent(key: str, prefix: str):
    for db in _databases(key, prefix):
        con = open_ro(db)
        if con is None:
            continue
        try:
            found = _sessions(con)
        except sqlite3.Error:  # an unknown schema, or a database busy beyond the timeout: skip it
            continue
        finally:
            con.close()
        for sid, (family, updated, n) in found.items():
            yield Item(
                f"{db}#{sid}",
                f"{family}:{updated}:{n}",
                lambda d=db, i=sid, f=family: load(key, d, i, f),
            )


def load(key: str, db: str, sid: str, family: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        s = Session(key, sid, f"{db}#{sid}")
        s.root = key
        messages: list[Message] = []
        calls = Calls()
        if family == "v2":
            _read_v2(con, s, messages, calls)
        else:
            _read_v1(con, s, messages, calls)
        s.messages = messages
        s.tool_errors = calls.errors()
        return s
    finally:
        con.close()


def _header(s: Session, directory, title, parent, created, updated) -> None:
    s.project = directory or None
    s.title = None if not title or _PLACEHOLDER.match(title) else title
    s.started_at, s.updated_at = iso_ts(created), iso_ts(updated)
    if parent:  # a subagent's session: its brief is the user turn, its report the assistant's
        s.origin, s.parent = "subagent", f"{s.agent}:{parent}"


def _json(value) -> dict:
    try:
        d = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _read_v1(con: sqlite3.Connection, s: Session, messages: list[Message], calls: Calls) -> None:
    row = con.execute(
        "select directory, title, parent_id, time_created, time_updated from session where id = ?", (s.native_id,)
    ).fetchone()
    if not row:
        return
    _header(s, *row)
    roles = {
        mid: _json(data).get("role")
        for mid, data in con.execute("select id, data from message where session_id = ?", (s.native_id,))
    }
    for mid, created, data in con.execute(
        "select message_id, time_created, data from part where session_id = ? order by message_id, id",
        (s.native_id,),
    ):
        part = _json(data)
        kind, role, ts = part.get("type"), roles.get(mid), iso_ts(created)
        if kind == "tool":
            _tool(part.get("tool"), part.get("callID"), part.get("state"), messages, calls, ts)
        elif kind == "text" and not part.get("synthetic") and role in ("user", "assistant"):
            _add(messages, role, part.get("text"), ts)


def _read_v2(con: sqlite3.Connection, s: Session, messages: list[Message], calls: Calls) -> None:
    row = con.execute(
        "select directory, title, parent_id, time_created, time_updated from session_v2 where id = ?", (s.native_id,)
    ).fetchone()
    if not row:
        return
    _header(s, *row)
    for kind, created, data in con.execute(
        "select type, time_created, data from session_message where session_id = ? order by seq", (s.native_id,)
    ):
        d, ts = _json(data), iso_ts(created)
        if kind == "user":
            _add(messages, "user", d.get("text"), ts)
        elif kind == "assistant":
            for item in d.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    _add(messages, "assistant", item.get("text"), ts)
                elif item.get("type") == "tool":
                    _tool(item.get("name"), item.get("id"), item.get("state"), messages, calls, ts)


def _add(messages: list[Message], role: str, text, ts) -> None:
    if isinstance(text, str):
        append(messages, role, clean_user_text(text) if role == "user" else clean_assistant_text(text), ts)


def _tool(name, call_id, state, messages: list[Message], calls: Calls, ts) -> None:
    """Note a finished tool call, failed when OpenCode says its state is `error`."""
    if not isinstance(state, dict) or state.get("status") not in ("completed", "error"):
        return
    failure = None
    if state.get("status") == "error":
        body = state.get("error")
        failure = signature(body if isinstance(body, str) else json.dumps(body)) or "error"
    calls.add(*position(messages), ts, name or "", state.get("input"), call_id, failure)


AGENTS = {
    "opencode": (
        "OpenCode",
        lambda: detect_agent("opencode", "opencode"),
        lambda: items_agent("opencode", "opencode"),
    ),
    "kilo": ("Kilo Code", lambda: detect_agent("kilo", "kilo"), lambda: items_agent("kilo", "kilo")),
}

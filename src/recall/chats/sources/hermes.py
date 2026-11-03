"""Hermes Agent history (Nous Research): `~/.hermes/state.db`, and `profiles/<name>/state.db` for older builds that
kept one per profile (`HERMES_HOME` moves the home), read with mode=ro; and the transcript files in a `sessions/`
folder beside a database, for the ids no database holds: `<id>.jsonl` (a `session_meta` line, then one message per
line) or `session_<id>.json` (`{session_start, last_updated, messages[]}`). Postgres (`sessiondb.provider:
postgresql`) is not read, and neither is Augure Desktop, which shares the schema under its own folder.

`messages` holds `role` (`user`, `assistant`, `tool`), `content`, `timestamp` (REAL seconds), `tool_calls` (an
OpenAI-style array) and `tool_call_id`; an assistant row that called tools may have no content, and a `tool` row
is output (a `terminal` result is JSON with an `exit_code`: 0 and -1, a command that never ran, are not failures).
A rewound row is `active = 0`; compaction archives the turns it summarised as `active = 0, compacted = 1` and
writes the kept head, a summary and the kept tail again as live rows, so a live row that repeats an archived one
is skipped, and the summary (`[CONTEXT COMPACTION ...`) is not a turn. `sessions` carries `title`, `source` (`cron`
is a run nobody typed, `subagent` a delegate_task child), `parent_session_id`, `model_config` (`_delegate_from`)
and, on builds that record it, `cwd`: the project, never guessed from a profile's name. A skill invocation
(`[SYSTEM: The user has invoked the "..." skill`) is read as the instruction the person gave beside it.

Format knowledge: deja-vu docs/registry/hermes.md and internal/sources/hermes*.go (MIT), agentsview
internal/parser/hermes.go (MIT).
"""

import contextlib
import json
import os
import sqlite3

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, open_ro, read_jsonl, table_names

CHAT_SCHEMA = "2026-11-03 new source: Hermes Agent"

KEY = "hermes"
_SKILL = '[SYSTEM: The user has invoked the "'
_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "
_RUNTIME_NOTE = "\n\n[Runtime note:"
_COMPACTION = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]")
_MULTIMODAL = "\x00json:"  # content stored as this prefix and a JSON list of parts
_MIN_ARCHIVED = 20  # a shorter repeat ("yes") is as likely a new turn as a copy


def _homes() -> list[str]:
    """Each folder that holds a `state.db` or a `sessions/` folder: the home, and a folder per profile."""
    out, seen = [], set()
    for home in roots.other_dirs(KEY):
        profiles = os.path.join(home, "profiles")
        try:
            names = sorted(os.listdir(profiles))
        except OSError:
            names = []
        for folder in [home] + [os.path.join(profiles, n) for n in names]:
            if os.path.isdir(folder) and os.path.realpath(folder) not in seen:
                seen.add(os.path.realpath(folder))
                out.append(folder)
    return out


def _databases() -> list[str]:
    return [db for h in _homes() if os.path.isfile(db := os.path.join(h, "state.db"))]


def _files(skip: set[str]) -> list[tuple[str, str]]:
    """(path, session id) of the transcript files whose id is not in `skip`; a `.jsonl` wins over its `.json`."""
    out = []
    for home in _homes():
        try:
            names = sorted(os.listdir(os.path.join(home, "sessions")))
        except OSError:
            continue
        for n in names:
            if n.endswith(".jsonl"):
                sid = n.removesuffix(".jsonl")
            elif (
                n.startswith("session_") and n.endswith(".json") and n.removesuffix(".json")[8:] + ".jsonl" not in names
            ):
                sid = n.removesuffix(".json")[len("session_") :]
            else:
                continue
            if sid not in skip:
                out.append((os.path.join(home, "sessions", n), sid))
    return out


def _ids(con: sqlite3.Connection) -> dict[str, tuple]:
    """session id -> (message count, newest timestamp, newest id) for each session with messages."""
    if "messages" not in table_names(con):
        return {}
    return {
        sid: (n, last, newest)
        for sid, n, last, newest in con.execute(
            "select session_id, count(*), max(timestamp), max(id) from messages group by session_id"
        )
    }


def _meta(con: sqlite3.Connection) -> dict[str, tuple]:
    """session id -> (ended_at, title): what a rename or the end of a session changes without a message."""
    if "sessions" not in table_names(con):
        return {}
    columns = {r[1] for r in con.execute("pragma table_info(sessions)")}
    wanted = [c for c in ("ended_at", "title") if c in columns]
    if "id" not in columns or not wanted:
        return {}
    return {r[0]: r[1:] for r in con.execute(f"select id, {', '.join(wanted)} from sessions")}


def detect() -> int | None:
    if not _homes():
        return None
    total, ids = 0, set()
    for db in _databases():
        if con := open_ro(db):
            with contextlib.suppress(sqlite3.Error):
                found = _ids(con)
                ids.update(found)
                total += len(found)
            con.close()
    return total + len(_files(ids))


def items():
    in_db: set[str] = set()
    for db in _databases():
        con = open_ro(db)
        if con is None:
            continue
        try:
            found, meta = _ids(con), _meta(con)
        except sqlite3.Error:  # an unknown schema, or a database busy beyond the timeout
            continue
        finally:
            con.close()
        in_db.update(found)
        for sid, stats in found.items():
            yield Item(f"{db}#{sid}", f"{stats}:{meta.get(sid)}", lambda d=db, i=sid: load_db(d, i))
    for path, sid in _files(in_db):
        try:
            st = os.stat(path)
        except OSError:
            continue
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path, i=sid: load_file(p, i))


def _content(value) -> str:
    if isinstance(value, str) and value.startswith(_MULTIMODAL):
        with contextlib.suppress(ValueError):
            value = json.loads(value[len(_MULTIMODAL) :])
    if isinstance(value, list):
        return "\n\n".join(p["text"] for p in value if isinstance(p, dict) and isinstance(p.get("text"), str))
    return value if isinstance(value, str) else ""


def _user(text: str) -> str:
    """What the person typed: a skill invocation holds the skill's text and then their instruction."""
    if text.startswith(_COMPACTION):
        return ""
    if text.startswith(_SKILL):
        _, found, after = text.partition(_SKILL_INSTRUCTION)
        return after.split(_RUNTIME_NOTE)[0].strip() if found else ""
    return clean_user_text(text)


def _calls(value) -> dict[str, tuple[str, object]]:
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            value = json.loads(value)
    out = {}
    for c in value if isinstance(value, list) else []:
        fn = c.get("function") if isinstance(c, dict) and isinstance(c.get("function"), dict) else c
        if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(fn, dict):
            args = fn.get("arguments")
            if isinstance(args, str):
                with contextlib.suppress(ValueError):
                    args = json.loads(args)
            out[c["id"]] = (fn.get("name") or "", args)
    return out


def _result(content: str) -> tuple[bool, str | None]:
    """(whether the call ran, why it failed or None): a `terminal` result with an exit code (0 passes; -1 is a
    command that never ran), or any result that names an error."""
    try:
        result = json.loads(content)
    except ValueError:
        return True, None
    if not isinstance(result, dict):
        return True, None
    code, error = result.get("exit_code"), result.get("error")
    if isinstance(code, int) and not isinstance(code, bool):
        if code == -1:
            return False, None
        if code != 0:
            return True, signature(str(result.get("output") or error or "")) or "error"
    if isinstance(error, str) and error.strip():
        return True, signature(error)
    return True, None


def _build(s: Session, rows: list[dict]) -> Session | None:
    """Fill the session from its message rows, each `{role, content, timestamp, tool_calls, tool_call_id}`, in order."""
    archived = {
        (r["role"], r["content"]) for r in rows if r.get("archived") and len(r.get("content") or "") >= _MIN_ARCHIVED
    }
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    for r in rows:
        role, ts, text = r["role"], iso_ts(r.get("timestamp")), _content(r.get("content"))
        if r.get("archived") is False and (role, r.get("content")) in archived:
            continue
        if role == "user":
            append(messages, "user", _user(text), ts)
        elif role == "assistant":
            append(messages, "assistant", clean_assistant_text(text), ts)
            pending.update(_calls(r.get("tool_calls")))
        elif role == "tool":
            call_id = r.get("tool_call_id")
            name, args = (
                pending.pop(call_id, (r.get("tool_name") or "", None)) if isinstance(call_id, str) else ("", None)
            )
            ran, failure = _result(text)
            if ran:
                calls.add(*position(messages), ts, name, args, call_id, failure)
    s.messages = messages
    s.tool_errors = calls.errors()
    s.started_at = s.started_at or next((m.ts for m in messages if m.ts), None)
    s.updated_at = s.updated_at or next((m.ts for m in reversed(messages) if m.ts), None)
    return s if messages else None


def _row_dicts(con: sqlite3.Connection, sid: str) -> list[dict]:
    columns = {r[1] for r in con.execute("pragma table_info(messages)")}
    names = ["role", "content", "timestamp"] + [c for c in ("tool_calls", "tool_call_id", "tool_name") if c in columns]
    live = ""
    if "active" in columns and "compacted" in columns:
        live = ", active, compacted"
    elif "active" in columns:
        live = ", active, 0"
    out = []
    for row in con.execute(f"select {', '.join(names)}{live} from messages where session_id = ? order by id", (sid,)):
        d = dict(zip(names, row, strict=False))
        if live:
            active, compacted = row[-2], row[-1]
            if not (active or compacted):  # rewound
                continue
            d["archived"] = not active  # a live row is False, a compacted one True
        out.append(d)
    return out


def load_db(db: str, sid: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        s = Session(KEY, sid, f"{db}#{sid}")
        s.root = KEY
        if "sessions" in table_names(con):
            columns = {r[1] for r in con.execute("pragma table_info(sessions)")}
            wanted = [
                c
                for c in ("title", "cwd", "source", "parent_session_id", "model_config", "started_at", "ended_at")
                if c in columns
            ]
            row = (
                con.execute(f"select {', '.join(wanted)} from sessions where id = ?", (sid,)).fetchone()
                if wanted
                else None
            )
            _session(s, dict(zip(wanted, row, strict=True)) if row else {})
        return _build(s, _row_dicts(con, sid))
    finally:
        con.close()


def _session(s: Session, row: dict) -> None:
    def text(key: str) -> str | None:
        return row[key].strip() or None if isinstance(row.get(key), str) else None

    s.title, s.project = text("title"), text("cwd")
    s.started_at, s.updated_at = iso_ts(row.get("started_at")), iso_ts(row.get("ended_at"))
    delegated = None
    if text("model_config"):
        with contextlib.suppress(ValueError, AttributeError):
            delegated = json.loads(row["model_config"]).get("_delegate_from")
    parent = delegated if isinstance(delegated, str) and delegated else text("parent_session_id")
    if delegated or row.get("source") == "subagent":
        s.origin, s.parent = "subagent", f"{KEY}:{parent}" if parent else None
    elif row.get("source") == "cron":
        s.origin = "headless"


def load_file(path: str, sid: str) -> Session | None:
    s = Session(KEY, sid, path)
    s.root = KEY
    try:
        if path.endswith(".jsonl"):
            head, rows = {}, []
            for d in read_jsonl(path):
                if d.get("role") == "session_meta":
                    head = d
                else:
                    rows.append(d)
            started, ended = head.get("timestamp"), None
        else:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                return None
            rows = [m for m in doc.get("messages") or [] if isinstance(m, dict)]
            started, ended = doc.get("session_start"), doc.get("last_updated")
    except (OSError, UnicodeError, ValueError):
        return None
    s.started_at, s.updated_at = iso_ts(started), iso_ts(ended)
    return _build(s, [r for r in rows if isinstance(r.get("role"), str)])


AGENTS = {KEY: ("Hermes Agent", detect, items)}

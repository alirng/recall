"""Kiro history (kiro.dev): the CLI, the IDE and the V3 engine write three different stores.

CLI: `~/.kiro/sessions/cli/<id>.jsonl` beside a header `<id>.json` (`session_id`, `cwd`, `title`, `parent_session_id`
for a sub-agent's session). Records are `{kind, data}`: a `Prompt` or an `AssistantMessage` whose `data.content[]`
parts are `text`, `toolUse` or (in a `ToolResults` record) `toolResult`; only `Prompt` carries a timestamp
(`meta.timestamp`, seconds), so a reply takes its prompt's. A reply arrives in pieces under one `message_id`, joined.
IDE and `kiro-cli --v3`: `~/.kiro/sessions/<workspace>/sess_<id>/messages.jsonl` beside `session.json`
(`workspacePaths`, `title`, `createdAt`); lines are `{timestamp, payload}` with `payload.type` `user`, `assistant`
(`operationType: Reasoning` is thinking, not read), `tool_call` and `tool_result`; older builds wrote flat
`{role, content}` lines. A sub-agent's transcript under `sess_<id>/sub-executions/` is not read.
Headless and classic CLI: `kiro-cli/data.sqlite3`, table `conversations_v2(key, conversation_id, value, created_at,
updated_at)`, `key` the folder it ran in and `value.history` the turns (`user.content.Prompt.prompt`,
`assistant.Response` or `assistant.ToolUse`); the newest row of a conversation id is read. A conversation
that also has a CLI transcript is read from the transcript.

Format knowledge: deja-vu docs/registry/kiro.md and internal/sources/kiro*.go (MIT), agentsview
internal/parser/kiro*.go (MIT).
"""

import contextlib
import json
import os
import sqlite3

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, open_ro, read_jsonl, table_names

CHAT_SCHEMA = "2026-11-03 new source: Kiro"

KEY = "kiro"
_DEPTH = 3  # folders below sessions/ that can hold a sess_<id>/messages.jsonl


def _sessions_dirs() -> list[str]:
    out, seen = [], set()
    for folder in roots.other_dirs(KEY):
        base = os.path.join(folder, "sessions")
        if os.path.isdir(base) and os.path.realpath(base) not in seen:
            seen.add(os.path.realpath(base))
            out.append(base)
    return out


def _cli_files() -> list[str]:
    out = []
    for base in _sessions_dirs():
        try:
            names = sorted(os.listdir(os.path.join(base, "cli")))
        except OSError:
            continue
        out += [os.path.join(base, "cli", n) for n in names if n.endswith(".jsonl")]
    return out


def _ide_files() -> list[str]:
    out = []
    for base in _sessions_dirs():
        top = base.rstrip(os.sep).count(os.sep)
        for dirpath, dirs, names in os.walk(base):
            if dirpath.count(os.sep) - top >= _DEPTH:
                dirs[:] = []
            dirs[:] = [d for d in sorted(dirs) if d not in ("cli", "sub-executions")]
            if os.path.basename(dirpath).startswith("sess_") and "messages.jsonl" in names:
                out.append(os.path.join(dirpath, "messages.jsonl"))
    return out


def _databases() -> list[str]:
    out, seen = [], set()
    for folder in roots.other_dirs(KEY):
        path = os.path.join(folder, "data.sqlite3")
        if os.path.isfile(path) and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            out.append(path)
    return out


def _conversations(con: sqlite3.Connection) -> list[tuple]:
    """(conversation id, key, updated_at) of the newest row of each conversation."""
    if "conversations_v2" not in table_names(con):
        return []
    return con.execute(
        "select conversation_id, key, max(updated_at) from conversations_v2 group by conversation_id"
    ).fetchall()


def _header(path: str) -> dict:
    with contextlib.suppress(OSError, ValueError, UnicodeError):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    return {}


def _stem(path: str) -> str:
    return os.path.basename(path).removesuffix(".jsonl")


def _header_path(path: str) -> str:
    return path.removesuffix(".jsonl") + ".json"


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    total = len(_cli_files()) + len(_ide_files())
    for db in _databases():
        if con := open_ro(db):
            with contextlib.suppress(sqlite3.Error):
                total += len(_conversations(con))
            con.close()
    return total


def _stat(log: str, header: str) -> str | None:
    """Fingerprint of a chat's log and its header (a rename rewrites the header and not the log)."""
    try:
        st = os.stat(log)
    except OSError:
        return None
    fingerprint = f"{st.st_mtime_ns}:{st.st_size}"
    with contextlib.suppress(OSError):
        h = os.stat(header)
        fingerprint += f":{h.st_mtime_ns}:{h.st_size}"
    return fingerprint


def items():
    cli = _cli_files()
    for path in cli:
        if fingerprint := _stat(path, _header_path(path)):
            yield Item(path, fingerprint, lambda p=path: load_cli(p))
    for path in _ide_files():
        if fingerprint := _stat(path, os.path.join(os.path.dirname(path), "session.json")):
            yield Item(path, fingerprint, lambda p=path: load_ide(p))
    databases = _databases()
    transcripts = (
        {_stem(p) for p in cli} | {_header(_header_path(p)).get("session_id") for p in cli} if databases else set()
    )
    for db in databases:
        con = open_ro(db)
        if con is None:
            continue
        try:
            rows = _conversations(con)
        except sqlite3.Error:
            continue
        finally:
            con.close()
        for cid, key, updated in rows:
            if cid and cid not in transcripts:
                yield Item(f"{db}#{cid}", f"{updated}:{key}", lambda d=db, i=cid: load_db(d, i))


def _text(content) -> str:
    """The words of a `content` that is a string or a list of `{kind: text, data}` / `{type: text, text}` parts."""
    if isinstance(content, str):
        return content
    out = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict):
            if part.get("kind") in (None, "text") and isinstance(part.get("data"), str):
                out.append(part["data"])
            elif part.get("type") == "text" and isinstance(part.get("text"), str):
                out.append(part["text"])
    return "\n".join(out)


def _output(parts) -> str:
    """What a CLI or database tool result said: its text, or a shell result's stdout and stderr."""
    out = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        data = part.get("data", part.get("Text", part.get("Json")))
        if isinstance(data, str):
            out.append(data)
        elif isinstance(data, dict):
            out += [data[k] for k in ("stdout", "stderr") if isinstance(data.get(k), str)]
    return "\n".join(o for o in out if o.strip())


def _exit(parts) -> int | None:
    """A shell result's exit code: "exit status: 1" in a transcript, "1" in the database."""
    for part in parts if isinstance(parts, list) else []:
        data = part.get("data", part.get("Json")) if isinstance(part, dict) else None
        if isinstance(data, dict) and isinstance(data.get("exit_status"), str):
            with contextlib.suppress(ValueError):
                return int(data["exit_status"].removeprefix("exit status:").strip())
    return None


def _failed(result: dict) -> bool:
    code = _exit(result.get("content"))
    return str(result.get("status", "")).lower() == "error" or (code is not None and code != 0)


def load_cli(path: str) -> Session | None:
    head = _header(_header_path(path))
    sid = head.get("session_id") if isinstance(head.get("session_id"), str) and head.get("session_id") else _stem(path)
    s = Session(KEY, sid, path)
    s.root = KEY
    s.project = head["cwd"] if isinstance(head.get("cwd"), str) and head["cwd"] else None
    s.title = head["title"].strip() or None if isinstance(head.get("title"), str) else None
    s.started_at, s.updated_at = iso_ts(head.get("created_at")), iso_ts(head.get("updated_at"))
    parent = head.get("parent_session_id")
    if isinstance(parent, str) and parent.strip() and parent != sid:  # session_created_reason is on top-level ones too
        s.origin, s.parent = "subagent", f"{KEY}:{parent.strip()}"
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    ts, run_id, run_raw, run_base = None, None, "", ""
    try:
        for d in read_jsonl(path):
            data = d.get("data")
            if not isinstance(data, dict):
                continue
            meta = data.get("meta")
            ts = iso_ts(meta.get("timestamp") if isinstance(meta, dict) else None) or ts
            parts = data["content"] if isinstance(data.get("content"), list) else []
            kind = d.get("kind")
            if kind == "Prompt":
                run_id = None
                append(messages, "user", clean_user_text(_text(parts)), ts)
            elif kind == "AssistantMessage":
                text, mid = _text(parts), data.get("message_id")
                if mid and mid == run_id and messages and messages[-1].role == "assistant":
                    run_raw += text  # the next piece of a streamed reply
                    cleaned = clean_assistant_text(run_raw)
                    messages[-1].text = f"{run_base}\n\n{cleaned}" if run_base else cleaned
                elif cleaned := clean_assistant_text(text):
                    run_id, run_raw = mid, text
                    run_base = messages[-1].text if messages and messages[-1].role == "assistant" else ""
                    append(messages, "assistant", cleaned, ts)
                else:
                    run_id = None
                for part in parts:
                    use = part.get("data") if isinstance(part, dict) and part.get("kind") == "toolUse" else None
                    if isinstance(use, dict) and isinstance(use.get("toolUseId"), str):
                        pending[use["toolUseId"]] = (use.get("name") or "", use.get("input"))
            for part in parts if kind == "ToolResults" else []:
                result = part.get("data") if isinstance(part, dict) and part.get("kind") == "toolResult" else None
                if isinstance(result, dict):
                    name, args = pending.pop(str(result.get("toolUseId")), ("", None))
                    failure = (signature(_output(result.get("content"))) or "error") if _failed(result) else None
                    calls.add(*position(messages), ts, name, args, result.get("toolUseId"), failure)
    except (OSError, UnicodeError):
        return None
    s.started_at = s.started_at or next((m.ts for m in messages if m.ts), None)
    s.updated_at = s.updated_at or ts
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def load_ide(path: str) -> Session | None:
    folder = os.path.dirname(path)
    meta = _header(os.path.join(folder, "session.json"))
    sid = meta.get("id") if isinstance(meta.get("id"), str) and meta.get("id") else os.path.basename(folder)
    s = Session(KEY, sid, path)
    s.root = KEY
    paths = meta.get("workspacePaths")
    s.project = paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0] else None
    s.title = meta["title"].strip() or None if isinstance(meta.get("title"), str) else None
    s.started_at, s.updated_at = iso_ts(meta.get("createdAt")), iso_ts(meta.get("lastModifiedAt"))
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    try:
        for d in read_jsonl(path):
            payload = d["payload"] if isinstance(d.get("payload"), dict) else d  # flat lines are the older shape
            kind, ts = payload.get("type") or payload.get("role"), iso_ts(d.get("timestamp"))
            if kind in ("user", "human", "prompt"):
                append(messages, "user", clean_user_text(_text(payload.get("content"))), ts)
            elif kind in ("assistant", "bot", "response") and payload.get("operationType") != "Reasoning":
                append(messages, "assistant", clean_assistant_text(_text(payload.get("content"))), ts)
            elif kind == "tool_call" and isinstance(payload.get("toolCallId"), str):
                # one line per state the call passes through; the last holds how it ended
                pending[payload["toolCallId"]] = (
                    payload.get("toolName") or "",
                    payload.get("args"),
                    payload.get("status"),
                )
            elif kind == "tool_result" and isinstance(payload.get("toolCallId"), str):
                name, args, status = pending.pop(payload["toolCallId"], ("", None, None))
                failure = None
                if payload.get("success") is False or status == "failed":
                    failure = signature(_text(payload.get("content"))) or "error"
                calls.add(*position(messages), ts, name, args, payload["toolCallId"], failure)
    except (OSError, UnicodeError):
        return None
    s.started_at = s.started_at or next((m.ts for m in messages if m.ts), None)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _turns(value: str) -> list[dict]:
    with contextlib.suppress(ValueError, TypeError):
        history = json.loads(value).get("history")
        return [t for t in history if isinstance(t, dict)] if isinstance(history, list) else []
    return []


def load_db(db: str, cid: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        row = con.execute(
            "select key, value, created_at, updated_at from conversations_v2 where conversation_id = ? "
            "order by updated_at desc, created_at desc limit 1",
            (cid,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    key, value, created, updated = row
    s = Session(KEY, cid, f"{db}#{cid}")
    s.root = KEY
    s.project = key or None
    s.started_at, s.updated_at = iso_ts(created), iso_ts(updated)
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    ts = None
    for turn in _turns(value):
        user = turn.get("user") if isinstance(turn.get("user"), dict) else {}
        ts = iso_ts(user.get("timestamp")) or ts
        for body in (user.get("content") if isinstance(user.get("content"), dict) else {}).values():
            if not isinstance(body, dict):
                continue
            if body.get("prompt"):  # a prompt comes under Prompt, and under CancelledToolUses beside its results
                append(messages, "user", clean_user_text(str(body["prompt"])), ts)
            for r in body.get("tool_use_results") or []:
                if isinstance(r, dict):
                    name, args = pending.pop(str(r.get("tool_use_id")), ("", None))
                    failure = (signature(_output(r.get("content"))) or "error") if _failed(r) else None
                    calls.add(*position(messages), ts, name, args, r.get("tool_use_id"), failure)
        meta = turn.get("request_metadata")
        end = iso_ts(meta.get("stream_end_timestamp_ms")) if isinstance(meta, dict) else None
        for body in (turn.get("assistant") if isinstance(turn.get("assistant"), dict) else {}).values():
            if isinstance(body, dict):
                append(messages, "assistant", clean_assistant_text(str(body.get("content") or "")), end or ts)
                for use in body.get("tool_uses") or []:
                    if isinstance(use, dict) and isinstance(use.get("id"), str):
                        pending[use["id"]] = (use.get("name") or "", use.get("args"))
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


AGENTS = {KEY: ("Kiro", detect, items)}

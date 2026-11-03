"""Muse Code (Meta's terminal coding agent): `~/.local/share/muse/sessions/YYYY/MM/DD/<id>/session.jsonl`, an
event-sourced log, one record per line: `{stream: {kind, id}, recorded_at (microseconds), payload_type,
payload}`, or a `retained_frame` whose `children[].record_json` are whole records as JSON strings.

The conversation is the `run` events of `runtime.session` records: `started` (`prompt`, a person's turn; a task
start has none), `assistant_message_committed` (`text`), `assistant_tool_calls_committed` (`tool_calls[]` with
`call_id`, `name`, `args` a JSON string) and `tool_result_batch_committed` (`results[]` with `tool_call_id`,
`text`). A failed call is a `bash` result whose JSON `exit_code` is not 0, or a result that reads `tool failed:`.
Only records of the session's own stream count: the log also holds each subagent's task records under another
stream id, and those prompts are not the person's. Subagents' own `subagent/<id>/session.jsonl` files are not
read (their runs are the parent's tool calls, and Muse's reminder observers log children too). The project is
`workspace_root` of the `runtime.session.metadata` record, else `cwd` of `runtime.session.route_facts`, else
`workspace_root` of `session.workspace_branch.observed`; the title is the last `session.name.changed`.

Format knowledge: SpecStory's musecode provider and MUSE-CODE-FORMAT.md (Apache-2.0), deja-vu
docs/registry/muse.md and internal/sources/muse.go (MIT).
"""

import contextlib
import glob
import json
import os

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: Muse Code"

KEY = "muse"
_WORKSPACE = (
    ("runtime.session.metadata", "workspace_root"),
    ("runtime.session.route_facts", "cwd"),
    ("session.workspace_branch.observed", "workspace_root"),
)


def _files() -> list[str]:
    pattern = os.path.join("sessions", "*", "*", "*", "*", "session.jsonl")  # not sessions/../<id>/subagent/..
    return sorted(p for folder in roots.other_dirs(KEY) for p in glob.glob(os.path.join(glob.escape(folder), pattern)))


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return len(_files())


def items():
    for path in _files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path: load(p))


def _records(path: str):
    for line in read_jsonl(path):
        children = line.get("children")
        if not isinstance(children, list):
            yield line
            continue
        for child in children:
            try:
                rec = json.loads(child.get("record_json") or "")
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(rec, dict):
                yield rec


def _seconds(value) -> float | None:
    return value / 1e6 if isinstance(value, (int, float)) and value > 0 else None


def load(path: str) -> Session | None:
    s = Session(KEY, os.path.basename(os.path.dirname(path)), path)
    s.root = KEY
    messages: list[Message] = []
    calls = Calls()
    running: dict[str, tuple[str, object]] = {}
    stream: str | None = None
    where: dict[str, str] = {}
    for rec in _records(path):
        ident = rec.get("stream") if isinstance(rec.get("stream"), dict) else {}
        if stream is None and ident.get("kind") == "session" and ident.get("id"):
            stream = ident["id"]
        if ident.get("id") and stream and ident["id"] != stream:  # a subagent's task, logged in this file
            continue
        ts = _seconds(rec.get("recorded_at"))
        if ts is not None:
            s.started_at = min(s.started_at or ts, ts)
            s.updated_at = max(s.updated_at or ts, ts)
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        kind = rec.get("payload_type")
        for want, key in _WORKSPACE:
            inner = payload.get("record") if isinstance(payload.get("record"), dict) else {}
            if kind == want and isinstance(inner.get(key), str) and inner[key]:
                where.setdefault(want, inner[key])
        if kind == "session.name.changed" and isinstance(payload.get("new_name"), str) and payload["new_name"].strip():
            s.title = payload["new_name"].strip().split("\n")[0][:120]
        elif kind == "runtime.session" and payload.get("kind") == "run" and isinstance(payload.get("event"), dict):
            _event(payload["event"], ts, messages, calls, running)
    s.project = next((where[w] for w, _ in _WORKSPACE if w in where), None)
    s.native_id = stream or s.native_id
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _event(ev: dict, ts, messages: list[Message], calls: Calls, running: dict) -> None:
    kind = ev.get("kind")
    if kind == "started" and isinstance(ev.get("prompt"), str):
        append(messages, "user", clean_user_text(ev["prompt"]), ts)
    elif kind == "assistant_message_committed" and isinstance(ev.get("text"), str):
        append(messages, "assistant", clean_assistant_text(ev["text"]), ts)
    elif kind == "assistant_tool_calls_committed":
        for c in ev.get("tool_calls") or []:
            if isinstance(c, dict):
                args = c.get("args")
                with contextlib.suppress(ValueError):
                    args = json.loads(args) if isinstance(args, str) else args
                running[str(c.get("call_id") or c.get("id"))] = (c.get("name") or "", args)
    elif kind == "tool_result_batch_committed":
        for r in ev.get("results") or []:
            if isinstance(r, dict) and isinstance(r.get("text"), str):
                name, args = running.pop(str(r.get("tool_call_id")), ("", None))
                failure = _failure(r["text"])
                calls.add(*position(messages), ts, name, args, r.get("tool_call_id"), failure)


def _failure(text: str) -> str | None:
    """Why a result is a failed call: its `bash` exit code, or Muse's own `tool failed:` line."""
    if text.startswith("tool failed:"):
        return signature(text) or "tool failed"
    try:
        report = json.loads(text) if text.lstrip().startswith("{") else None
    except ValueError:
        return None
    if isinstance(report, dict) and isinstance(report.get("exit_code"), int) and report["exit_code"] != 0:
        return signature(str(report.get("output") or "")) or f"exit code {report['exit_code']}"
    return None


AGENTS = {KEY: ("Muse Code", detect, items)}

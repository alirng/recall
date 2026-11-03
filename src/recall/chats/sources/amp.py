"""Amp's local threads: `~/.local/share/amp/threads/T-*.json`, one JSON object per thread, rewritten whole as the
thread goes on. Amp builds from 0.0.1774963753 (2026-03-31) keep threads on ampcode.com and write none, so this
reads the older builds' files, and the files that Amp's plugin writes in the same shape (see deja-vu's page).

A thread is `{id, title, created (ms), env: {initial: {trees: [{uri: "file:///.."}]}}, messages: [..]}`. A message
is `{role: user|assistant, content: [blocks], meta: {sentAt (ms)}, usage: {timestamp (ISO)}}` and a block is
`text`, `thinking`, `tool_use` {id, name, input} or `tool_result` {toolUseID, run: {status, result, error}}. Only
`text` blocks are indexed. A run that ended `error`, or a shell run with a positive `exitCode`, is a failed call;
one that was rejected or cancelled is the person's choice, not a failure. A message with no time of its own takes
the previous one's, the first the thread's `created`.

Format knowledge: deja-vu docs/registry/amp.md and fixtures/registry/amp (MIT); agentsview
internal/parser/amp.go (MIT).
"""

import glob
import json
import os
from urllib.parse import unquote, urlparse

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts

CHAT_SCHEMA = "2026-11-03 new source: Amp"

KEY = "amp"


def _files() -> list[str]:
    return sorted(
        p for folder in roots.other_dirs(KEY) for p in glob.glob(os.path.join(glob.escape(folder), "threads", "*.json"))
    )


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


def _folder(uri) -> str | None:
    u = urlparse(uri) if isinstance(uri, str) else None
    if u is None or u.scheme != "file" or u.netloc not in ("", "localhost"):
        return None
    return unquote(u.path).rstrip("/") or None


def load(path: str) -> Session | None:
    with open(path, encoding="utf-8") as f:
        thread = json.load(f)  # a file Amp is rewriting right now does not parse: the indexer tries it again
    if not isinstance(thread, dict):
        return None
    s = Session(KEY, os.path.basename(path).removesuffix(".json"), path)
    s.root = KEY
    title = thread.get("title")
    s.title = title.strip().split("\n")[0][:120] or None if isinstance(title, str) else None
    s.started_at = last = iso_ts(thread.get("created"))
    s.updated_at = s.started_at
    env = thread.get("env") if isinstance(thread.get("env"), dict) else {}
    trees = (env.get("initial") or {}).get("trees") if isinstance(env.get("initial"), dict) else None
    s.project = (
        _folder(trees[0].get("uri")) if isinstance(trees, list) and trees and isinstance(trees[0], dict) else None
    )
    messages: list[Message] = []
    calls = Calls()
    running: dict[str, tuple[str, object]] = {}
    for m in thread.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        meta, usage = m.get("meta"), m.get("usage")
        ts = iso_ts((meta or {}).get("sentAt") if isinstance(meta, dict) else None) or iso_ts(
            (usage or {}).get("timestamp") if isinstance(usage, dict) else None
        )
        last = ts = ts or last
        s.updated_at = max(s.updated_at or 0, ts or 0) or None
        content = m.get("content")
        for b in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text" and isinstance(b.get("text"), str):
                text = clean_user_text(b["text"]) if m["role"] == "user" else clean_assistant_text(b["text"])
                append(messages, m["role"], text, ts)
            elif kind == "tool_use":
                running[str(b.get("id"))] = (b.get("name") or "", b.get("input"))
            elif kind == "tool_result":
                _result(b, running, messages, calls, ts)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _result(b: dict, running: dict, messages: list[Message], calls: Calls, ts) -> None:
    call_id = b.get("toolUseID") or b.get("tool_use_id")
    run = b.get("run") if isinstance(b.get("run"), dict) else {}
    if run.get("status") not in ("done", "error"):  # still running, rejected or cancelled
        return
    result = run.get("result")
    code = result.get("exitCode") if isinstance(result, dict) else None
    failure = None
    if run.get("status") == "error" or (isinstance(code, int) and code > 0):  # -1: no code from the process
        error = run.get("error")
        detail = (error.get("message") if isinstance(error, dict) else error) or (
            result.get("output") if isinstance(result, dict) else result
        )
        failure = signature(detail if isinstance(detail, str) else json.dumps(detail or "")) or "error"
    name, args = running.pop(str(call_id), ("", None))
    calls.add(*position(messages), ts, name, args, call_id, failure)


AGENTS = {KEY: ("Amp", detect, items)}

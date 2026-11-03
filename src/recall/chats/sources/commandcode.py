"""Command Code (commandcode.ai): `~/.commandcode/projects/<encoded-cwd>/<session>.jsonl`, one transcript per
session. Since 1.73 (format version 3) line 1 is a header `{"type": "session", "version": 3, "id", "timestamp",
"cwd"}` and each later line is `{"type": "message", "timestamp", "message": {"role", "content"}}`, the content
in Claude's blocks (`text`, `tool_use`, `tool_result`). The older shape is one flat `{role, content, timestamp,
sessionId}` line per message (roles user, assistant and tool); Command Code migrates such a file in place when it
next opens the session. The folder name is a lossy slug of the cwd, so the project is the header's `cwd`.

A failed command's `tool_result` opens with "Exit code: N" and carries no `is_error`. Beside a transcript sit
`<session>.checkpoints.jsonl`, `<session>.prompts.jsonl` and `<session>.v2.bak`, which are not conversations.

Format knowledge: deja-vu docs/registry/commandcode.md, internal/sources/commandcode.go and
fixtures/registry/commandcode (MIT); agentsview internal/parser/commandcode.go (MIT).
"""

import glob
import os
import re

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

CHAT_SCHEMA = "2026-11-03 new source: Command Code"

KEY = "commandcode"
_NOT_TRANSCRIPTS = (".checkpoints.", ".prompts.", ".v2.bak")
_EXIT = re.compile(r"\AExit code: (-?\d+)")


def _files() -> list[str]:
    pattern = os.path.join("projects", "*", "*.jsonl")
    found = (p for folder in roots.other_dirs(KEY) for p in glob.glob(os.path.join(glob.escape(folder), pattern)))
    return sorted(p for p in found if not any(skip in os.path.basename(p) for skip in _NOT_TRANSCRIPTS))


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


def load(path: str) -> Session | None:
    s = Session(KEY, os.path.basename(path).removesuffix(".jsonl"), path)
    s.root = KEY
    messages: list[Message] = []
    calls = Calls()
    running: dict[str, tuple[str, object]] = {}
    for d in read_jsonl(path):
        ts = iso_ts(d.get("timestamp"))
        if ts is not None:
            s.started_at = min(s.started_at or ts, ts)
            s.updated_at = max(s.updated_at or ts, ts)
        if d.get("type") == "session":
            s.native_id = d.get("id") or s.native_id
            s.project = d["cwd"] if isinstance(d.get("cwd"), str) and d["cwd"] else s.project
        elif d.get("type") == "message" and isinstance(d.get("message"), dict):
            _message(d["message"], ts, messages, calls, running)
        elif d.get("type") is None:  # the older flat line
            s.native_id = d.get("sessionId") or s.native_id
            _message(d, ts, messages, calls, running)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _message(m: dict, ts, messages: list[Message], calls: Calls, running: dict) -> None:
    role, content = m.get("role"), m.get("content")
    if role in ("user", "assistant"):
        text = text_blocks(content, ("text",))
        append(messages, role, clean_user_text(text) if role == "user" else clean_assistant_text(text), ts)
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            running[str(b.get("id"))] = (b.get("name") or "", b.get("input"))
        elif b.get("type") == "tool_result":
            name, args = running.pop(str(b.get("tool_use_id")), ("", None))
            output = b.get("content")
            output = (
                output
                if isinstance(output, str)
                else "\n".join(str(x.get("text") or "") for x in output or [] if isinstance(x, dict))
            )
            code = _EXIT.match(output.lstrip())
            failure = (signature(output) or "error") if code and int(code.group(1)) != 0 else None
            calls.add(*position(messages), ts, name, args, b.get("tool_use_id"), failure)


AGENTS = {KEY: ("Command Code", detect, items)}

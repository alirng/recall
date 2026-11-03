"""Junie (JetBrains' coding agent, the CLI and the IDE's agent): `~/.junie/sessions/<session-id>/events.jsonl`, one
event per line `{kind, timestampMs, ...}`, with `sessions/index.jsonl` giving each session's `projectDir` and
`taskName`. `UserPromptEvent` is what the person typed (`presentablePrompt`, else `prompt`). The agent's work
arrives as `SessionA2uxEvent{event: {agentEvent: {kind, stepId, ...}}}`, one block per step that Junie updates in
place, so the last update of a `stepId` wins and the step keeps the place of its first: `MarkdownBlockUpdatedEvent`
(`text`) and `ResultBlockUpdatedEvent` (`result`) are the agent's words, `TerminalBlockUpdatedEvent` a command
(failed when `status` is `FAILED` or `exitCode` is not 0). `AgentTaskNameUpdatedEvent` names the task.
A headless run (`junie "task"`) writes no `UserPromptEvent`, and its task is only in `state.json`, which is not
read: such a session holds the agent's words alone and is marked headless.

Format knowledge: deja-vu docs/registry/junie.md, internal/sources/junie.go, aui.go and fixtures/registry/junie
(MIT); agentsview internal/parser/junie.go (MIT).
"""

import contextlib
import glob
import os
import re

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: Junie"

KEY = "junie"
_ADDITIONAL_CONTEXT = re.compile(r"(?s)<additional_context>.*?</additional_context>")


def _files() -> list[str]:
    pattern = os.path.join("sessions", "*", "events.jsonl")
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
        index = 0  # the project and a fallback title come from an index beside the sessions
        with contextlib.suppress(OSError):
            index = os.stat(_index_path(path)).st_mtime_ns
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}:{index}", lambda p=path: load(p))


def _index_path(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(path)), "index.jsonl")


def _indexed(path: str) -> dict:
    sid = os.path.basename(os.path.dirname(path))
    found: dict = {}
    with contextlib.suppress(OSError):
        found = next((d for d in reversed(list(read_jsonl(_index_path(path)))) if d.get("sessionId") == sid), {})
    return found


def _str(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def load(path: str) -> Session | None:
    index = _indexed(path)
    s = Session(KEY, os.path.basename(os.path.dirname(path)), path)
    s.root = KEY
    s.project = _str(index.get("projectDir"))
    s.title = _str(index.get("taskName"))
    order: list = []  # ("user", text, ts) or ("step", stepId)
    steps: dict[str, tuple[dict, float | None]] = {}
    named = False
    for d in read_jsonl(path):
        ts = iso_ts(d.get("timestampMs"))
        if ts is not None:
            s.started_at = min(s.started_at or ts, ts)
            s.updated_at = max(s.updated_at or ts, ts)
        if d.get("kind") == "UserPromptEvent":
            order.append(("user", _str(d.get("presentablePrompt")) or _str(d.get("prompt")) or "", ts))
        elif d.get("kind") == "SessionA2uxEvent":
            event = d.get("event") if isinstance(d.get("event"), dict) else {}
            ae = event.get("agentEvent") if isinstance(event.get("agentEvent"), dict) else {}
            kind = str(ae.get("kind") or "").rsplit(".", 1)[-1]  # the IDE writes the package in front
            if kind == "AgentTaskNameUpdatedEvent" and _str(ae.get("name")) and not named:
                s.title, named = _str(ae["name"]).split("\n")[0][:120], True
            elif kind == "CurrentDirectoryUpdatedEvent" and not s.project:
                s.project = _str(ae.get("currentDirectory"))
            elif kind in ("MarkdownBlockUpdatedEvent", "ResultBlockUpdatedEvent", "TerminalBlockUpdatedEvent"):
                step = str(ae.get("stepId") or f"{kind}:{len(order)}")
                if step not in steps:
                    order.append(("step", step))
                steps[step] = ({**ae, "kind": kind}, ts)
    messages: list[Message] = []
    calls = Calls()
    for entry in order:
        if entry[0] == "user":
            append(messages, "user", clean_user_text(_ADDITIONAL_CONTEXT.sub("", entry[1])), entry[2])
            continue
        ae, ts = steps[entry[1]]
        if ae["kind"] == "TerminalBlockUpdatedEvent":
            _command(ae, ts, messages, calls)
            continue
        text = ae.get("text") if ae["kind"] == "MarkdownBlockUpdatedEvent" else ae.get("result")
        if isinstance(text, str):
            append(messages, "assistant", clean_assistant_text(_ADDITIONAL_CONTEXT.sub("", text)), ts)
    if not any(m.role == "user" for m in messages):
        s.origin = "headless"
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _command(ae: dict, ts, messages: list[Message], calls: Calls) -> None:
    code = ae.get("exitCode")
    if ae.get("status") not in ("COMPLETED", "FAILED") and not isinstance(code, int):
        return  # still running
    failed = ae.get("status") == "FAILED" or (isinstance(code, int) and code != 0)
    output = ae.get("output")
    failure = (signature(output if isinstance(output, str) else "") or f"exit code {code}") if failed else None
    calls.add(*position(messages), ts, "terminal", {"command": ae.get("command")}, ae.get("stepId"), failure)


AGENTS = {KEY: ("Junie", detect, items)}

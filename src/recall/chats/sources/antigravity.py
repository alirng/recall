"""Google Antigravity history (the IDE, the 2.0 desktop app and the `agy` CLI).

Current builds write, per conversation, a plaintext transcript at
`<root>/brain/<uuid>/.system_generated/logs/transcript_full.jsonl` (one step per line; the SQLite
`conversations/<uuid>.db` holds undocumented protobuf and is not read). The roots are
`~/.gemini/antigravity`, `antigravity-ide` and `antigravity-cli`. Builds before June 2026 wrote only an
encrypted `conversations/<uuid>.pb`, which recall never touches, and Markdown artifacts in `brain/<uuid>/`
(task, implementation plan, walkthrough): a conversation with no transcript is indexed from those, the
agent's own plan and report, since the words the person typed are in the encrypted file.

Format knowledge: SpecStory's ANTIGRAVITY-FORMAT.md (Apache-2.0), cross-checked with franken_agent_detection
and agent-sessions; Google documents none of it (docs/agents.md).
"""

import contextlib
import json
import os
import re

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: Antigravity"

KEY = "antigravity"
LOGS = os.path.join(".system_generated", "logs")
_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_ARTIFACTS = (".md",)
_FAILED = re.compile(r"The command failed with exit code: (\d+)")
# Steps that are not a turn: context markers, injected notices and compaction headers.
_SKIP = ("CONVERSATION_HISTORY", "SYSTEM_MESSAGE", "CHECKPOINT")


def _brains() -> list[tuple[str, str]]:
    """(root folder, brain/<uuid> folder) for each conversation, once per uuid: a conversation that
    two roots hold (an upgraded install) is read from the first."""
    out, seen = [], set()
    for folder in roots.other_dirs(KEY):
        base = os.path.join(folder, "brain")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if name not in seen and os.path.isdir(os.path.join(base, name)):
                seen.add(name)
                out.append((folder, os.path.join(base, name)))
    return out


def _transcript(brain: str) -> str | None:
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        path = os.path.join(brain, LOGS, name)
        if os.path.isfile(path):
            return path
    return None


def _artifacts(brain: str) -> list[str]:
    try:
        names = sorted(os.listdir(brain))
    except OSError:
        return []
    return [os.path.join(brain, n) for n in names if n.endswith(_ARTIFACTS) and not n.startswith(".")]


def detect() -> int | None:
    if not roots.other_dirs(KEY):
        return None
    return sum(1 for _, brain in _brains() if _transcript(brain) or _artifacts(brain))


def items():
    workspaces: dict[str, dict[str, str]] = {}
    for folder, brain in _brains():
        uuid = os.path.basename(brain)
        transcript = _transcript(brain)
        try:
            if transcript:
                st = os.stat(transcript)
                fingerprint, key = f"{st.st_mtime_ns}:{st.st_size}", transcript
            else:
                files = _artifacts(brain)
                if not files:
                    continue
                stats = [os.stat(f) for f in files]
                fingerprint = f"art:{max(s.st_mtime_ns for s in stats)}:{sum(s.st_size for s in stats)}"
                key = brain
        except OSError:
            continue
        if folder not in workspaces:
            workspaces[folder] = _history(folder)
        yield Item(
            key,
            fingerprint,
            lambda t=transcript, b=brain, u=uuid, w=workspaces[folder]: load(t, b, u, w.get(u)),
        )


def _history(folder: str) -> dict[str, str]:
    """conversation id -> workspace folder, from the CLI's prompt log (interactive sessions only). The
    transcript itself names no folder, and a folder is never guessed from the paths a session touched."""
    out: dict[str, str] = {}
    path = os.path.join(folder, "history.jsonl")
    with contextlib.suppress(OSError):
        for d in read_jsonl(path):
            if d.get("conversationId") and isinstance(d.get("workspace"), str):
                out[d["conversationId"]] = d["workspace"]
    return out


def load(transcript: str | None, brain: str, uuid: str, workspace: str | None = None) -> Session | None:
    s = Session(KEY, uuid, transcript or brain)
    s.root = KEY
    s.project = workspace
    if transcript is None:
        return _artifact_chat(s, brain)
    messages: list[Message] = []
    calls = Calls()
    pending: list[tuple[str, dict]] = []  # the tool calls the last planner step made, in order
    for d in read_jsonl(transcript):
        kind, ts = d.get("type"), iso_ts(d.get("created_at"))
        s.started_at = s.started_at or ts
        s.updated_at = ts or s.updated_at
        if kind == "USER_INPUT":
            found = _REQUEST.search(d.get("content") or "")
            append(messages, "user", clean_user_text(found.group(1) if found else ""), ts)
        elif kind == "PLANNER_RESPONSE":
            append(messages, "assistant", clean_assistant_text(d.get("content") or ""), ts)
            pending = [
                (c.get("name") or "", c.get("args") or {}) for c in d.get("tool_calls") or [] if isinstance(c, dict)
            ]
        elif kind not in _SKIP and pending:  # a tool's result: the step that follows its call
            name, args = pending.pop(0)
            calls.add(*position(messages), ts, name, args, f"{uuid}:{d.get('step_index')}", _failure(d))
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _failure(step: dict) -> str | None:
    """A tool call failed when the step's status says so, or, for a shell command (whose step is DONE
    whatever its exit code), when its output says it exited non-zero."""
    content = step.get("content") or ""
    if step.get("status") == "ERROR":
        return signature(content) or "error"
    if step.get("type") == "RUN_COMMAND" and _FAILED.search(content):
        return signature(content.split("Output:", 1)[-1]) or _FAILED.search(content).group(0)
    return None


def _artifact_chat(s: Session, brain: str) -> Session | None:
    """A conversation from before the plaintext transcripts: the agent's artifacts, one message each."""
    messages: list[Message] = []
    for path in _artifacts(brain):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
            st = os.stat(path)
        except OSError:
            continue
        meta = _meta(path)
        ts = iso_ts(meta.get("updatedAt")) or st.st_mtime
        s.started_at = min(s.started_at or ts, ts)
        s.updated_at = max(s.updated_at or ts, ts)
        s.title = s.title or meta.get("summary")
        if text:
            append(messages, "assistant", f"{os.path.basename(path)}\n\n{text}", ts)
    s.messages = messages
    return s


def _meta(path: str) -> dict:
    try:
        with open(path + ".metadata.json", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


AGENTS = {KEY: ("Antigravity", detect, items)}

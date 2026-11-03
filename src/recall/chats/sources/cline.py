"""Cline, Roo Code and Kilo Code (the Roo-style extension, before Kilo 7): whole-file JSON, rewritten as a chat goes on.

Cline's SDK (CLI, desktop app, newer extension) keeps `<data>/sessions/<id>/<id>.messages.json` with a manifest
`<id>.json` beside it. The older layout, still written by Roo and by Kilo's extension and found beside Cline's,
is one folder per task, `<globalStorage>/<extension>/tasks/<id>/api_conversation_history.json`, whose title and
folder come from `state/taskHistory.json` (Cline) or `history_item.json` (Roo, Kilo). The extension folders sit
in the host editor's globalStorage (VS Code, VS Code Insiders, VSCodium, Cursor, Windsurf), see roots.OTHERS.

A Cline session that Cline itself imported from another agent (`metadata.importedFrom`) is skipped: that agent's
own source indexes it. Subagent and teammate message files of a session are skipped too.
Format knowledge: deja-vu docs/registry/{cline,roo}.md and internal/sources/cline.go (MIT), and Cline's own
source as cited in docs/research/agent-storage-and-integration.md.
"""

import contextlib
import functools
import glob
import json
import os
import re

from ... import roots
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts

CHAT_SCHEMA = "2026-11-03 new source: Cline, Roo Code, Kilo Code (extension)"

LEGACY = "api_conversation_history.json"
# Blocks the host adds to the person's turn, and the retry prompt it sends when the model used no tool.
_HOST_BLOCKS = re.compile(
    r"(?ms)^[ \t]*<(environment_details|workspace_diagnostics|slash_command)>[ \t]*\r?\n.*?(?:^[ \t]*</\1>[ \t]*$|\Z)"
)
_NO_TOOLS = re.compile(
    r"(?ms)^\[ERROR\] You did not use a tool in your previous response!.*?(?:\(This is an automated message.*?\)|\Z)"
)
_ENVELOPE = re.compile(r"(?s)\A<(task|user_message|user_input)(?:\s[^>]*)?>\s*(.*?)\s*(?:</\1>)?\s*\Z")
# The result of a tool call in a task written before native tool calling arrives as a text block like this.
_TOOL_RESULT = re.compile(r"\A\[[\w .-]+ for '.*?'\] Result:")


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):  # a file the agent is rewriting right now reads as damaged: next run
        return None


@functools.lru_cache(maxsize=16)
def _history(path: str, stamp: int) -> dict[str, dict]:
    """Cline's state/taskHistory.json: task id -> its entry (task, ts, cwdOnTaskInitialization)."""
    entries = _read(path)
    if not isinstance(entries, list):
        return {}
    return {str(e["id"]): e for e in entries if isinstance(e, dict) and e.get("id")}


def _meta(task_dir: str) -> dict:
    """What the agent says about a task folder: Roo and Kilo's history_item.json, else Cline's taskHistory."""
    item = _read(os.path.join(task_dir, "history_item.json"))
    if isinstance(item, dict):
        return item
    path = os.path.join(os.path.dirname(os.path.dirname(task_dir)), "state", "taskHistory.json")
    try:
        return _history(path, os.stat(path).st_mtime_ns).get(os.path.basename(task_dir), {})
    except OSError:
        return {}


def _sources(key: str) -> list[tuple[str, str]]:
    """(kind, path) of each chat file of this agent: kind is "sdk" or "task"."""
    out = []
    for folder in roots.other_dirs(key):
        out += [("sdk", p) for p in glob.glob(os.path.join(folder, "sessions", "*", "*.messages.json"))]
        out += [("task", p) for p in glob.glob(os.path.join(folder, "tasks", "*", LEGACY))]
    return out


def detect_agent(key: str) -> int | None:
    if not roots.other_dirs(key):
        return None
    return len(_sources(key))


def items_agent(key: str):
    for kind, path in _sources(key):
        try:
            st = os.stat(path)
        except OSError:
            continue
        extra = 0  # a task's title and folder come from a file beside it, which can change on its own
        with contextlib.suppress(OSError):
            extra = os.stat(os.path.join(os.path.dirname(path), "history_item.json")).st_mtime_ns
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}:{extra}", lambda p=path, k=kind: load(key, k, p))


def load(key: str, kind: str, path: str) -> Session | None:
    data = _read(path)
    if data is None:
        raise ValueError("not readable as JSON yet")  # being rewritten: the indexer tries again later
    folder = os.path.dirname(path)
    sid = os.path.basename(folder)
    s = Session(key, sid, path)
    s.root = key
    if kind == "sdk":
        if not isinstance(data, dict) or data.get("agent") not in (None, "lead"):
            return None
        manifest = _read(os.path.join(folder, f"{sid}.json"))
        manifest = manifest if isinstance(manifest, dict) else {}
        meta = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        if meta.get("importedFrom"):
            return None
        s.project = manifest.get("cwd") or manifest.get("workspace_root")
        prompt = (manifest.get("prompt") or "").strip()
        s.title = (meta.get("title") or prompt.split("\n")[0])[:120] or None
        s.started_at = iso_ts(manifest.get("started_at") or manifest.get("created_at"))
        s.updated_at = iso_ts(manifest.get("ended_at") or manifest.get("updated_at") or data.get("updated_at"))
        turns = data.get("messages")
    else:
        info = _meta(folder)
        s.project = info.get("workspace") or info.get("cwdOnTaskInitialization")
        s.title = (info.get("task") or "").strip().split("\n")[0][:120] or None
        s.started_at = iso_ts(info.get("ts"))
        turns = data
    messages: list[Message] = []
    for m in turns if isinstance(turns, list) else []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        if (m.get("metadata") or {}).get("displayRole") == "system":  # context a hook added, not a turn
            continue
        ts = iso_ts(m.get("ts"))
        s.started_at = s.started_at or ts
        s.updated_at = max(s.updated_at or 0, ts or 0) or None
        text = _text(m.get("content"))
        clean = clean_user_text(_person(text)) if m["role"] == "user" else clean_assistant_text(text)
        append(messages, m["role"], clean, ts)
    s.messages = messages
    return s


def _text(content) -> str:
    """The text blocks of a turn (a string, or a list of typed blocks), without a tool's result."""
    if isinstance(content, str):
        return content.strip()
    parts = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "text":
            text = (b.get("text") or "").strip()
            if text and not _TOOL_RESULT.match(text):
                parts.append(text)
    return "\n".join(parts)


def _person(text: str) -> str:
    """The person's words: without the host's blocks and retry prompt, and out of the <task> envelope."""
    text = _NO_TOOLS.sub("", _HOST_BLOCKS.sub("", text)).strip()
    found = _ENVELOPE.match(text)
    return found.group(2) if found else text


AGENTS = {
    "cline": ("Cline", lambda: detect_agent("cline"), lambda: items_agent("cline")),
    "roo": ("Roo Code", lambda: detect_agent("roo"), lambda: items_agent("roo")),
    "kilo-legacy": ("Kilo Code (extension)", lambda: detect_agent("kilo-legacy"), lambda: items_agent("kilo-legacy")),
}

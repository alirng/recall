"""Mistral Vibe: `~/.vibe/logs/session/session_<date>_<time>_<id>/messages.jsonl` (one OpenAI-style message per
line: `role` user, assistant, tool or system, `content`, `tool_calls`) and a `meta.json` beside it with the
`session_id`, `title`, `start_time` and `end_time` (ISO) and the folder in `working_dir` or
`environment.working_directory`. A record with `injected: true` is context Vibe added (often under the user
role), and system, tool and reasoning records are not indexed. The format records no tool failures.

Format knowledge: agentsview internal/parser/vibe.go and its testdata/vibe (MIT); franken_agent_detection
src/connectors/vibe.rs (MIT).
"""

import contextlib
import glob
import json
import os

from ... import roots
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, read_jsonl

CHAT_SCHEMA = "2026-11-03 new source: Mistral Vibe"

KEY = "vibe"


def _files() -> list[str]:
    pattern = os.path.join("logs", "session", "*", "messages.jsonl")
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
        meta = 0  # the title and folder are in a file beside the messages, which Vibe rewrites on its own
        with contextlib.suppress(OSError):
            meta = os.stat(os.path.join(os.path.dirname(path), "meta.json")).st_mtime_ns
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}:{meta}", lambda p=path: load(p))


def _meta(path: str) -> dict:
    try:
        with open(os.path.join(os.path.dirname(path), "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def load(path: str) -> Session | None:
    meta = _meta(path)
    env = meta.get("environment") if isinstance(meta.get("environment"), dict) else {}
    s = Session(KEY, str(meta.get("session_id") or os.path.basename(os.path.dirname(path))), path)
    s.root = KEY
    folder = meta.get("working_dir") or env.get("working_directory")
    s.project = folder if isinstance(folder, str) and folder else None
    title = meta.get("title")
    s.title = title.strip().split("\n")[0][:120] or None if isinstance(title, str) else None
    s.started_at, s.updated_at = iso_ts(meta.get("start_time")), iso_ts(meta.get("end_time"))
    messages: list[Message] = []
    for m in read_jsonl(path):
        role, text = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or m.get("injected") or not isinstance(text, str):
            continue
        append(messages, role, clean_user_text(text) if role == "user" else clean_assistant_text(text), None)
    s.messages = messages
    return s


AGENTS = {KEY: ("Mistral Vibe", detect, items)}

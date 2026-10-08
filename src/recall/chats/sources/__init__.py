"""Readers for each agent's on-disk chat history.

Each source yields Items: a stable key, a cheap fingerprint (so unchanged chats
are skipped on re-index) and a loader that parses the chat only when needed.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from ..transcript import Message, Session


@dataclass
class Item:
    key: str
    fingerprint: str
    load: Callable[[], Session | None]


def iso_ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_jsonl(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                yield d


def text_blocks(content, types=("text", "input_text", "output_text")) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") in types).strip()


def append(messages: list[Message], role: str, text: str, ts: float | None) -> None:
    """Add a turn, merging consecutive turns from the same speaker."""
    if not text:
        return
    if messages and messages[-1].role == role:
        messages[-1].text += "\n\n" + text
    else:
        messages.append(Message(role, text, ts))


# Agent name -> (display name, detector, item sources). A detector returns the
# number of chats found on disk, or None if the agent is not installed.
def agents():
    from . import claude, codex, cursor

    return {
        "claude": ("Claude Code", claude.detect, {"claude": claude.items}),
        "codex": ("Codex", codex.detect, {"codex": codex.items}),
        "cursor": ("Cursor", cursor.detect, {"cursor": cursor.transcript_items, "cursor-legacy": cursor.legacy_items}),
    }


def all_sources(selected: list[str] | None = None) -> dict:
    """Item sources for the selected agents; none selected means every agent."""
    out = {}
    for name, (_, _, sources) in agents().items():
        if not selected or name in selected:
            out.update(sources)
    return out

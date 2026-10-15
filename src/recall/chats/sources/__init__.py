"""Readers for each agent's on-disk chat history.

Each source yields Items: a stable key, a cheap fingerprint (so unchanged chats
are skipped on re-index) and a loader that parses the chat only when needed.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from ..transcript import Message, Session
from . import compressed


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
    """The JSON objects of a JSONL chat file. A path whose file Codex has since compressed
    (or a .jsonl.zst path) is read from whichever form is on disk; see compressed.py."""
    with compressed.open_text(path) as f:
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
# number of chats found on disk, or None if the agent is not installed. Each agent is read
# from every root recall found for it (see roots.py); pass `found` to reuse a discovery.
def agents(found=None):
    from ... import roots
    from . import claude, codex, cursor

    found = roots.discover() if found is None else found
    by_agent = {a: [r for r in found if r.agent == a] for a in roots.AGENTS}
    cl, cx, cu = by_agent["claude"], by_agent["codex"], by_agent["cursor"]
    return {
        "claude": ("Claude Code", lambda: claude.detect(cl), {"claude": lambda: claude.items(cl)}),
        "codex": ("Codex", lambda: codex.detect(cx), {"codex": lambda: codex.items(cx)}),
        "cursor": (
            "Cursor",
            lambda: cursor.detect(cu),
            {"cursor": lambda: cursor.transcript_items(cu), "cursor-legacy": lambda: cursor.legacy_items(cu)},
        ),
    }


def all_sources(selected: list[str] | None = None, found=None) -> dict:
    """Item sources for the selected agents; none selected means every agent."""
    out = {}
    for name, (_, _, sources) in agents(found).items():
        if not selected or name in selected:
            out.update(sources)
    return out

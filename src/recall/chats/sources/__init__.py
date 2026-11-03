"""Readers for each agent's on-disk chat history.

Each source yields Items: a stable key, a cheap fingerprint (so unchanged chats
are skipped on re-index) and a loader that parses the chat only when needed.
"""

import contextlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from ..transcript import Message, Session
from . import compressed

CHAT_SCHEMA = "2026-10-15 parts replace schema 7"


class Incomplete(Exception):
    """Raised by a source after its items when it could not list them all: what it yielded was not
    a full read, so the indexer does not record one."""


@dataclass
class Item:
    key: str
    fingerprint: str
    load: Callable[[], Session | None]


def open_ro(path: str, timeout: float = 2.0) -> sqlite3.Connection | None:
    """Another agent's SQLite file, read-only (`mode=ro`, never `immutable`: that would ignore the WAL, which
    holds the newest rows). None when it cannot be opened."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout, check_same_thread=False)
        con.execute("pragma query_only = on")
        return con
    except sqlite3.Error:
        return None


def table_names(con: sqlite3.Connection) -> set[str]:
    with contextlib.suppress(sqlite3.Error):
        return {r[0] for r in con.execute("select name from sqlite_master where type in ('table', 'view')")}
    return set()


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
    main = {
        "claude": ("Claude Code", lambda: claude.detect(cl), {"claude": lambda: claude.items(cl)}),
        "codex": ("Codex", lambda: codex.detect(cx), {"codex": lambda: codex.items(cx)}),
        "cursor": (
            "Cursor",
            lambda: cursor.detect(cu),
            {"cursor": lambda: cursor.transcript_items(cu), "cursor-legacy": lambda: cursor.legacy_items(cu)},
        ),
    }
    return {**main, **other_agents()}


# Agents recall reads beside the three above, one module per format family (see docs/agents.md). A module
# lists its agents in AGENTS: key -> (display name, detect, items). detect() and items() take no
# arguments: each agent's folders come from roots.other_dirs(key). Their chats carry the agent's key as
# Session.root, which keeps them out of `recall dream` unless [roots.<key>] mine = true.
FAMILIES = ("antigravity", "copilot", "droid", "gemini", "grok", "kimi", "opencode", "pi")


def family_modules() -> list:
    """The family modules that exist. A name in FAMILIES with no file yet is skipped; a module that is
    there but does not import is an error, not a skip."""
    import importlib

    out = []
    for name in FAMILIES:
        try:
            out.append(importlib.import_module(f"{__name__}.{name}"))
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{name}":
                raise
    return out


def other_agents() -> dict:
    out = {}
    for module in family_modules():
        for key, (label, detect, items) in module.AGENTS.items():
            out[key] = (label, detect, {key: items})
    return out


def all_sources(selected: list[str] | None = None, found=None) -> dict:
    """Item sources for the selected agents; none selected means every agent."""
    out = {}
    for name, (_, _, sources) in agents(found).items():
        if not selected or name in selected:
            out.update(sources)
    return out

"""Synthetic stores for the agents recall reads beyond Claude Code, Codex and Cursor.

Each family module here has `FIXTURES`: agent key -> Fixture. The stores are built from the agents'
documented formats (their own repos' test fixtures where there are any), never from a real chat, and are
written under a sandbox HOME by docs/benchmarks/setup_eval.py's `agents` scenarios.
"""

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Fixture:
    build: Callable[[str], None]  # write the agent's store under this HOME (two chats, one with `query`)
    query: str  # a word only the first chat holds, so a search for it must find that chat
    project: str | None = None  # the folder the first chat is attributed to, when the agent records one


def all_fixtures() -> dict[str, Fixture]:
    out: dict[str, Fixture] = {}
    for info in pkgutil.iter_modules(__path__):
        out.update(importlib.import_module(f"{__name__}.{info.name}").FIXTURES)
    return out

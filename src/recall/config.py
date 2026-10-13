"""Settings, kept in config.toml in recall's data folder. `recall setup` writes it;
editing it by hand is fine too."""

import json
import os
import tomllib

from . import paths

DEFAULTS = {
    "sources": [],  # agents to import from; empty means every agent found
    "model": {},  # provider, name, and for some providers base_url / api_key / effort
    "embeddings": {},  # optional semantic search: provider, name, base_url; see embeddings.py
    "dream": {"max_minutes": 120, "max_calls": 80},
    "contexts": {},  # name -> folders, e.g. work = ["~/Projects/Acme"]; see contexts.py
    "usage_log": True,  # recall logs its own use to usage.jsonl; see usage.py
    # When an agent edits a file another chat has open changes to: ask, worktree, tell or off
    # per situation. These are the recommended rules; see status.py and `recall setup`.
    "coordination": {"same_copy": "ask", "conflict": "tell", "apart": "tell", "overlap": "off"},
    # Exploration subagents' reports are always recorded; serve = true also hands the matching
    # earlier report to a new exploration at its start. Off: no measured gain; see explore.py.
    "explore": {"serve": False},
    # Which chats produced which commits, kept in recall's index on this machine and never
    # written to a repo; see commits.py. false stops linking and drops the stored links.
    "links": {"enabled": True},
}


def load() -> dict:
    try:
        with open(paths.CONFIG, "rb") as f:
            stored = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        stored = {}
    # A table written as a plain value (contexts = 3) falls back to its default instead of
    # breaking every command, the session-start hook among them.
    return {
        k: (
            {**v, **(t if isinstance(t := stored.get(k, {}), dict) else {})}
            if isinstance(v, dict)
            else stored.get(k, v)
        )
        for k, v in DEFAULTS.items()
    }


def save(cfg: dict) -> None:
    os.makedirs(paths.HOME, exist_ok=True)
    lines = ["# recall settings. Run `recall setup` to change them interactively, or edit here.", ""]
    for key, value in cfg.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml(value)}")
    for key, table in cfg.items():
        if isinstance(table, dict) and table:
            lines += ["", f"[{key}]"]
            lines += [f"{k} = {_toml(v)}" for k, v in table.items() if v not in (None, "")]
    with open(paths.CONFIG, "w") as f:
        f.write("\n".join(lines) + "\n")


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string

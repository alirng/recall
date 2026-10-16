"""Settings, kept in config.toml in recall's data folder. `recall setup` writes it;
editing it by hand is fine too."""

import json
import os
import re
import tomllib

from . import paths

DEFAULTS = {
    "sources": [],  # agents to import from; empty means every agent found
    "model": {},  # provider, name, and for some providers base_url / api_key / effort
    "embeddings": {},  # optional semantic search: provider, name, base_url; see embeddings.py
    # max_calls caps a plan's or local model's calls; on an API key max_usd (dollars) caps a run.
    "dream": {"max_minutes": 120, "max_calls": 80, "max_usd": 1.0},
    "contexts": {},  # name -> folders, e.g. work = ["~/Projects/Acme"]; see contexts.py
    # connect = true: recall keeps every Claude Code, Codex and Cursor folder connected (see autopilot.py).
    # Unset until `recall setup` asks, once.
    "autopilot": {},
    # Per harness root (see roots.py): [roots.claude-work] connect / index / mine = true or false.
    "roots": {},
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
    # On battery the nightly run and background jobs wait for power ("defer"); "run" lets them run.
    # See power.py.
    "power": {"on_battery": "defer"},
}


def load() -> dict:
    return load_checked()[0]


def load_checked() -> tuple[dict, bool]:
    """load(), and whether config.toml could be read. A missing file reads as empty (True); one
    that exists but cannot be read or parsed gives the defaults and False, so a caller whose
    output depends on a setting can tell "not set" from "could not read"."""
    readable = True
    try:
        with open(paths.CONFIG, "rb") as f:
            stored = tomllib.load(f)
    except FileNotFoundError:
        stored = {}
    except (OSError, tomllib.TOMLDecodeError):
        stored, readable = {}, False
    # A table written as a plain value (contexts = 3) falls back to its default instead of
    # breaking every command, the session-start hook among them.
    return {
        k: (
            {**v, **(t if isinstance(t := stored.get(k, {}), dict) else {})}
            if isinstance(v, dict)
            else stored.get(k, v)
        )
        for k, v in DEFAULTS.items()
    }, readable


def save(cfg: dict) -> None:
    os.makedirs(paths.HOME, exist_ok=True)
    lines = ["# recall settings. Run `recall setup` to change them interactively, or edit here.", ""]
    for key, value in cfg.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml(value)}")
    for key, table in cfg.items():
        if isinstance(table, dict) and table:
            plain = {k: v for k, v in table.items() if not isinstance(v, dict) and v not in (None, "")}
            if plain or not any(isinstance(v, dict) for v in table.values()):
                lines += ["", f"[{key}]"]
                lines += [f"{k} = {_toml(v)}" for k, v in plain.items()]
            for sub, values in table.items():  # a table of tables: [roots.claude-work]
                if isinstance(values, dict) and values:
                    lines += ["", f"[{key}.{_key(sub)}]"]
                    lines += [f"{k} = {_toml(v)}" for k, v in values.items() if v not in (None, "")]
    with open(paths.CONFIG, "w") as f:
        f.write("\n".join(lines) + "\n")


def _key(name) -> str:
    return str(name) if re.fullmatch(r"[A-Za-z0-9_-]+", str(name)) else json.dumps(str(name))


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string

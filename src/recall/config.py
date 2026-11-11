"""Settings, kept in config.toml in recall's data folder. `recall setup` writes it;
editing it by hand is fine too."""

import contextlib
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
    # Grok Bot's chat cache (chats/sources/grokbot.py): read only when enabled = true (`recall setup` asks);
    # skip = ["bot name or id", ...] are bots never read.
    "grokbot": {"enabled": False, "skip": []},
    # wake = false: a message to an idle chat waits for its next turn instead of waking it (wake.py).
    # inherit_permissions = true: a closed chat woken by a message keeps your own permission settings.
    # wake_open_sessions = true: also start a turn in an open Claude Code chat, with that chat's own permissions.
    "messaging": {"wake": True, "wake_open_sessions": False, "inherit_permissions": False},
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


def update(key: str, value) -> None:
    """Set one setting (a whole table, such as [coordination]) and save, leaving the rest of
    config.toml as it was: unlike load(), nothing is filled in from the defaults. Refuses to save
    over a config.toml it cannot read, which would lose the settings in it."""
    try:
        with open(paths.CONFIG, "rb") as f:
            stored = tomllib.load(f)
    except FileNotFoundError:
        stored = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"config.toml can't be read ({e}); fix or remove it first") from e
    stored[key] = value
    save(stored)


def save(cfg: dict) -> None:
    """Write config.toml in one step: a temp file beside it, then a rename, so a hook reading the
    settings mid-save sees the old file or the new one, never half of it. The text is read back before
    the rename and must equal what was asked to be saved (empty values and tables aside, which are
    left out); otherwise nothing is written and ValueError says so. A new file is readable only by you."""
    import tempfile

    want = _clean(cfg, top=True)
    lines = ["# recall settings. Run `recall setup` to change them interactively, or edit here.", ""]
    for key, value in want.items():
        if not isinstance(value, dict):
            lines.append(f"{_key(key)} = {_toml(value)}")
    for key, table in want.items():
        if isinstance(table, dict):
            _emit([key], table, lines)
    text = "\n".join(lines) + "\n"
    try:
        if tomllib.loads(text) != want:
            raise ValueError("it would not read back the same")
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise ValueError(f"config.toml not saved: {e}; the file is unchanged") from e
    os.makedirs(paths.HOME, exist_ok=True)
    try:
        mode = os.stat(paths.CONFIG).st_mode & 0o777
    except OSError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=paths.HOME, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, paths.CONFIG)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _clean(value, top: bool = False):
    """The settings as they are written: no None, no empty strings inside a table, no empty tables,
    tuples as lists. What save() checks the written file against."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = _clean(v)
            if v is None or (v == "" and not top) or (isinstance(v, dict) and not v):
                continue
            out[str(k)] = v
        return out
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _emit(path: list, table: dict, lines: list) -> None:
    """A table and, below it, its sub-tables at any depth: [roots.claude-work.extra]."""
    plain = {k: v for k, v in table.items() if not isinstance(v, dict)}
    if plain:
        lines += ["", "[" + ".".join(_key(p) for p in path) + "]"]
        lines += [f"{_key(k)} = {_toml(v)}" for k, v in plain.items()]
    for sub, values in table.items():
        if isinstance(values, dict):
            _emit([*path, sub], values, lines)


def _key(name) -> str:
    return str(name) if re.fullmatch(r"[A-Za-z0-9_-]+", str(name)) else json.dumps(str(name))


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):  # inside an array: an inline table
        return "{" + ", ".join(f"{_key(k)} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string

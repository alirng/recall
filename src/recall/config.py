"""Settings, kept in config.toml in recall's data folder. `recall setup` writes it;
editing it by hand is fine too."""

import json
import os
import tomllib

from . import paths

DEFAULTS = {
    "sources": [],  # agents to import from; empty means every agent found
    "model": {},  # provider, name, and for some providers base_url / api_key / effort
    "dream": {"max_minutes": 120, "max_calls": 80},
}


def load() -> dict:
    try:
        with open(paths.CONFIG, "rb") as f:
            stored = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        stored = {}
    return {k: ({**v, **stored.get(k, {})} if isinstance(v, dict) else stored.get(k, v)) for k, v in DEFAULTS.items()}


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

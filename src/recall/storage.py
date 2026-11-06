"""Every place recall keeps things on this machine, inside and outside its data folder, each with a
plain label, its size and a kind (see KINDS), for the Storage panel and a later "Remove recall"; and
removing the copies recall kept before an upgrade, the only items an app may delete.

Each file is marked on its own, never a whole folder: cache/ holds files that can be rebuilt next to
ones that cannot (mail, edits).
"""

import contextlib
import os
import re
import shutil
import subprocess
import sys

from . import paths

KINDS = {
    "data": "lost if deleted",
    "rebuildable": "recall makes it again",
    "running": "exists only while something runs",
    "settings": "choices you made",
    "backup": "a copy kept before an upgrade",
}


def _home(path: str) -> str:
    return path.replace(os.path.expanduser("~"), "~", 1)


def _size(path: str) -> int:
    total = 0
    try:
        if os.path.isfile(path) or os.path.islink(path):
            return os.lstat(path).st_size
        with os.scandir(path) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    total += _size(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return total


# What each file under recall's data folder is (kinds: see KINDS).
_HOME_ITEMS = {
    "recall.db": ("Chat index and memory tables (chat copies, forgotten list, memory runs)", "data"),
    "recall.db-wal": ("Chat index journal", "running"),
    "recall.db-shm": ("Chat index shared memory", "running"),
    "recall-vectors": ("Search vectors", "rebuildable"),
    "usage.jsonl": ("Log of how recall was used", "data"),
    "config.toml": ("Settings", "settings"),
    "autopilot.json": ("Autopilot state", "settings"),
    "evals": ("Your search and memory evals", "data"),
    "deferred.json": ("Jobs waiting for power", "running"),
    "catch-up.stamp": ("Last catch-up run", "running"),
    "running": ("Sockets and locks while recall runs", "running"),
    "logs": ("Logs", "running"),
    "cache": ("Caches", None),
    "backups": ("Backups", None),
}
_CACHE_ITEMS = {
    "edits.jsonl": ('Record of edits agents made, for "which chat wrote this"', "data"),
    "mail.jsonl": ("Messages between chats", "data"),
    "mail-tokens.json": ("Message tokens", "data"),
    "mail-read.json": ("Which messages were read", "data"),
    "edit-index.json": ("Edit index", "rebuildable"),
    "edit-warnings.json": ("Edit warnings already shown", "rebuildable"),
    "rc-scans.json": ("Scan of agent rule files", "rebuildable"),
    "status.json": ("Status cache", "rebuildable"),
    "jit": ("Memory handed to sessions", "rebuildable"),
    "embed-ok": ("Embedding check", "rebuildable"),
    "embed-warm": ("Embedding warm-up", "rebuildable"),
}


def _item(label, path, kind, bytes_=None, **more) -> dict:
    exists = os.path.lexists(path) if path else False
    return {
        "label": label,
        "path": _home(path) if path else None,
        "bytes": (_size(path) if bytes_ is None and exists else bytes_) or 0,
        "kind": kind,
        "exists": exists,
    } | more


def _home_items() -> list[dict]:
    items = []
    try:
        names = sorted(os.listdir(paths.HOME))
    except OSError:
        return items
    for name in names:
        full = os.path.join(paths.HOME, name)
        label, kind = _HOME_ITEMS.get(name, (None, None))
        if name.endswith(".lock"):
            label, kind = "Lock file", "running"
        elif name.startswith("config.toml."):
            items.append(_backup(name, full))
            continue
        elif label is None:
            label, kind = f"Other: {name}", "data"  # unknown files count as yours until recall says otherwise
        if name == "cache":
            items += _folder_items(full, _CACHE_ITEMS, "rebuildable", "cache")
        elif name == "backups":
            items += [_backup(e, os.path.join(full, e)) for e in sorted(os.listdir(full))]
        elif name == "logs":
            items += [_item(f"Log: {e}", os.path.join(full, e), "running") for e in sorted(os.listdir(full))]
        elif name == "running":
            items += [_item(f"Running: {e}", os.path.join(full, e), "running") for e in sorted(os.listdir(full))]
        else:
            items.append(_item(label, full, kind))
    return items


_BACKUPS = (  # what upgrades leave behind, by file name: label, then the version the name gives
    (re.compile(r"recall-(\d[\w.]*?)-(\d{4}-\d\d-\d\d)\.db$"), "Old index copy", "{0}"),
    (re.compile(r"Recall-menubar-before-([\w.]+?)\.app$"), "Old menu bar app", "before {0}"),
    (re.compile(r"config\.toml\.before-(.+)$"), "Old settings copy", "before {0}"),
    (re.compile(r"config\.toml\..+$"), "Old settings copy", None),
)


def _backup(name: str, path: str) -> dict:
    """A copy kept before an upgrade, labelled for what it is, with the version and date its name gives
    (the date it was made otherwise)."""
    from datetime import datetime

    label, version, day = f"Backup: {name}", None, None
    for pattern, what, form in _BACKUPS:
        m = pattern.match(name)
        if m:
            label = what
            version = form.format(*m.groups()) if form else None
            day = m.group(2) if m.re.groups > 1 else None
            break
    when = None
    with contextlib.suppress(ValueError):
        when = datetime.strptime(day, "%Y-%m-%d") if day else None
    if when is None:
        with contextlib.suppress(OSError):
            when = datetime.fromtimestamp(os.lstat(path).st_mtime).replace(hour=0, minute=0, second=0, microsecond=0)
    # ISO 8601 with an offset, as every time the API gives; a day with no time of day reads as midnight.
    return _item(label, path, "backup", version=version, date=when.astimezone().isoformat() if when else None)


def _folder_items(folder: str, table: dict, other_kind: str, name: str) -> list[dict]:
    out = []
    for e in sorted(os.listdir(folder)):
        label, kind = table.get(e) or (
            ("Lock file", "running") if e.endswith(".lock") else (f"Other in {name}/: {e}", "data")
        )
        out.append(_item(label, os.path.join(folder, e), kind))
    return out


def _installs() -> list[dict]:
    """The recall program as installed, wherever it was put: the command on PATH or in
    ~/.local/bin, and the folder it lives in."""
    found, seen = [], set()
    for cand in (os.path.expanduser("~/.local/bin/recall"), shutil.which("recall")):
        if not cand or not os.path.lexists(cand):
            continue
        real = os.path.realpath(cand)
        if real in seen:
            continue
        seen.add(real)
        found.append(_item("The recall command", cand, "rebuildable", bytes_=0))
        tool = os.path.dirname(os.path.dirname(real))  # .../tools/recall-agents/bin/recall -> .../recall-agents
        if os.path.basename(tool) != "bin" and os.path.isdir(tool):
            found.append(_item("The installed recall program", tool, "rebuildable"))
    return found


def _keychain() -> dict:
    """Whether recall has a key in the Keychain. Asks for the item's attributes only, never its secret."""
    from . import keys

    present = False
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", keys.SERVICE], capture_output=True, timeout=10
            )
            present = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "label": "Model API key in the Keychain",
        "path": "macOS Keychain",
        "bytes": 0,
        "kind": "settings",
        "exists": present,
    }


def _outside_items() -> list[dict]:
    home = os.path.expanduser("~")
    items = [
        _item("Your notes, a git repo", paths.MEMORY, "data", history_bytes=_size(os.path.join(paths.MEMORY, ".git")))
    ]
    items += _installs()
    agents = os.path.join(home, "Library", "LaunchAgents")
    try:
        plists = sorted(f for f in os.listdir(agents) if f.startswith(("dev.recall.", "com.rererecall.")))
    except OSError:
        plists = []
    items += [_item(f"Background job: {f[:-6]}", os.path.join(agents, f), "settings") for f in plists]
    for app in ("~/Applications/Recall.app", "/Applications/Recall.app"):
        if os.path.exists(os.path.expanduser(app)):
            items.append(_item("The menu bar app", os.path.expanduser(app), "rebuildable"))
    items.append(
        _item(
            "Menu bar app settings (an earlier build)",
            os.path.join(home, "Library/Preferences/dev.recall.app.plist"),
            "settings",
        )
    )
    items.append(
        _item("Scratch files from an earlier build", os.path.join(home, "Library/Caches/recall"), "rebuildable")
    )
    if sys.platform == "darwin":
        items.append(_keychain())
    return [i for i in items if i["exists"] or i["path"] == "macOS Keychain"]  # the Keychain item says present or not


def inventory() -> dict:
    items = _home_items() + _outside_items()
    totals: dict[str, int] = {}
    for i in items:
        totals[i["kind"]] = totals.get(i["kind"], 0) + i["bytes"]
    return {"items": items, "totals": totals, "total_bytes": sum(totals.values()), "kinds": KINDS}


def remove(chosen: list[str]) -> dict:
    """Delete copies recall kept before an upgrade. `chosen` are paths as inventory() shows them. Only
    an item that is tagged backup now and lies inside recall's data folder is removed; if any chosen
    path is not one, nothing is removed. A symlink is removed itself, never what it points to."""
    backups = {i["path"]: i for i in _home_items() if i["kind"] == "backup" and i["exists"]}
    refused = [c for c in chosen if c not in backups]
    if refused:
        raise ValueError(f"not a copy recall kept before an upgrade: {refused[0]}")
    home = os.path.realpath(paths.HOME)
    targets = []
    for shown in dict.fromkeys(chosen):
        full = os.path.expanduser(shown)
        folder = os.path.realpath(os.path.dirname(full))
        if os.path.commonpath([folder, home]) != home:
            raise ValueError(f"outside recall's data folder: {shown}")
        targets.append((shown, full, backups[shown]["bytes"]))
    removed = []
    for shown, full, size in targets:
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.unlink(full)
        removed.append({"path": shown, "bytes": size})
    return {"removed": removed, "bytes": sum(r["bytes"] for r in removed)}

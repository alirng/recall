"""Autopilot: keep recall connected to every harness root, once the person has said yes.

Someone who works across Claude Code, Codex and Cursor, or across a personal and a work account,
ends up with several config folders (see roots.py). Each needs recall's MCP server, its hooks and
its skill, and a new account or an agent update can leave one without. With `[autopilot]
connect = true` in config.toml, a cheap check finds roots that are new or missing a part and
installs it with the same code `recall install` uses, backing each file up first.

  nightly      `recall dream` runs the check before anything else
  session      the session-start hook looks (a few file reads) and, if something is missing, starts
               `recall autopilot` in the background; the hook itself never installs. This fires in
               Claude Code and Codex, whose SessionStart hook recall installs. Cursor's
               sessionStart is documented as fire-and-forget, so Cursor roots rely on the nightly run.

Rules: with the setting off, nothing is changed and nothing is written. A root that a shell rc names
but that does not exist yet (an alias for a work account that has not been launched) is left alone
and connected after its folder appears. Chat history is never written. Each change is one line in
usage.jsonl, and the last ones are kept in autopilot.json for `recall doctor`.
"""

import contextlib
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time

from . import config, paths, roots, usage
from .integrations import agents

STATE = os.path.join(paths.HOME, "autopilot.json")
LOCK = os.path.join(paths.HOME, "autopilot.lock")
PARTS = {"mcp": "MCP server", "hook": "hooks", "skill": "skill"}
RETRY_AFTER = 3600  # a hook starts a background run at most this often when a part stays missing


def enabled() -> bool:
    return config.load()["autopilot"].get("connect") is True


def set_enabled(on: bool) -> None:
    cfg = config.load()
    cfg["autopilot"] = {**cfg["autopilot"], "connect": on}
    config.save(cfg)


# --- what is connected (read-only) ------------------------------------------------------------


def report() -> list[dict]:
    """One row per root: its parts, account hint and when it was last indexed. Reads the index
    read-only, so it never creates or migrates it."""
    found = roots.discover()
    indexed = _indexed()
    try:
        cmd = agents._recall_bin()
    except SystemExit:
        cmd = None
    rows = []
    for r in found:
        have = agents.parts(r, cmd) if r.exists else {}
        rows.append(
            {
                "root": r.name,
                "agent": r.agent,
                "path": r.path,
                "data": r.data,
                "via": r.via,
                "exists": r.exists,
                "account": roots.account(r) if r.exists else None,
                "parts": have,
                "connected": bool(have) and all(have.values()),
                "needs_trust": r.agent == "codex" and bool(have.get("hook")),
                **indexed.get(r.name, {}),
            }
        )
    return rows


def _indexed() -> dict[str, dict]:
    """root -> {chats, last_chat, indexed_at}, from the index opened read-only."""
    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return out
    with contextlib.closing(con):
        try:
            for name, n, last in con.execute("select root, count(*), max(updated_at) from sessions group by root"):
                if name:
                    out[name] = {"chats": n, "last_chat": last}
            for key, at in con.execute("select k, v from meta where k like 'indexed_at_root:%'"):
                out.setdefault(key.split(":", 1)[1], {})["indexed_at"] = float(at)
        except sqlite3.Error:
            pass
    return out


def recent(n: int = 5) -> list[str]:
    """The last changes autopilot made, newest last, for `recall doctor`."""
    return [f"{c['at'].replace('T', ' ')}  {_describe(c)}" for c in _load().get("changes", [])[-n:]]


def summary_line(rows: list[dict]) -> str:
    real = [r for r in rows if r["exists"]]
    ok = sum(1 for r in real if r["connected"])
    waiting = len(rows) - len(real)
    text = f"roots: {ok} of {len(real)} connected" + (f", {waiting} waiting for first launch" if waiting else "")
    return text + ("" if ok == len(real) else " (recall doctor)")


# --- the check --------------------------------------------------------------------------------


def run(trigger: str = "manual", dry_run: bool = False, log=None) -> list[dict]:
    """Connect what is missing, if autopilot is on. Returns the changes made (or, with dry_run,
    that would be). Never raises; a failure is a line in the log, not a crash in a hook or the nightly run."""
    if not enabled():
        return []
    os.makedirs(paths.HOME, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []  # another run is on it
        try:
            return _run(trigger, dry_run, log or (lambda _: None))
        except Exception as e:
            usage.record("autopilot", "error", client="autopilot", note=repr(e)[:200], trigger=trigger)
            if log:
                log(f"error: {e!r}")
            return []


def _run(trigger: str, dry_run: bool, log) -> list[dict]:
    state = _load()
    first_run = "roots" not in state
    known = state.setdefault("roots", {})
    failed = state.setdefault("failed", {})
    now = time.time()
    changes = []
    try:
        cmd = agents._recall_bin()
    except SystemExit:
        cmd = None
    found = roots.discover()
    for r in found:
        entry = known.get(r.name)
        hint = roots.account(r) if r.exists else None
        if entry is None and not first_run:
            changes.append(_note(r, "found", trigger, how=r.via, exists=r.exists))
        elif entry and not entry.get("exists", True) and r.exists:
            changes.append(_note(r, "appeared", trigger, how=r.via))  # a named folder was created
        elif entry and entry.get("account") and hint and entry["account"] != hint:
            changes.append(_note(r, "account", trigger, was=entry["account"], now=hint))
        known[r.name] = {
            "agent": r.agent,
            "path": r.path,
            "exists": r.exists,
            "account": hint or (entry or {}).get("account"),
            "first_seen": (entry or {}).get("first_seen", now),
            "connected_at": (entry or {}).get("connected_at"),
        }
    if cmd:
        for r in agents.connectable(found):
            before = agents.parts(r, cmd)
            missing = [p for p, ok in before.items() if ok is False]
            if not missing:
                continue
            if not dry_run:
                try:
                    agents.connect_root(r, cmd, tuple(missing), False)
                except Exception as e:  # a malformed config file must not stop the other roots
                    failed.setdefault(r.name, {})["error"] = repr(e)[:120]
            after = agents.parts(r, cmd) if not dry_run else before
            files = agents.root_files(r)
            for part in missing:
                path = files["hook" if part == "hook" else "mcp" if part == "mcp" else "skills"]
                if dry_run:
                    changes.append(_note(r, "would connect", trigger, part=part, file=path, log=False))
                elif after.get(part):
                    changes.append(_note(r, "connect", trigger, part=part, file=path))
                    failed.get(r.name, {}).pop(part, None)
                elif failed.get(r.name, {}).get(part) != "failed":
                    failed.setdefault(r.name, {})[part] = "failed"
                    changes.append(_note(r, "connect", trigger, part=part, file=path, ok=False))
            if not dry_run and all(v for v in agents.parts(r, cmd).values()):
                for same in found:  # a Cursor build shares its folder with the others
                    if same.agent == r.agent and os.path.realpath(same.path) == os.path.realpath(r.path):
                        known[same.name]["connected_at"] = now
    state["checked_at"] = now
    state["changes"] = (state.get("changes", []) + [c for c in changes if c["tool"] != "would connect"])[-40:]
    if not dry_run:
        _save(state)
    for c in changes:
        log(_describe(c))
    return changes


def _note(r, tool: str, trigger: str, log: bool = True, **extra) -> dict:
    change = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "root": r.name, "trigger": trigger, **extra}
    if log:
        extra = {k: v for k, v in change.items() if k not in ("at", "tool", "root")}
        usage.record("autopilot", tool, {"root": r.name}, client="autopilot", **extra)
    return change


def _describe(c: dict) -> str:
    if c["tool"] in ("connect", "would connect"):
        what = PARTS[c["part"]]
        state = "" if c.get("ok", True) else " FAILED"
        return f"{c['root']}: {c['tool']} {what}{state} ({c['file']})"
    if c["tool"] == "found":
        return f"{c['root']}: new root ({c['how']})" + ("" if c["exists"] else ", not created yet")
    if c["tool"] == "appeared":
        return f"{c['root']}: folder created, connecting"
    return f"{c['root']}: account changed"


# --- when it runs -----------------------------------------------------------------------------


def on_session_start(e) -> None:
    """Session-start handler (hooks.HANDLERS): look, never install. When a root is missing a part,
    start `recall autopilot` in the background, at most once an hour."""
    try:
        if not enabled():
            return None
        state = _load()
        if time.time() - state.get("spawned_at", 0) < RETRY_AFTER:
            return None
        cmd = agents._recall_bin()
        if all(all(v for v in agents.parts(r, cmd).values()) for r in agents.connectable(roots.discover())):
            return None
        state["spawned_at"] = time.time()
        _save(state)
        env = {**os.environ, "RECALL_BIN": cmd}
        subprocess.Popen(
            [sys.executable, "-m", "recall", "autopilot", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except (Exception, SystemExit):  # recall not on PATH ends in sys.exit; a hook must not
        pass
    return None


def _load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    os.makedirs(paths.HOME, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)

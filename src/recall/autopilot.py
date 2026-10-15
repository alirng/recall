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

Rules:
  * With the setting off, nothing is changed and nothing is written.
  * The yes covers the hook kinds this release installs (hooks.consent_key), recorded in
    autopilot.json when the person says it. A kind a later release adds is not installed on its own:
    `recall doctor` lists it as "new hooks available" and `recall install` takes it, once.
  * A folder is written to only after the agent has used it (roots.Root.exists): an alias for a work
    account that has not signed in yet is left alone and connected after its folder appears.
  * [roots.NAME] connect = false keeps one root out; `recall uninstall --root NAME` sets it.
  * A hook that already runs an existing recall is left as it is (agents._reconcile), so a recall run
    from a repo never takes hooks over from the installed one.
  * Chat history is never written. Each change is one line in usage.jsonl, and the last ones are
    kept in autopilot.json for `recall doctor`, along with every root recall connected and the files
    it wrote, so `recall uninstall` reaches a root that is no longer discovered.
"""

import contextlib
import fcntl
import json
import os
import sys
import time

from . import config, paths

STATE = os.path.join(paths.HOME, "autopilot.json")
LOCK = os.path.join(paths.HOME, "autopilot.lock")
PARTS = {"mcp": "MCP server", "hook": "hooks", "skill": "skill"}
RETRY_AFTER = 3600  # a hook starts a background run at most this often when a part stays missing
AGENT_NAMES = ("claude", "codex", "cursor")


def enabled() -> bool:
    return config.load()["autopilot"].get("connect") is True


def set_enabled(on: bool) -> None:
    """Turn autopilot on or off. Turning it on is the person's yes to the hook kinds this release
    installs: they are recorded as the set autopilot may repair."""
    cfg = config.load()
    cfg["autopilot"] = {**cfg["autopilot"], "connect": on}
    config.save(cfg)
    if on:
        grant_consent()


# --- consent: which hooks the yes covers ----------------------------------------------------------


def all_kinds() -> list[str]:
    from .integrations import agents

    return [k for agent in AGENT_NAMES for k in agents.hook_kinds(agent)]


def consented() -> set[str] | None:
    """The hook kinds the person said yes to; None when no yes is recorded."""
    kinds = _load().get("consent", {}).get("hooks")
    return set(kinds) if isinstance(kinds, list) else None


def grant_consent(kinds: list[str] | None = None, extend: bool = False) -> None:
    """Record the yes: every hook kind this release installs (or `kinds`). extend adds to a yes
    already given, which is what a person's own `recall install` does."""
    state = _load()
    kinds = set(all_kinds() if kinds is None else kinds)
    if extend:
        kinds |= set(state.get("consent", {}).get("hooks") or [])
    state["consent"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "hooks": sorted(kinds)}
    _save(state)


# --- what is connected (read-only) ------------------------------------------------------------


def report(deep: bool = True) -> list[dict]:
    """One row per root: its parts, state, account hint and when it was last indexed. Reads the index
    read-only, so it never creates or migrates it. deep=False skips the sign-in lookups, one of
    which opens Cursor's multi-GB database: `recall status`, which a menu bar polls, leaves them out."""
    from . import roots
    from .integrations import agents

    found = roots.discover()
    indexed = _indexed()
    cfg = config.load()
    on = cfg["autopilot"].get("connect") is True
    consent = consented() if on else None
    try:
        cmd = agents._recall_bin()
    except SystemExit:
        cmd = None
    rows = []
    for r in found:
        live = r.exists and (r.agent != "cursor" or os.path.isdir(r.path))
        have = agents.parts(r, cmd, consent) if live else {}
        connect = roots.option(r, "connect", cfg)
        new = []
        if live and cmd and consent is not None:
            new = [k for k in agents.hook_kinds(r.agent) if k not in consent]
            new = agents._hook_gaps(r, cmd, set(new)) if new else []
        approval = agents.cursor_approval(r) if live and r.agent == "cursor" and have.get("mcp") else None
        rows.append(
            {
                "root": r.name,
                "agent": r.agent,
                "path": r.path,
                "data": r.data,
                "via": r.via,
                "exists": r.exists,
                "account": roots.account(r) if r.exists and deep else None,
                "parts": have,
                "connected": bool(have) and all(have.values()),
                "needs_trust": r.agent == "codex" and bool(have.get("hook")),
                "needs_approval": bool(approval) and approval["approved"] == 0,
                "approval": approval,
                "connect": connect,
                "mine": roots.option(r, "mine", cfg),
                "index": roots.option(r, "index", cfg),
                "new_hooks": new,
                "has_cursor_config": os.path.isdir(r.path) if r.agent == "cursor" else None,
                **indexed.get(r.name, {}),
            }
        )
    return rows


def state_of(row: dict) -> str:
    """connected, trust (Codex has not been told to run the hooks), approval (Cursor has not been told
    to load the server), missing, skipped or waiting: what `recall doctor` and the summary say."""
    if not row["exists"]:
        return "waiting"
    if not row["connect"]:
        return "skipped"
    if row["agent"] == "cursor" and not row["has_cursor_config"]:
        return "missing"
    if not row["connected"]:
        return "missing"
    if row["needs_trust"]:
        return "trust"
    if row["needs_approval"]:
        return "approval"
    return "connected"


def _indexed() -> dict[str, dict]:
    """root -> {chats, last_chat, indexed_at}, from the index opened read-only."""
    import sqlite3

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
    """`roots: 2 of 4 connected, 1 waiting for Codex trust ...`: a root counts as connected only when the
    agent can use it. Codex hooks need a one-time trust in /hooks and Cursor's server a one-time approval
    per project, neither of which recall can give or see."""
    states = [state_of(r) for r in rows]
    live = [s for s in states if s != "waiting"]
    ok = live.count("connected")
    extra = [
        f"{n} {what}"
        for n, what in (
            (states.count("trust"), "awaiting Codex trust"),
            (states.count("approval"), "awaiting Cursor approval"),
            (states.count("skipped"), "skipped by you"),
            (states.count("waiting"), "waiting for first launch"),
        )
        if n
    ]
    text = f"roots: {ok} of {len(live)} connected" + "".join(f", {x}" for x in extra)
    return text + ("" if ok == len(live) else " (recall doctor)")


# --- the check --------------------------------------------------------------------------------


def run(trigger: str = "manual", dry_run: bool = False, log=None) -> list[dict]:
    """Connect what is missing, if autopilot is on. Returns the changes made (or, with dry_run,
    that would be, writing nothing at all). Never raises; a failure is a line in the log, not a crash
    in a hook or the nightly run."""
    if not enabled():
        return []
    from . import usage

    if dry_run:
        return _run(trigger, True, log or (lambda _: None))
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


@contextlib.contextmanager
def stopped(turn_off: bool = True, wait: float = 60):
    """Hold autopilot still while something removes recall: turn it off (so nothing connects again
    afterwards), wait for a run in progress to end, and keep its lock until the caller is done."""
    if turn_off and enabled():
        set_enabled(False)
    os.makedirs(paths.HOME, exist_ok=True)
    with open(LOCK, "w") as lock:
        end = time.time() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > end:
                    raise TimeoutError("an autopilot run is still going; try again in a minute") from None
                time.sleep(0.3)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _wanted(r, cfg: dict | None = None) -> bool:
    """Still on, and the root not switched off, read fresh: a person who runs `recall autopilot off`
    or `recall uninstall` while a run is going stops it at the next root."""
    from . import roots

    cfg = cfg or config.load()
    return cfg["autopilot"].get("connect") is True and roots.option(r, "connect", cfg)


def _spec(r) -> dict:
    return {
        "agent": r.agent,
        "name": r.name,
        "path": r.path,
        "data": r.data,
        "via": r.via,
        "default": r.default,
        "by_env": r.by_env,
    }


def record_connected(found: list, cmd: str | None = None) -> None:
    """Note every root that has any part of recall in it, whoever put it there (autopilot, or the
    person's own `recall install`), so `recall uninstall` can still reach it after discovery loses it."""
    from .integrations import agents

    state = _load()
    connected = state.setdefault("connected", {})
    for r in agents.connectable(found):
        if any(agents.parts(r, cmd).values()):
            connected[r.name] = {"spec": _spec(r), "files": agents.root_files(r), "at": time.time()}
    _save(state)


def recorded_roots(found: list) -> list:
    """The roots autopilot (or `recall install`) connected that discovery no longer finds: an alias
    taken out of a shell rc, a variable unset. Their folders may still hold recall's files."""
    from . import roots

    known = {(r.agent, os.path.realpath(r.path)) for r in found}
    out = []
    for name, entry in _load().get("connected", {}).items():
        spec = entry.get("spec") or {}
        try:
            key = (spec["agent"], os.path.realpath(spec["path"]))
            if key not in known and os.path.isdir(spec["path"]):
                known.add(key)
                out.append(
                    roots.Root(
                        spec["agent"],
                        name,
                        spec["path"],
                        spec.get("data"),
                        spec.get("via", "default"),
                        exists=True,
                        default=bool(spec.get("default")),
                        by_env=bool(spec.get("by_env")),
                    )
                )
        except (KeyError, TypeError):
            continue
    return out


def forget(names: list[str] | None = None) -> None:
    """Drop the record of connected roots (all of them, or `names`) once recall is removed from them;
    with all of them, the hook consent goes too."""
    state = _load()
    if names is None:
        state.pop("connected", None)
        state.pop("consent", None)
    else:
        for n in names:
            state.get("connected", {}).pop(n, None)
    _save(state)


def _run(trigger: str, dry_run: bool, log) -> list[dict]:
    from . import roots
    from .integrations import agents

    state = _load()
    first_run = "roots" not in state
    known = state.setdefault("roots", {})
    failed = state.setdefault("failed", {})
    connected = state.setdefault("connected", {})
    consent = consented()
    allow = consent if consent is not None else set()  # no yes recorded: MCP and skill only
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
            changes.append(_note(r, "found", trigger, log=not dry_run, how=r.via, exists=r.exists))
        elif entry and not entry.get("exists", True) and r.exists:
            changes.append(_note(r, "appeared", trigger, log=not dry_run, how=r.via))  # the agent has used it now
        elif entry and entry.get("account") and hint and entry["account"] != hint:
            changes.append(_note(r, "account", trigger, log=not dry_run, was=entry["account"], now=hint))
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
            if not _wanted(r):
                continue
            before = agents.parts(r, cmd, allow)
            missing = [p for p, ok in before.items() if ok is False]
            if missing:
                if not dry_run:
                    try:
                        agents.connect_root(r, cmd, tuple(missing), False, allow=allow)
                    except Exception as e:  # a malformed config file must not stop the other roots
                        failed.setdefault(r.name, {})["error"] = repr(e)[:120]
                after = agents.parts(r, cmd, allow) if not dry_run else before
                files = agents.root_files(r)
                for part in missing:
                    path = files["hook" if part == "hook" else "mcp" if part == "mcp" else "skills"]
                    if dry_run:
                        changes.append(_note(r, "would connect", trigger, log=False, part=part, file=path))
                    elif after.get(part):
                        changes.append(_note(r, "connect", trigger, part=part, file=path))
                        failed.get(r.name, {}).pop(part, None)
                    elif failed.get(r.name, {}).get(part) != "failed":
                        failed.setdefault(r.name, {})[part] = "failed"
                        changes.append(_note(r, "connect", trigger, part=part, file=path, ok=False))
            have = agents.parts(r, cmd, allow)
            if not dry_run and any(have.values()):
                connected[r.name] = {"spec": _spec(r), "files": agents.root_files(r), "at": now}
            if not dry_run and all(v for v in have.values()):
                for same in found:  # a Cursor build shares its folder with the others
                    if same.agent == r.agent and os.path.realpath(same.path) == os.path.realpath(r.path):
                        known[same.name]["connected_at"] = now
    state["checked_at"] = now
    state["changes"] = (state.get("changes", []) + [c for c in changes if c["tool"] != "would connect"])[-40:]
    if not dry_run:
        _save({**_load(), **{k: v for k, v in state.items() if k != "consent"}})  # a yes given meanwhile stays
    for c in changes:
        log(_describe(c))
    return changes


def _note(r, tool: str, trigger: str, log: bool = True, **extra) -> dict:
    change = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "root": r.name, "trigger": trigger, **extra}
    if log:
        from . import usage

        extra = {k: v for k, v in change.items() if k not in ("at", "tool", "root")}
        usage.record("autopilot", tool, {"root": r.name}, client="autopilot", **extra)
    return change


def _describe(c: dict) -> str:
    if c["tool"] in ("connect", "would connect"):
        what = PARTS[c["part"]]
        state = "" if c.get("ok", True) else " FAILED"
        return f"{c['root']}: {c['tool']} {what}{state} ({c['file']})"
    if c["tool"] == "found":
        return f"{c['root']}: new root ({c['how']})" + ("" if c["exists"] else ", waiting for first launch")
    if c["tool"] == "appeared":
        return f"{c['root']}: the agent has used it, connecting"
    return f"{c['root']}: account changed"


# --- when it runs -----------------------------------------------------------------------------


def on_session_start(e) -> None:
    """Session-start handler (hooks.HANDLERS): look, never install. When a root is missing a part,
    start `recall autopilot` in the background, at most once an hour."""
    try:
        cfg = config.load()
        if cfg["autopilot"].get("connect") is not True:
            return None
        state = _load()
        if time.time() - state.get("spawned_at", 0) < RETRY_AFTER:
            return None
        import subprocess

        from . import roots
        from .integrations import agents

        cmd = agents._recall_bin()
        allow = consented() or set()
        todo = [r for r in agents.connectable(roots.discover()) if roots.option(r, "connect", cfg)]
        if all(all(v for v in agents.parts(r, cmd, allow).values()) for r in todo):
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

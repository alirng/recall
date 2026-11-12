"""`recall api`: an HTTP + JSON door to this recall instance, for the Mac app and the menu bar.

It listens on a unix socket (running/api.sock in recall's data folder, mode 0600; no TCP) and answers
requests under /v1. GET reads. A few writes (editing and forgetting notes, the coordination rules, the
model, removing copies kept before an upgrade) go through recall's own modules, the same code the CLI
runs, and all pass through change() here. Live updates are server-sent events (/v1/events) fed by
kqueue file events.

At rest it costs nothing: no timer loops, no polling. The one timer is the idle exit, armed when the
last request or event stream ends. File watchers run only while an event stream is open.
Clients start it on demand and it quits by itself, so nothing needs to be installed or kept running.
See docs/api.md.
"""

import asyncio
import contextlib
import fcntl
import json
import os
import plistlib
import re
import select
import socket
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__, paths

API_VERSION = 1
WRITES = ["notes", "coordination", "model", "storage", "imports"]  # what this server can change; /v1/info says so
DEFAULT_IDLE = 300  # seconds with no request and no open event stream before the server exits
RUNNING = os.path.join(paths.HOME, "running")  # files that exist only while recall runs
SOCKET = os.path.join(RUNNING, "api.sock")
SUN_PATH_MAX = 103  # macOS limit on a unix socket path
COALESCE = 2.0  # seconds a burst of file events waits before one "changed" event goes out
SSE_PING = 3600  # sse-starlette pings every 15 s unless told otherwise; an open stream should not wake the process


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra  # extra: more error fields


# --- the database thread ------------------------------------------------------------
# sqlite connections belong to the thread that opened them, so every index read runs on this one.

_db = ThreadPoolExecutor(1, thread_name_prefix="recall-db")
_con_cache: list = []


def _schema_ok() -> bool:
    """The index is there and its chat tables are this code's. One built by another release is left to
    `recall index`: the API never rebuilds them."""
    import sqlite3

    from .chats import store

    if not os.path.exists(paths.DB):
        return False
    try:
        ro = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        row = ro.execute("select v from meta where k='chat_schema'").fetchone()
        ro.close()
    except sqlite3.Error:
        row = None
    return bool(row) and row[0] == store.chat_schema_version()


def _con():
    """The index connection. Never rebuilds the chat tables: if the stored schema is not this
    code's, the API says so and leaves the index to `recall index`."""
    if _con_cache:
        return _con_cache[0]
    from .chats import store

    if not os.path.exists(paths.DB):
        raise ApiError(503, "no_index", "there is no index yet: run `recall index`")
    if not _schema_ok():
        raise ApiError(503, "index_needs_rebuild", "the index was built by another release: run `recall index`")
    _con_cache.append(store.connect())
    return _con_cache[0]


# --- new chats before a search ------------------------------------------------------
# The MCP server brings the index up to date before each tool call, and a search here does the same with
# the same indexer.Refresh, so the app finds what an agent would.

FRESH_WAIT = 1.0
_refresher: list = []  # made on first use: the indexer is imported only when a search needs it


def _fresh(wait: float = FRESH_WAIT) -> None:
    """New chats into the index before a search, as an agent's tool call gets them. Never on an index
    another release built."""
    if not _schema_ok():
        return
    if not _refresher:
        from .chats import indexer

        _refresher.append(
            indexer.Refresh(lambda e: print(f"recall api: index refresh failed: {_safe(repr(e))}", file=sys.stderr))
        )
    _refresher[0](wait)


def _indexing() -> bool:
    return bool(_refresher) and _refresher[0].busy()


async def on_db(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_db, fn, *args)


async def off_db(fn, *args):
    return await asyncio.to_thread(fn, *args)


# --- small helpers ------------------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    from datetime import datetime

    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _day(text: str | None) -> str | None:
    """A date or a "YYYY-MM-DD HH:MM" local time as ISO 8601 with an offset. A bare date has no time
    of day, so it reads as midnight local time."""
    from datetime import datetime

    if not text:
        return None
    text = str(text).strip()
    for form in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, form).astimezone().isoformat(timespec="seconds")
        except ValueError:
            continue
    return text  # already carries an offset, or is not a time


def _num(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw))) if raw not in (None, "") else default
    except ValueError as e:
        raise ApiError(400, "bad_parameter", f"not a number: {raw!r}") from e


def _opt(request, name: str) -> str | None:
    return request.query_params.get(name) or None


def _window(value: str | None) -> float | None:
    from .chats import store

    try:
        return store.parse_since(value)
    except ValueError as e:
        raise ApiError(400, "bad_window", str(e)) from e


_ttl: dict[str, tuple[float, object]] = {}


def _cached(key: str, seconds: float, make):
    """A recent answer, for the few endpoints that cost a second or more to build."""
    hit = _ttl.get(key)
    if hit and time.monotonic() - hit[0] < seconds:
        return hit[1]
    value = make()
    _ttl[key] = (time.monotonic(), value)
    return value


def _safe(text) -> str:
    """An error message as a client may see it: credentials cut out, as the indexer does for chats."""
    from .chats.redact import redact

    return redact(str(text))


def _home(path: str) -> str:
    return path.replace(os.path.expanduser("~"), "~", 1)


def _live(con, alias: str = "s") -> str:
    """SQL (starting with "and") that leaves out chats `recall forget` removed, whose rows an older
    release may still have written into the index. Empty when the index has no such list."""
    has = con.execute("select 1 from sqlite_master where name = 'forgotten_chats'").fetchone()
    return f" and {alias}.id not in (select id from forgotten_chats)" if has else ""


# --- instance -----------------------------------------------------------------------


def info() -> dict:
    return {
        "recall": __version__,
        "api": API_VERSION,
        "instance": {"name": "This Mac" if sys.platform == "darwin" else socket.gethostname(), "kind": "local"},
        "pid": os.getpid(),
        "started_at": _iso(STARTED),
        "idle_exit_seconds": STATE.idle if STATE else None,
        "live_updates": hasattr(select, "kqueue"),
        "writes": WRITES,
        "home": _home(paths.HOME),
        "memory": _home(paths.MEMORY),
    }


STARTED = time.time()


# --- status, stats, storage, runs, config -------------------------------------------


_needs_sig: list = []  # the Needs-you signature of the last status computed, for the "status" event
STATUS_EVERY = 300  # seconds: the most often the event stream recomputes status


def _needs(s: dict) -> tuple:
    """What the Needs-you list is made of: the overlaps that matter now and the servers to restart."""
    return (
        tuple(sorted((o["repo"], o["file"], tuple(o["sessions"]), o["level"], o["situation"]) for o in s["overlaps"])),
        tuple(sorted(x["pid"] for x in s["outlived"])),
        s.get("level"),
    )


def status_view(since: str, force: bool = False) -> dict:
    def make():
        from . import autopilot, power, status
        from .chats import store
        from .chats.sources import codex, compressed

        s = status.status(store.parse_since(since))
        if n := codex.unreadable():
            s["unreadable"] = {"agent": "codex", "chats": n, "fix": compressed.fix_hint()}
        s["outlived"] = status.outlived()
        s["power"] = {"on_battery": power.on_battery(), "deferred": power.deferred()}
        out = {**s, "roots": autopilot.report(deep=False)}
        _needs_sig[:] = [_needs(out)]
        return out

    if force:
        _ttl.pop(f"status:{since}", None)
    return _cached(f"status:{since}", 15, make)


def stats_view() -> dict:
    from .chats import indexer, store
    from .chats.sources import codex, compressed

    con = _con()
    by_agent = store.stats(con)
    if _live(con):  # chats counted here exclude the forgotten ones
        shown = {
            r["agent"]: r
            for r in con.execute(
                "select agent, count(*) n, sum(n_messages) m, max(updated_at) last from sessions s"
                f" where 1 {_live(con)} group by agent"
            )
        }
        for agent in list(by_agent):
            r = shown.get(agent)
            if r is None:
                del by_agent[agent]
            else:
                by_agent[agent] |= {"chats": r["n"], "messages": r["m"], "latest": store.fmt_time(r["last"])}
    for agent, row in by_agent.items():
        row["latest"] = _day(row.get("latest"))
        latest = con.execute(
            "select id, title, project_name, updated_at from sessions s where agent = ? and n_messages > 0"
            f" and origin is not 'subagent' {_live(con)} order by updated_at desc limit 1",
            (agent,),
        ).fetchone()
        row["latest_chat"] = (
            {"chat_id": latest["id"], "title": latest["title"], "project": latest["project_name"]} if latest else None
        )
    out = {
        "agents": by_agent,
        "chats": sum(r["chats"] for r in by_agent.values()),
        "messages": sum(r["messages"] or 0 for r in by_agent.values()),
        "notes": len(_notes()[0]),
    }
    if n := codex.unreadable():
        out["unreadable"] = {"agent": "codex", "chats": n, "fix": compressed.fix_hint()}
    if parked := indexer.parked():
        out["not_indexed"] = {"chats": len(parked), "retry": "recall index --force"}
    return out


def storage_view() -> dict:
    from . import storage

    return _cached("storage", 30, storage.inventory)


_COUNTS_SKIP = {"batches", "batched_calls", "cost_usd"}


def _run_row(r) -> dict:
    from .memory import runs

    try:
        stats = json.loads(r["stats"] or "{}")
    except ValueError:
        stats = {}
    status = runs.status(r)
    return {
        "id": r["id"],
        "started": _iso(r["started"]),
        "finished": _iso(r["finished"]),
        "seconds": round(r["finished"] - r["started"], 1) if r["finished"] and r["started"] else None,
        "status": status,
        "backend": r["backend"],
        "chats_read": r["chats"],
        "candidates": r["candidates"],
        "model_calls": r["llm_calls"],
        "tokens_in": r["tokens_in"],
        "tokens_out": r["tokens_out"],
        "cost_usd": stats.get("cost_usd"),
        "batches": stats.get("batches"),
        "counts": {k: v for k, v in stats.items() if k not in _COUNTS_SKIP and isinstance(v, int)},
        "error": r["error"] or (runs.STOPPED if status == "crashed" else None),
    }


_STEP = re.compile(r"^(\d\d:\d\d:\d\d) (.*)$")
_STEP_KINDS = (
    ("commit links", "commit links"),
    ("model:", "model"),
    ("reading", "read"),
    ("read ", "read"),
    ("consolidating", "consolidate"),
    ("sent ", "batch"),
    ("batch ", "batch"),
    ("memory:", "memory"),
    ("cost:", "cost"),
)


def run_log(started: float | None, run_id: int | None = None) -> dict:
    """A run's log lines and its phases, dated from the run record. The log is plain text with
    times of day only, so both are read out of text: a line recall words differently later falls
    under kind "other" and a phase with no lines is left out. A run marks where its lines start
    (dream.run); for one from before that mark, the last run the log shows."""
    from datetime import datetime, timedelta

    from .integrations import scheduler

    empty = {"steps": [], "phases": []}
    try:
        with open(scheduler.LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return empty
    mark = f" run {run_id} started"
    marked = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].endswith(mark)), None)
    start = marked
    if start is None:
        start = next(
            (i for i in range(len(lines) - 1, -1, -1) if _STEP.match(lines[i]) and " model: " in lines[i]), None
        )
        if start is None:
            return empty
        if start and lines[start - 1][9:].startswith("commit links"):
            start -= 1
    day = datetime.fromtimestamp(started) if started else datetime.now()
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    steps, last = [], None
    for line in lines[start:]:
        m = _STEP.match(line)
        if not m:
            continue
        clock = datetime.strptime(m[1], "%H:%M:%S").time()
        at = datetime.combine(day.date(), clock)
        if last and at < last:  # past midnight
            day += timedelta(days=1)
            at = datetime.combine(day.date(), clock)
        last = at
        text = m[2].strip()
        kind = next((k for prefix, k in _STEP_KINDS if text.lstrip().startswith(prefix)), "other")
        steps.append({"time": at.astimezone().isoformat(timespec="seconds"), "kind": kind, "text": text, "_at": at})
    phases = _phases(steps)
    for st in steps:
        st.pop("_at")
    return {"steps": steps, "phases": phases}


def _phases(steps: list[dict]) -> list[dict]:
    """commit links, read, consolidate and commit notes, each from its first line to the start of the
    next. Consolidate ends at the last batch answer and commit notes at the "memory:" line, which is
    written once the notes are on disk and committed; with no batch the two meet."""

    def first(*kinds):
        return next((s for s in steps if s["kind"] in kinds), None)

    def last(*kinds):
        return next((s for s in reversed(steps) if s["kind"] in kinds), None)

    def iso(t):
        return t.astimezone().isoformat(timespec="seconds")

    model, reading, done = first("model"), first("read"), last("read")
    folding, memory, cost = first("consolidate"), first("memory"), last("cost")
    batch_end = next((s for s in reversed(steps) if s["kind"] == "batch" and "answered" in s["text"]), None)
    links = first("commit links")
    out = []

    def add(name, a, b, result):
        if a and b:
            out.append({"name": name, "started": iso(a["_at"]), "finished": iso(b["_at"]), "result": result})

    if links and model:
        add("commit links", links, model, links["text"])
    if reading:
        add(
            "read",
            reading,
            done if done and done is not reading else reading,
            done["text"] if done else reading["text"],
        )
    if folding and memory:
        end = batch_end if batch_end and batch_end["_at"] >= folding["_at"] else memory
        add("consolidate", folding, end, f"{sum(1 for s in steps if s['kind'] == 'consolidate')} projects")
        add("commit notes", end, memory, memory["text"])
    if cost and memory and cost is not memory:
        out[-1]["result"] += " · " + cost["text"]
    return out


def runs_view(limit: int, with_steps: bool = False) -> dict:
    con = _con()
    try:
        rows = con.execute("select * from dream_runs order by id desc limit ?", (limit,)).fetchall()
    except Exception:
        rows = []
    runs = [_run_row(r) for r in rows]
    out: dict = {"runs": runs}
    if with_steps:
        out["latest"] = (runs[0] | run_log(rows[0]["started"], rows[0]["id"])) if runs else None
    return out


def schedule_view() -> dict:
    from .integrations import scheduler

    cfg = _config()["dream"]
    out = {"limits": cfg, "kind": None, "enabled": False, "time": None, "loaded": None}
    if sys.platform == "darwin":
        out["kind"] = "launchd"
        if os.path.exists(scheduler.PLIST):
            try:
                with open(scheduler.PLIST, "rb") as f:
                    at = plistlib.load(f)["StartCalendarInterval"]
                out["time"] = f"{at['Hour']:02d}:{at['Minute']:02d}"
                out["enabled"] = True
                r = subprocess.run(
                    ["launchctl", "print", f"gui/{os.getuid()}/{scheduler.LABEL}"], capture_output=True, timeout=10
                )
                out["loaded"] = r.returncode == 0
            except (OSError, KeyError, plistlib.InvalidFileException, subprocess.SubprocessError):
                pass
    elif sys.platform.startswith("linux"):
        out["kind"] = "systemd"
        out["enabled"] = os.path.exists(os.path.join(scheduler.SYSTEMD, "recall-dream.timer"))
    return out


def _config() -> dict:
    from . import config

    return config.load()


def _key_home(ref) -> dict:
    """Where an API key lives, never the key itself."""
    from . import keys

    if not ref:
        return {"configured": False, "stored_in": None}
    ref = str(ref)
    if ref == "keychain":
        where = keys.storage_name()
    elif ref == "file":
        where = _home(paths.KEYS)
    elif ref.startswith("env:"):
        where = f"environment variable {ref[4:]}"
    else:
        where = "config.toml"  # a key written into the file itself: say so, do not show it
    return {"configured": True, "stored_in": where}


def config_view() -> dict:
    cfg = _config()
    m, e = cfg["model"], cfg["embeddings"]
    model = {k: m.get(k) for k in ("provider", "name", "server", "batch", "fallback", "consolidate", "effort")}
    model |= {"key": _key_home(m.get("api_key")), "local": m.get("provider") == "local"}
    return {
        "model": model,
        "embeddings": {"provider": e.get("provider"), "name": e.get("name"), "on": bool(e.get("provider"))},
        "dream": cfg["dream"],
        "schedule": schedule_view(),
        "power": cfg["power"],
        "usage_log": cfg["usage_log"],
        "links": cfg["links"],
        "explore": cfg["explore"],
        "contexts": sorted(cfg["contexts"]),
    }


def coordination_view() -> dict:
    from . import status

    now = status.rules()
    return {
        "preset": status.preset_of(now),
        "rules": [
            {
                "situation": k,
                "meaning": text,
                "action": now[k],
                "default": status.PRESETS["recommended"][0][k],
            }
            for k, text in status.SITUATIONS.items()
        ],
        "actions": list(status.ACTIONS),
        "presets": {k: {"rules": v[0], "meaning": v[1]} for k, v in status.PRESETS.items()},
    }


def doctor_view() -> dict:
    def make():
        from . import autopilot, power
        from .chats import indexer
        from .chats.sources import codex
        from .memory import runs

        rows = autopilot.report()
        for r in rows:
            r["state"] = autopilot.state_of(r)
        on = autopilot.enabled()
        return {
            "roots": rows,
            "autopilot": {
                "on": on,
                "consented": on and autopilot.consented() is not None,
                "recent": autopilot.recent(),
            },
            "parked_chats": len(indexer.parked()),
            "power": power.summary(),
            "notes": [n for n in (codex.unreadable_note(),) if n] + runs.health(),
        }

    return _cached("doctor", 30, make)


# --- notes --------------------------------------------------------------------------

_notes_cache: list = []


def _notes() -> tuple[dict, str]:
    """Every note (archived ones included), re-read only when a note file changed."""
    from .memory.edit import load_notes
    from .memory.notes import NoteStore

    if not os.path.isdir(paths.MEMORY):
        return {}, ""
    store = NoteStore()
    digest = store.digest()
    if _notes_cache and _notes_cache[0] == digest:
        return _notes_cache[1], digest
    notes = load_notes(store, include_archived=True)
    _notes_cache[:] = [digest, notes]
    return notes, digest


def _archived(n) -> bool:
    return os.sep + "archive" + os.sep in n.path.replace(paths.MEMORY, "", 1)


def _gist(text: str, n: int = 160) -> str:
    text = " ".join((text or "").replace("#", " ").split())
    return text[:n]


def _note_row(n) -> dict:
    return {
        "note_id": n.id,
        "type": n.type,
        "scope": n.scope,
        "project": n.project_name,
        "title": n.title,
        "gist": _gist(n.body),
        "confidence": n.confidence,
        "status": n.status,
        "archived": _archived(n),
        "created": _day(n.created),
        "updated": _day(n.updated),
        "last_seen": _day(n.last_seen),
        "sources": len(n.sources),
        "rules": len(n.active_rules),
        "said_by": n.said_by,
        "edited_by_hand": n.edited_by_hand,
    }


def notes_view(type_, project, status, scope, limit, offset, sort) -> dict:
    notes, _ = _notes()
    rows = list(notes.values())
    facets = {
        "types": _count(n.type for n in rows),
        "scopes": _count(n.scope for n in rows),
        "statuses": _count(n.status for n in rows),
        "projects": _count(n.project_name for n in rows if n.project_name),
    }
    if status != "all":
        rows = [n for n in rows if n.status == status and not _archived(n)]
    if type_:
        rows = [n for n in rows if n.type == type_]
    if scope:
        rows = [n for n in rows if n.scope == scope]
    if project:
        rows = [n for n in rows if n.project_name == project]
    keys = {
        "updated": lambda n: (n.updated or n.last_seen or "", n.title),
        "last_seen": lambda n: (n.last_seen or "", n.title),
        "created": lambda n: (n.created or "", n.title),
    }
    if sort == "title":
        rows.sort(key=lambda n: n.title.lower())
    else:
        rows.sort(key=keys.get(sort, keys["updated"]), reverse=True)
    return {
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "notes": [_note_row(n) for n in rows[offset : offset + limit]],
        "facets": facets,
    }


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", paths.MEMORY, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _rules_at(commit: str, rel: str) -> tuple[dict, list[str], str | None]:
    """The note file as a commit had it: its rules by id, its related-note ids, and whether it
    existed. Read straight from the file's frontmatter, so nothing of the working copy leaks in."""
    from .memory import notes as n

    text = _git("show", f"{commit}:{rel}")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text or "", re.S)
    if not m:
        return {}, [], None
    try:
        meta = n._frontmatter(m.group(1))
    except Exception:
        return {}, [], None
    rules = {r.id: r for r in map(n._rule_from, meta.get("rules") or []) if r}
    related = [i.strip() for i in re.findall(r"\[\[([^\]|#]+)", m.group(2).partition(n.RELATED_MARK)[2])]
    return rules, related, text


def _changes(new: tuple, old: tuple | None) -> list[dict]:
    """What one commit did to one note, from its rules before and after."""
    rules, related, _ = new
    out = []
    if old is None or old[2] is None:
        return [{"kind": "created", "rule_id": r.id, "text": r.text} for r in rules.values()] or [
            {"kind": "created", "rule_id": None, "text": ""}
        ]
    before = old[0]
    for rid, r in rules.items():
        b = before.get(rid)
        if b is None:
            out.append({"kind": "added", "rule_id": rid, "text": r.text})
        elif b.status == "active" and r.status != "active":
            out.append({"kind": "retired", "rule_id": rid, "text": r.text})
        elif b.text != r.text:
            out.append({"kind": "refined", "rule_id": rid, "text": r.text})
        elif (b.last_seen, len(b.sources)) != (r.last_seen, len(r.sources)):
            out.append({"kind": "confirmed", "rule_id": rid, "text": r.text})
    if set(related) != set(old[1]):
        out.append({"kind": "relinked", "rule_id": None, "text": f"{len(related)} related notes"})
    return out


def _history(path: str, limit: int = 30) -> list[dict]:
    """The note's commits, newest first, each with `changes`: the rules created, added, refined,
    retired or confirmed, and whether its links moved, found by comparing the file with its parent's."""
    rel = os.path.relpath(path, paths.MEMORY)
    log = _git("log", "--follow", f"--max-count={limit}", "--numstat", "--format=%H%x09%ct%x09%s", "--", rel)
    names = _git("log", "--follow", f"--max-count={limit}", "--name-only", "--format=%H", "--", rel)
    if log is None:
        return []
    where: dict[str, str] = {}  # commit -> the note's path in it (it moves between folders)
    commit = None
    for line in (names or "").splitlines():
        if len(line) == 40:
            commit = line
        elif line and commit:
            where[commit] = line
    out: list[dict] = []
    for line in log.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and len(parts[0]) == 40:
            out.append(
                {
                    "_full": parts[0],
                    "commit": parts[0][:10],
                    "date": _iso(float(parts[1])),
                    "subject": "\t".join(parts[2:]),
                }
            )
        elif len(parts) == 3 and out and parts[0].isdigit():
            out[-1] |= {"lines_added": int(parts[0]), "lines_removed": int(parts[1])}
    states = [_rules_at(h["_full"], where.get(h["_full"], rel)) for h in out]
    for i, h in enumerate(out):
        older = states[i + 1] if i + 1 < len(out) else None  # the oldest commit in the list created the file
        if older is None and len(out) == limit:
            older = (None, None, "")  # more history exists than we read: compare with the real parent below
            parent = _rules_at(h["_full"] + "^", where.get(h["_full"], rel))
            older = parent if parent[2] is not None else None
        h["changes"] = _changes(states[i], older)
        del h["_full"]
    return out


def note_view(note_id: str) -> dict:
    from .memory import links

    notes, _ = _notes()
    n = notes.get(note_id) or next((v for k, v in notes.items() if k.startswith(note_id)), None)
    if not n:
        raise ApiError(404, "no_such_note", f"no note {note_id!r}")
    everything = set(n.sources) | {s for r in n.rules for s in r.sources}
    found = {}
    if everything:
        con = _con()
        marks = ",".join("?" * len(everything))
        for r in con.execute(
            f"select id, agent, title, project_name, updated_at, n_messages from sessions s where id in ({marks})"
            + _live(con),
            list(everything),
        ):
            found[r["id"]] = {
                "chat_id": r["id"],
                "agent": r["agent"],
                "title": r["title"],
                "project": r["project_name"],
                "date": _iso(r["updated_at"]),
                "messages": r["n_messages"],
                "found": True,
            }

    def chat(i):
        return found.get(i) or {"chat_id": i, "found": False}

    return _note_row(n) | {
        "body": n.body,
        "stamp": n.stamp,
        "supersedes": n.supersedes,
        "superseded_by": n.superseded_by,
        "reach": n.reach,
        "path": _home(n.path),
        "source_chats": [chat(i) for i in n.sources],
        "rules_all": [
            {
                "id": r.id,
                "text": r.text,
                "status": r.status,
                "first_seen": _day(r.first_seen),
                "last_seen": _day(r.last_seen),
                "said_by": r.said_by,
                "evidence": r.evidence,
                "retired": _day(r.retired),
                "reason": r.reason or None,
                "replaced_by": r.replaced_by,
                "edited": _day(r.edited),
                "stated": _day(r.stated),
                "sources": r.sources,
            }
            for r in n.rules
        ],
        "related": [
            {
                "note_id": x["note_id"],
                "title": notes[x["note_id"]].title,
                "type": notes[x["note_id"]].type,
                "reason": x["reason"],
            }
            for x in links.neighbours(n, notes)
        ],
        "history": _history(n.path),
    }


# --- chats --------------------------------------------------------------------------


def _chat_shape(row: dict) -> dict:
    """Chat rows as the store makes them have a folder in `project` and times as local text; the API
    says `project` for the name everywhere, `folder` for the path, and ISO 8601 for every time."""
    row["folder"] = row.pop("project", None)
    row["project"] = row.pop("project_name", None)
    for key in ("updated", "started"):
        if key in row:
            row[key] = _day(row[key])
    return row


def _chat_row(r) -> dict:
    from .chats import store

    return _chat_shape(
        store.session_row(r)
        | {
            "project_name": r["project_name"],
            "started": store.fmt_time(r["started_at"]),
            "first_prompt": (r["first_prompt"] or "")[:200],
            "origin": r["origin"],
        }
    )


def chats_view(agent, project, since, until, limit, offset, subagents) -> dict:
    from .chats import store

    con = _con()
    try:
        where = (" and s.project_name = ?", [project]) if project else None
        fsql, fargs = store._filters(con, agent, None, since, until=until, where=where)
    except ValueError as e:
        raise ApiError(400, "bad_window", str(e)) from e
    skip = "" if subagents else "and s.origin is not 'subagent'"
    base = f"from sessions s where n_messages > 0 {skip} {fsql} {_live(con)}"
    total = con.execute(f"select count(*) {base}", fargs).fetchone()[0]
    rows = con.execute(f"select * {base} order by updated_at desc limit ? offset ?", [*fargs, limit, offset])
    return {"total": total, "limit": limit, "offset": offset, "chats": [_chat_row(r) for r in rows]}


def chat_view(chat_id, offset, limit, max_chars) -> dict:
    from .chats import store

    con = _con()
    sid = store.resolve_id(con, chat_id)
    if sid and con.execute(f"select 1 from sessions s where s.id = ? {_live(con)}", (sid,)).fetchone() is None:
        sid = None  # forgotten: as good as gone
    out = store.read(con, sid, None, offset, limit, max_chars=max_chars) if sid else None
    if not out:
        raise ApiError(404, "no_such_chat", f"no chat {chat_id!r}")
    out["project_name"] = (
        con.execute("select project_name from sessions where id = ?", (out["chat_id"],)).fetchone() or [None]
    )[0]
    for m in out["transcript"]:
        m["time"] = _day(m["time"])
    return _chat_shape(out)


def projects_view() -> dict:
    con = _con()
    rows = con.execute(
        "select project_name, count(*) chats, max(updated_at) last, count(distinct agent) agents,"
        " group_concat(distinct agent) names from sessions s where n_messages > 0 and origin is not 'subagent'"
        f" and project_name is not null and project_name != '' {_live(con)} group by project_name order by last desc"
    )
    notes, _ = _notes()
    n_notes = _count(n.project_name for n in notes.values() if n.project_name and n.status == "active")
    return {
        "projects": [
            {
                "name": r["project_name"],
                "chats": r["chats"],
                "notes": n_notes.get(r["project_name"], 0),
                "agents": sorted(r["names"].split(",")),
                "latest": _iso(r["last"]),
            }
            for r in rows
        ]
    }


# --- search -------------------------------------------------------------------------


def search_view(q, kind, agent, project, since, until, limit, client) -> dict:
    from . import usage
    from .chats import store
    from .memory import context

    started = time.perf_counter()
    con = _con()
    given = {
        "agent": agent,
        "project": project,
        "since": since,
        "until": until,
        "kind": kind if kind != "all" else None,
    }
    tags = [{"key": k, "value": v} for k, v in given.items() if v]
    for tag in tags:
        if tag["key"] in ("since", "until"):
            tag["resolved"] = _iso(_window(tag["value"]) if tag["key"] == "since" else store.parse_until(tag["value"]))
    out: dict = {"query": q, "filters": tags}
    try:
        if kind in ("all", "chats"):
            # The search an agent's search_chats runs, with its project filter: every folder a project
            # spans, or part of the folder or title. Forks come back folded, the others in `copies`.
            out["chats"] = store.search(con, q, agent, project, since, limit, until, where=(_live(con), []))
            names = {}
            if out["chats"]:
                marks = ",".join("?" * len(out["chats"]))
                ids = [r["chat_id"] for r in out["chats"]]
                names = dict(
                    con.execute(f"select id, project_name from sessions where id in ({marks})", ids).fetchall()
                )
            for r in out["chats"]:
                r["project_name"] = names.get(r["chat_id"])
                _chat_shape(r)
                for hit in r.get("hits") or ():
                    if isinstance(hit, dict) and "time" in hit:
                        hit["time"] = _day(hit["time"])
                for copy in r.get("copies") or ():
                    copy["updated"] = _day(copy["updated"])
            usage.record("api", "search", {"query": q, "agent": agent, "project": project}, out["chats"], client=client)
        if kind in ("all", "notes"):
            context.refresh_notes(con)
            found = context.search(con, q, project, limit)  # as memory_search: the project matched in the query
            out["notes"] = [
                {**r, "gist": _gist(r["body"]), "body": None, "last_seen": _day(r["last_seen"])} for r in found
            ]
            usage.record("api", "memory search", {"query": q, "project": project}, found, client=client)
    except ValueError as e:
        raise ApiError(400, "bad_window", str(e)) from e
    out["took_ms"] = usage.ms_since(started)
    return out


# --- writes -------------------------------------------------------------------------
# Each write is a call into recall's own modules (memory/edit.py, status.py, onboarding.py,
# storage.py), which the CLI uses too. These functions only find what the call needs.


def _note_path(note_id: str) -> str:
    notes, _ = _notes()
    n = notes.get(note_id)
    if not n:
        raise ApiError(404, "no_such_note", f"no note {note_id!r}")
    return n.path


def note_preview(note_id: str, text: str) -> dict:
    from .memory import edit

    return edit.preview(_note_path(note_id), text)


def reindex(notes: dict | None, digest: str | None) -> dict:
    """The notes search tables, brought up to date after a note changed (on the database thread)."""
    from .memory import edit

    started = time.perf_counter()
    with contextlib.suppress(ApiError):  # no index yet: nothing to refresh
        edit.reindex(_con(), notes, digest)
    return {"index_ms": round((time.perf_counter() - started) * 1000)}


def models_view() -> dict:
    from . import onboarding

    def make():
        cfg = _config()["model"]
        return {"options": onboarding.model_options(), "fallback": cfg.get("fallback"), "more": "recall model"}

    return _cached("models", 30, make)


def model_set(option: str, accept_destination: str | None = None) -> dict:
    from . import onboarding
    from .llm import FatalError, LLMError

    try:
        out = onboarding.use_model(option, accept_destination)
    except onboarding.NeedsConsent as e:
        raise ApiError(409, "needs_consent", str(e), destination=e.destination) from e
    except (FatalError, LLMError, ValueError) as e:
        raise ApiError(422, "test_failed", str(e)) from e
    finally:
        _ttl.pop("models", None)
    return out


def coordination_set(preset: str | None, rules: dict | None) -> dict:
    from . import status

    was = status.rules()
    status.set_rules(rules, preset)
    return coordination_view() | {"was": was}


def import_export(path: str) -> dict:
    from .chats.sources import exports

    return exports.run(path)


def storage_remove(chosen: list) -> dict:
    from . import storage

    try:
        return storage.remove([str(c) for c in chosen])
    finally:
        _ttl.pop("storage", None)


# --- live updates -------------------------------------------------------------------

_O_EVTONLY = 0x8000  # macOS: open for events only, so a watched file can sit on a volume being unmounted


def _head_sha() -> str | None:
    git = os.path.join(paths.MEMORY, ".git")
    try:
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            with open(os.path.join(git, head[5:])) as f:
                return f.read().strip()
        return head
    except OSError:
        return None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def fingerprints() -> dict:
    from .integrations import scheduler

    out = {"notes": _head_sha(), "config": _mtime(paths.CONFIG)}
    try:
        con = _con()
        out["chats"] = tuple(con.execute("select count(*), max(updated_at) from sessions").fetchone())
        try:
            # max(finished) too: a run's row is written as it starts, and one started by hand logs nowhere when it ends.
            row = con.execute("select max(id), max(finished) from dream_runs").fetchone()
            out["runs"] = (row[0], row[1], _mtime(scheduler.LOG))
        except Exception:
            out["runs"] = (None, _mtime(scheduler.LOG))
    except ApiError:
        pass
    return out


class Watcher:
    """kqueue on the files whose change means the app's view is stale. One kqueue fd is registered
    with the event loop, so the process sleeps until the kernel reports a change. A change starts
    a COALESCE-second window; one fingerprint check at its end decides which topics to announce."""

    def __init__(self, loop, announce):
        self.loop, self.announce = loop, announce
        self.kq = select.kqueue()
        self.fds: dict[int, str] = {}
        self.wanted = self._wanted()
        self.timer = None
        self.status_timer = None
        self.status_at = 0.0
        self.last: dict = {}
        self.flags = (
            select.KQ_NOTE_WRITE
            | select.KQ_NOTE_EXTEND
            | select.KQ_NOTE_DELETE
            | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_ATTRIB
        )
        for p in self.wanted:
            self._open(p)
        loop.add_reader(self.kq.fileno(), self._ready)

    @staticmethod
    def _wanted() -> list[str]:
        git = os.path.join(paths.MEMORY, ".git")
        db = paths.DB
        return [
            paths.HOME,
            db,
            db + "-wal",
            paths.CONFIG,
            paths.LOGS,
            os.path.join(paths.LOGS, "dream.log"),
            os.path.join(git, "refs", "heads"),
            os.path.join(git, "logs", "HEAD"),
        ]

    def _open(self, path: str) -> None:
        if path in self.fds.values():
            return
        try:
            fd = os.open(path, os.O_RDONLY | (_O_EVTONLY if sys.platform == "darwin" else 0))
        except OSError:
            return  # not there yet: its folder's event brings us back here
        ev = select.kevent(fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR, self.flags)
        self.kq.control([ev], 0, 0)
        self.fds[fd] = path

    def _ready(self) -> None:
        reopen = False
        for ev in self.kq.control(None, 64, 0):
            if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                os.close(ev.ident)
                self.fds.pop(ev.ident, None)
                reopen = True
        if reopen:
            for p in self.wanted:
                self._open(p)
        if self.timer is None:
            self.timer = self.loop.call_later(COALESCE, lambda: asyncio.ensure_future(self._settle()))

    async def prime(self) -> None:
        self.last = await on_db(fingerprints)

    async def _settle(self) -> None:
        self.timer = None
        now = await on_db(fingerprints)
        topics = [k for k, v in now.items() if self.last.get(k) != v]
        self.last = now
        if topics:
            self.announce(topics)
            if self.status_timer is None:  # one recompute per STATUS_EVERY, however many changes come
                wait = max(0.0, STATUS_EVERY - (time.monotonic() - self.status_at))
                self.status_timer = self.loop.call_later(wait, lambda: asyncio.ensure_future(self._status()))

    async def _status(self) -> None:
        """Announce "status" when the Needs-you result differs from the last one computed. The first
        computation only sets the baseline (a client's own /status call sets it too)."""
        self.status_timer = None
        self.status_at = time.monotonic()
        before = _needs_sig[0] if _needs_sig else None
        try:
            await off_db(status_view, "12h", True)
        except Exception:
            return
        if before is not None and _needs_sig[0] != before:
            self.announce(["status"])

    def close(self) -> None:
        self.loop.remove_reader(self.kq.fileno())
        for t in (self.timer, self.status_timer):
            if t:
                t.cancel()
        for fd in self.fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self.kq.close()


class Hub:
    def __init__(self):
        self.queues: set[asyncio.Queue] = set()
        self.watcher: Watcher | None = None

    async def join(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.queues.add(q)
        if self.watcher is None and hasattr(select, "kqueue"):
            self.watcher = Watcher(asyncio.get_running_loop(), self.announce)
            await self.watcher.prime()
        return q

    def leave(self, q) -> None:
        self.queues.discard(q)
        if not self.queues and self.watcher:
            self.watcher.close()
            self.watcher = None

    def announce(self, topics: list[str]) -> None:
        for q in self.queues:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"event": "changed", "data": json.dumps({"topics": topics, "at": _iso(time.time())})})


HUB = Hub()


# --- the app ------------------------------------------------------------------------


class Idle:
    """Counts requests and event streams in flight. At zero it arms the one timer this process
    owns; the next connection cancels it."""

    def __init__(self, seconds: float):
        self.idle, self.active, self.handle, self.stop = seconds, 0, None, None

    def arm(self) -> None:
        if self.idle and self.active == 0 and self.stop is not None:
            self.handle = asyncio.get_running_loop().call_later(self.idle, self._expire)

    def _expire(self) -> None:
        """The idle time is up. An index run a search started is left to finish rather than cut short."""
        if _indexing():
            self.handle = asyncio.get_running_loop().call_later(5, self._expire)
        elif self.stop is not None:
            self.stop.set()

    def enter(self) -> None:
        self.active += 1
        if self.handle:
            self.handle.cancel()
            self.handle = None

    def leave(self) -> None:
        self.active -= 1
        self.arm()


STATE: Idle | None = None


class Track:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or STATE is None:
            return await self.app(scope, receive, send)
        STATE.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            STATE.leave()


def build_app():
    from sse_starlette import EventSourceResponse
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def reply(data, status=200):
        return JSONResponse(data, status_code=status)

    async def h_root(request):
        return reply({"api": API_VERSION, "endpoints": ENDPOINTS})

    async def h_info(request):
        return reply(info())

    async def h_status(request):
        fresh = _opt(request, "fresh") in ("1", "true")
        return reply(await off_db(status_view, _opt(request, "since") or "12h", fresh))

    async def h_stats(request):
        return reply(await on_db(stats_view))

    async def h_storage(request):
        return reply(await off_db(storage_view))

    async def h_runs(request):
        return reply(await on_db(runs_view, _num(_opt(request, "limit"), 10, 1, 100), False))

    async def h_run_latest(request):
        return reply(await on_db(runs_view, 1, True))

    async def h_config(request):
        return reply(await off_db(config_view))

    async def h_coord(request):
        return reply(await off_db(coordination_view))

    async def h_doctor(request):
        return reply(await off_db(doctor_view))

    async def h_projects(request):
        return reply(await on_db(projects_view))

    async def h_notes(request):
        p = request.query_params
        args = (
            _opt(request, "type"),
            _opt(request, "project"),
            p.get("status", "active"),
            _opt(request, "scope"),
            _num(p.get("limit"), 50, 1, 500),
            _num(p.get("offset"), 0, 0, 10**7),
            p.get("sort", "updated"),
        )
        return reply(await on_db(notes_view, *args))

    async def h_note(request):
        return reply(await on_db(note_view, request.path_params["note_id"]))

    async def h_chats(request):
        p = request.query_params
        args = (
            _opt(request, "agent"),
            _opt(request, "project"),
            _opt(request, "since"),
            _opt(request, "until"),
            _num(p.get("limit"), 50, 1, 500),
            _num(p.get("offset"), 0, 0, 10**7),
            p.get("subagents") == "1",
        )
        return reply(await on_db(chats_view, *args))

    async def h_chat(request):
        p = request.query_params
        args = (
            request.path_params["chat_id"],
            _num(p.get("offset"), 0, 0, 10**7) if "offset" in p else None,
            _num(p.get("limit"), 30, 1, 200),
            _num(p.get("max_chars"), 6000, 200, 100000),
        )
        return reply(await on_db(chat_view, *args))

    async def h_search(request):
        p = request.query_params
        q = (p.get("q") or "").strip()
        if not q:
            raise ApiError(400, "missing_query", "q is required")
        kind = p.get("kind", "all")
        if kind not in ("all", "chats", "notes"):
            raise ApiError(400, "bad_parameter", "kind is all, chats or notes")
        client = usage_client(request)
        if kind != "notes":
            await off_db(_fresh)  # new chats first, as an agent's search gets them
        args = (
            q,
            kind,
            _opt(request, "agent"),
            _opt(request, "project"),
            _opt(request, "since"),
            _opt(request, "until"),
            _num(p.get("limit"), 10, 1, 50),
            client,
        )
        return reply(await on_db(search_view, *args))

    async def h_note_preview(request):
        body = await _json(request)
        return reply(await off_db(note_preview, request.path_params["note_id"], str(body.get("text") or "")))

    async def h_note_edit(request):
        from .memory import edit

        body = await _json(request)
        args = (request.path_params["note_id"], str(body.get("text") or ""), body.get("stamp"))
        return reply(await change(edit.edit, *args, notes=True))

    async def h_note_forget(request):
        from .memory import edit

        body = await _json(request)
        return reply(await change(edit.forget, request.path_params["note_id"], body.get("stamp"), notes=True))

    async def h_note_restore(request):
        from .memory import edit

        body = await _json(request)
        return reply(
            await change(edit.restore, request.path_params["note_id"], str(body.get("commit") or ""), notes=True)
        )

    async def h_coord_set(request):
        body = await _json(request)
        rules, preset = body.get("rules"), body.get("preset")
        if preset is not None and not isinstance(preset, str):
            raise ApiError(400, "bad_parameter", "preset is a string")
        if rules is not None and not (
            isinstance(rules, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in rules.items())
        ):
            raise ApiError(400, "bad_parameter", "rules is {situation: action}, both strings")
        if preset is None and not isinstance(rules, dict):
            raise ApiError(400, "bad_parameter", "send a preset, or rules: {situation: action}")
        return reply(await change(coordination_set, body.get("preset"), rules, topics=["config"]))

    async def h_models(request):
        return reply(await off_db(models_view))

    async def h_model_set(request):
        body = await _json(request)
        if not body.get("option"):
            raise ApiError(400, "bad_parameter", "option is one of /v1/models' option ids")
        accept = body.get("accept_destination")
        if accept is not None and not isinstance(accept, str):
            raise ApiError(400, "bad_parameter", "accept_destination is a string")
        return reply(await change(model_set, str(body["option"]), accept, topics=["config"]))

    async def h_storage_remove(request):
        body = await _json(request)
        chosen = body.get("paths")
        if not isinstance(chosen, list) or not chosen:
            raise ApiError(400, "bad_parameter", "paths is a list of /v1/storage paths tagged backup")
        return reply(await change(storage_remove, chosen))

    async def h_import(request):
        path = (await _json(request)).get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ApiError(400, "bad_parameter", "path is the absolute path of an export on this Mac")
        await on_db(_con)  # an index another release built is left to `recall index`
        return reply(await change(import_export, path, topics=["chats"]))

    async def h_events(request):
        q = await HUB.join()

        async def stream():
            try:
                yield {"event": "hello", "data": json.dumps({"api": API_VERSION, "live": hasattr(select, "kqueue")})}
                while True:
                    yield await q.get()
            finally:
                HUB.leave(q)

        return EventSourceResponse(stream(), ping=SSE_PING)

    async def on_error(request, exc):
        return reply({"error": {"code": exc.code, "message": _safe(exc.message), **exc.extra}}, exc.status)

    async def on_crash(request, exc):
        import traceback

        print(f"recall api: {request.method} {request.url.path}\n{_safe(traceback.format_exc())}", file=sys.stderr)
        return reply(
            {"error": {"code": "internal", "message": "something went wrong; the details are in api.log"}}, 500
        )

    routes = [
        Route("/v1", h_root),
        Route("/v1/info", h_info),
        Route("/v1/status", h_status),
        Route("/v1/stats", h_stats),
        Route("/v1/storage", h_storage),
        Route("/v1/runs", h_runs),
        Route("/v1/runs/latest", h_run_latest),
        Route("/v1/config", h_config),
        Route("/v1/coordination", h_coord),
        Route("/v1/coordination", h_coord_set, methods=["PUT"]),
        Route("/v1/models", h_models),
        Route("/v1/model", h_model_set, methods=["PUT"]),
        Route("/v1/storage/remove", h_storage_remove, methods=["POST"]),
        Route("/v1/import", h_import, methods=["POST"]),
        Route("/v1/doctor", h_doctor),
        Route("/v1/projects", h_projects),
        Route("/v1/notes", h_notes),
        Route("/v1/notes/{note_id}", h_note),
        Route("/v1/notes/{note_id}", h_note_edit, methods=["PUT"]),
        Route("/v1/notes/{note_id}/preview", h_note_preview, methods=["POST"]),
        Route("/v1/notes/{note_id}/forget", h_note_forget, methods=["POST"]),
        Route("/v1/notes/{note_id}/restore", h_note_restore, methods=["POST"]),
        Route("/v1/chats", h_chats),
        Route("/v1/chats/{chat_id}/messages", h_chat),
        Route("/v1/search", h_search),
        Route("/v1/events", h_events),
    ]
    app = Starlette(routes=routes, exception_handlers={ApiError: on_error, Exception: on_crash})
    return Track(app)


def usage_client(request) -> str:
    return (request.headers.get("x-recall-client") or "app")[:40]


BODY_MAX = 1_000_000


async def _json(request) -> dict:
    raw = await request.body()
    if len(raw) > BODY_MAX:
        raise ApiError(413, "too_large", f"a request body is at most {BODY_MAX} bytes")
    try:
        data = json.loads(raw or b"{}")
    except ValueError as e:
        raise ApiError(400, "bad_json", "the body is not JSON") from e
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "the body is a JSON object")
    return data


async def change(fn, *args, notes: bool = False, topics: list[str] | None = None) -> dict:
    """Every write goes through here, so a check that applies to all of them (step 5's per-device
    token) sits in one place. Runs the write off the event loop and the database thread, refreshes
    the notes search index after a notes change, and tells open event streams what changed."""
    from .memory.edit import Refused

    started = time.perf_counter()
    try:
        out = await off_db(fn, *args)
    except Refused as e:
        raise ApiError(e.status, e.code, e.message) from e
    except ValueError as e:
        raise ApiError(400, "refused", str(e)) from e
    took = {"write_ms": round((time.perf_counter() - started) * 1000)}
    loaded, digest = out.pop("_notes", None), out.pop("_digest", None)
    if notes:
        took |= await on_db(reindex, loaded, digest)
        topics = ["notes"]
    if topics:
        await _announce(topics)
    return out | {"took": took}


async def _announce(topics: list[str]) -> None:
    """Say what changed now, rather than when the file watcher's two-second window closes, and let the
    watcher know, so it doesn't say it again."""
    HUB.announce(topics)
    if HUB.watcher is not None:
        await HUB.watcher.prime()


ENDPOINTS = {
    "/v1/info": "version, API version, instance name, what this server can change (writes)",
    "/v1/status": "live sessions, overlaps, outlived servers, power, roots (?since=12h)",
    "/v1/stats": "chats and messages by agent, with each agent's latest chat",
    "/v1/storage": "bytes by part: index, vectors, notes, backups, rest",
    "/v1/runs": "memory runs, newest first (?limit=10)",
    "/v1/runs/latest": "the last memory run with its steps",
    "/v1/config": "model, embeddings, limits, schedule; keys are never returned",
    "/v1/coordination": "what happens when two chats edit the same file",
    "/v1/doctor": "each agent folder: connected or not, and what is missing",
    "/v1/projects": "projects with chat and note counts",
    "/v1/notes": "notes list (?type&project&status&scope&sort&limit&offset)",
    "/v1/notes/{id}": "one note: rules, source chats, related notes, git history",
    "/v1/chats": "chats list (?agent&project&since&until&subagents=1&limit&offset)",
    "/v1/chats/{id}/messages": "a chat's messages (?offset&limit&max_chars)",
    "/v1/search": "search chats and notes (?q&kind&agent&project&since&until&limit)",
    "/v1/events": "server-sent events: hello, then changed {topics}",
    "PUT /v1/notes/{id}": "save a note's text {text, stamp}: busy and conflict refuse it",
    "POST /v1/notes/{id}/preview": "what saving {text} would record, rule by rule; writes nothing",
    "POST /v1/notes/{id}/forget": "delete a note {stamp} so it is not learned again; returns the commit",
    "POST /v1/notes/{id}/restore": "put back a note forget deleted {commit}",
    "PUT /v1/coordination": "set the coordination rules {preset} or {rules: {situation: action}}",
    "/v1/models": "the models that can write memories here now, each with an option id",
    "PUT /v1/model": "switch the model {option} after one test call",
    "POST /v1/storage/remove": "delete copies kept before an upgrade {paths}",
    "POST /v1/import": "index a claude.ai or ChatGPT account export {path}",
}


# --- serving ------------------------------------------------------------------------


def _launchd_socket(name: str = "Listeners") -> socket.socket:
    """The socket launchd listens on for this job (a Sockets entry in its plist), so a client that
    connects starts the server and launchd keeps queued connections across its idle exits."""
    import ctypes

    lib = ctypes.CDLL(None)
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    if lib.launch_activate_socket(name.encode(), ctypes.byref(fds), ctypes.byref(count)) != 0 or count.value < 1:
        sys.exit("recall api: launchd gave no socket; --launchd belongs in a plist with a Sockets entry")
    sock = socket.socket(fileno=fds[0])
    sock.setblocking(False)
    return sock


def _bind(path: str) -> tuple[socket.socket, object]:
    """Take the lock, clear a stale socket file, and bind with mode 0600."""
    if len(path.encode()) > SUN_PATH_MAX:
        sys.exit(
            f"recall api: macOS allows a socket path of at most {SUN_PATH_MAX} bytes and this one is "
            f"{len(path.encode())}: {path}\nUse a shorter RECALL_HOME or pass --socket with a short path."
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(os.path.splitext(path)[0] + ".lock", "a")  # noqa: SIM115 held for the life of the server
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)  # another recall api holds the lock: it is running or starting
    if os.path.lexists(path):
        mode = os.lstat(path).st_mode  # lstat: a symlink here is refused, not followed
        if not stat.S_ISSOCK(mode):
            sys.exit(f"recall api: {path} exists and is not a socket; move it away or pass --socket elsewhere")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(path)
            sys.exit(0)  # answered: someone else serves it
        except OSError:
            os.unlink(path)  # nobody listens: a crashed run left it
        finally:
            probe.close()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    sock.listen(64)
    sock.setblocking(False)
    return sock, lock


def _log_to_file() -> None:
    """Send warnings and tracebacks to logs/api.log when nobody is watching the terminal."""
    if sys.stderr is None or sys.stderr.isatty():
        return
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        fd = os.open(os.path.join(paths.LOGS, "api.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass


def run(idle: float = DEFAULT_IDLE, path: str | None = None, launchd: bool = False) -> None:
    import uvicorn

    global STATE
    path = path or SOCKET
    from .integrations import scheduler

    # Started by an app or launchd, PATH is bare and misses the agent CLIs (claude, codex) and local
    # model servers: give it the folders the nightly run gets, so the model menu sees what the run will.
    os.environ["PATH"] = scheduler.with_job_path(os.environ.get("PATH"))
    if launchd:  # launchd owns the socket file: no lock, no cleanup
        sock, lock, inode = _launchd_socket(), None, None
    else:
        sock, lock = _bind(path)
        inode = os.stat(path).st_ino
    _log_to_file()
    STATE = Idle(idle)

    class Quiet(uvicorn.Server):
        """uvicorn's main loop wakes ten times a second; this one waits for the idle timer or a signal."""

        async def main_loop(self):
            await STATE.stop.wait()

        def handle_exit(self, sig, frame):
            super().handle_exit(sig, frame)
            if STATE.stop is not None:
                STATE.stop.get_loop().call_soon_threadsafe(STATE.stop.set)

    config = uvicorn.Config(
        build_app(), log_level="warning", access_log=False, server_header=False, date_header=False, lifespan="off"
    )
    server = Quiet(config)

    async def main():
        STATE.stop = asyncio.Event()
        STATE.arm()
        await server.serve(sockets=[sock])

    try:
        asyncio.run(main())
    finally:
        if lock is not None:
            with contextlib.suppress(OSError):
                if os.stat(path).st_ino == inode:
                    os.unlink(path)
            lock.close()
        _db.shutdown(wait=False, cancel_futures=True)

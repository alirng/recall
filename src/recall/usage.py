"""recall's log of its own use, so what gets built next follows what actually helps.

Every MCP tool call, session-start load, and search or read from the CLI adds one line to
usage.jsonl in recall's data folder: when, through what, which agent, the words asked
and the ids that came back. It never holds chat or note text, and it stays on this machine.
`recall usage` sums it up; set `usage_log = false` in config.toml to stop it.
"""

import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime

from . import paths

LOG = os.path.join(paths.HOME, "usage.jsonl")
KINDS = {
    "search": {"search_chats", "memory_search", "search", "memory search"},
    "read": {"read_chat", "read_note", "show", "memory show"},
    "recent": {"recent_chats", "recent"},
    "load": {"session_start", "project_memory"},
    "jit": {"jit"},
}
_enabled: bool | None = None


def record(via: str, tool: str, args: dict | None = None, result=None, *, client: str | None = None, **extra) -> None:
    """Add one entry. Never raises: a log line must not break a tool call or a hook."""
    try:
        if not enabled():
            return
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "via": via, "tool": tool}
        client = client or client_from_env()
        if client:
            entry["client"] = client
        if args := {k: v for k, v in (args or {}).items() if v not in (None, "", [])}:
            entry["args"] = args
        entry |= _outcome(result) | {k: v for k, v in extra.items() if v is not None}
        os.makedirs(paths.HOME, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def ms_since(start: float) -> int:
    """Milliseconds since a time.perf_counter() reading."""
    return round((time.perf_counter() - start) * 1000)


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        from . import config

        _enabled = bool(config.load().get("usage_log", True))
    return _enabled


def client_name(name: str | None) -> str | None:
    """'claude-code', 'codex-mcp-client', 'cursor-vscode' -> claude, codex, cursor."""
    if not name:
        return None
    low = name.lower()
    return next((agent for agent in ("claude", "codex", "cursor") if agent in low), name)


def client_from_env() -> str | None:
    """Which agent ran this command, when an agent's shell tool ran it."""
    env = os.environ
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if env.get("CURSOR_TRACE_ID") or env.get("CURSOR_AGENT"):
        return "cursor"
    return None


def _outcome(result) -> dict:
    if result is None:
        return {}
    if isinstance(result, str):
        return {"chars": len(result)}
    if isinstance(result, dict):
        if "error" in result:
            return {"error": str(result["error"])[:200]}
        found = result.get("chat_id") or result.get("note_id")
        return {"ids": [found]} if found else {}
    if isinstance(result, list):
        ids = [r.get("chat_id") or r.get("note_id") for r in result if isinstance(r, dict)]
        return {"n": len(result), "ids": [i for i in ids if i][:10]}
    return {}


# --- reading the log -------------------------------------------------------------


def entries(since: float | None = None) -> list[dict]:
    out = []
    try:
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is None or datetime.fromisoformat(e["at"]).timestamp() >= since:
                    out.append(e)
    except OSError:
        pass
    return out


def kind(entry: dict) -> str:
    return next((k for k, tools in KINDS.items() if entry.get("tool") in tools), "other")


def summary(since: float | None = None) -> dict:
    """Counts by agent and kind, how often a search found nothing or led to opening a
    result, what session-start loads carried, and the latest entries."""
    log = entries(since)
    by_client: dict[str, Counter] = defaultdict(Counter)
    for e in log:
        by_client[e.get("client") or e["via"]][kind(e)] += 1
    searches = [e for e in log if kind(e) == "search"]
    loads = [e for e in log if kind(e) == "load"]
    return {
        "since": log[0]["at"] if log else None,
        "calls": len(log),
        "by_agent": {c: dict(n) for c, n in sorted(by_client.items(), key=lambda kv: -sum(kv[1].values()))},
        "searches": {
            "total": len(searches),
            "empty": sum(1 for e in searches if e.get("n") == 0),
            "opened": _opened(log),
        },
        "loads": {
            "total": len(loads),
            "with_project": sum(1 for e in loads if e.get("project")),
            "empty": sum(1 for e in loads if not e.get("chars")),
        },
        "jit": jit_summary(log),
        "projects": dict(Counter(e["project"] for e in log if e.get("project")).most_common(8)),
        "latest": log[-10:],
    }


def jit_summary(log: list[dict]) -> dict:
    """What jit.py injected: fires by event, how often a prompt fired, characters per session,
    and how many injected chats or notes the agent then opened (a read of that id by the same
    agent within the hour: the log keeps no link between a hook's session and an MCP call's)."""
    jit = [e for e in log if e.get("tool") == "jit"]
    fires = [e for e in jit if e.get("ids")]
    if not jit:
        return {}
    sessions: dict[str, int] = defaultdict(int)
    for e in fires:
        sessions[f"{e.get('client')}:{e.get('session')}"] += e.get("chars") or 0
    followed = 0
    for i, e in enumerate(log):
        if e.get("tool") != "jit" or not e.get("ids"):
            continue
        start = datetime.fromisoformat(e["at"]).timestamp()
        for later in log[i + 1 :]:
            if datetime.fromisoformat(later["at"]).timestamp() - start > 3600:
                break
            same = kind(later) == "read" and later.get("client") == e.get("client")
            if same and set(e["ids"]) & set(later.get("ids") or []):
                followed += 1
                break
    prompts = [e for e in jit if (e.get("args") or {}).get("event") == "prompt"]
    chars = sorted(sessions.values())
    return {
        "fires": len(fires),
        "by_event": dict(Counter((e.get("args") or {}).get("event") for e in fires)),
        "items": dict(Counter(k for e in fires for k in e.get("kinds") or [])),
        "prompts": len(prompts),
        "prompts_fired": sum(1 for e in prompts if e.get("ids")),
        "sessions": len(sessions),
        "chars_per_session": {"median": chars[len(chars) // 2], "max": chars[-1]} if chars else {},
        "opened_after": followed,
    }


def _opened(log: list[dict]) -> int:
    """Searches whose results were opened by a later read in the same agent session."""
    opened = 0
    for i, e in enumerate(log):
        if kind(e) != "search" or not e.get("ids"):
            continue
        ids, session = set(e["ids"]), e.get("session")
        for later in log[i + 1 : i + 40]:
            if kind(later) == "read" and later.get("session") == session and ids & set(later.get("ids", [])):
                opened += 1
                break
    return opened

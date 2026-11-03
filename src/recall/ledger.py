"""The outcome ledger: what recall delivered, joined to what happened next in the same agent session.

usage.jsonl says what each hook delivered (note and rule ids) and to which agent session; the chat
index says what the session did afterwards. `recall ledger` joins them and counts, per channel and per
note, the outcomes the index can verify: a tool failure soon after, a commit linked to the session, a
commit touching the file whose read triggered the delivery, and the person correcting the agent.
It only reads. There is no comparison group, so these are rates to rank notes by, not proof that
memory helps.
"""

import contextlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

from . import paths, usage

FAIL_WINDOW = 15 * 60  # a tool failure this soon after a delivery
CORRECT_WINDOW = 30 * 60  # the person's correction this soon after one
COMMIT_WINDOW = 24 * 3600  # a commit linked to the session this soon after one
CHANNELS = {"session_start": "session start", "jit": "mid-session"}


def _ts(entry: dict) -> float:
    return datetime.fromisoformat(entry["at"]).timestamp()


def _key(entry: dict) -> str | None:
    """The chat id an entry's agent session has in the index, when the row names a session."""
    session, client = entry.get("session"), entry.get("client")
    if not isinstance(session, str) or session.startswith("pid:") or not client:
        return None  # an older release logged the server's process number
    return f"{client}:{session}"


def report(since: float | None = None, top: int = 15) -> dict:
    log = usage.entries(since)
    try:
        con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    except sqlite3.Error:
        con = None
    with contextlib.closing(con) if con else contextlib.nullcontext() as con:
        known = (
            {r[0] for r in con.execute("select id from sessions")} if con else set()
        )  # the index is missing: nothing joins
        channels: dict[str, Counter] = defaultdict(Counter)
        notes: dict[str, Counter] = defaultdict(Counter)
        for e in log:
            channel = _channel(e)
            if not channel:
                continue
            key = _key(e)
            c = channels[channel]
            c["rows"] += 1
            c["with_session"] += key is not None
            if not key or key not in known:
                continue
            c["joined"] += 1
            if not e.get("ids") or channel == "pull":
                continue
            try:
                outcome = _outcomes(con, key, _ts(e), (e.get("args") or {}).get("file"))
            except sqlite3.Error:  # an index without the link tables yet
                continue
            c["delivered"] += 1
            for name, hit in outcome.items():
                c[name] += hit
            for nid in e["ids"]:
                if nid.startswith(("rule:", "claude:", "codex:", "cursor:")):
                    continue  # repo rule files and chats; the per-note table is for notes
                n = notes[nid]
                n["deliveries"] += 1
                for name, hit in outcome.items():
                    n[name] += hit
    ranked = sorted(notes.items(), key=lambda kv: -kv[1]["deliveries"])[:top]
    return {
        "since": log[0]["at"] if log else None,
        "channels": {k: dict(v) for k, v in channels.items()},
        "notes": [{"note": nid, **dict(c)} for nid, c in ranked],
        "notes_total": len(notes),
    }


def _channel(e: dict) -> str | None:
    """Which delivery or pull a usage row is, by the names in the table above; MCP calls are "pull"."""
    if e.get("via") == "mcp" and e.get("tool") not in (None, "notes index", "index refresh"):
        return "pull"
    return CHANNELS.get(e.get("tool")) if e.get("via") == "hook" else None


def _outcomes(con, chat: str, at: float, file: str | None) -> dict[str, bool]:
    """What the index shows the session did after `at`."""
    failed = con.execute(
        "select 1 from tool_errors where session_id = ? and ts > ? and ts <= ? limit 1", (chat, at, at + FAIL_WINDOW)
    ).fetchone()
    commits = con.execute(
        """select c.files from commit_links c join link_commits k on k.repo = c.repo and k.sha = c.sha
           where c.session_id = ? and k.at > ? and k.at <= ?""",
        (chat, at, at + COMMIT_WINDOW),
    ).fetchall()
    touched = bool(file) and any(file in _files(r[0]) for r in commits)
    asked = con.execute(
        "select text from messages where session_id = ? and role = 'user' and ts > ? and ts <= ? order by ts limit 20",
        (chat, at, at + CORRECT_WINDOW),
    ).fetchall()
    from .chats.work import _CORRECTION

    return {
        "failed_15m": bool(failed),
        "commit_24h": bool(commits),
        "commit_touches_file": touched,
        "corrected_30m": any(_CORRECTION.search((r[0] or "")[:400]) for r in asked),
    }


def _files(raw: str | None) -> list[str]:
    try:
        return [f for f in json.loads(raw or "[]") if isinstance(f, str)]
    except json.JSONDecodeError:
        return []


def render(r: dict) -> str:
    if not r["channels"]:
        return f"Nothing logged yet ({usage.LOG})."
    out = [f"Deliveries and pulls since {r['since'][:16].replace('T', ' ')}\n"]
    cols = [
        "rows",
        "with_session",
        "joined",
        "delivered",
        "failed_15m",
        "commit_24h",
        "commit_touches_file",
        "corrected_30m",
    ]
    out.append("".ljust(14) + "".join(c.replace("_", " ").rjust(20) for c in cols))
    for name, c in r["channels"].items():
        out.append(name.ljust(14) + "".join(str(c.get(k, 0)).rjust(20) for k in cols))
    out.append(
        "\nwith session = the row names the agent's session (older rows hold a process number); joined = that "
        "session is in the index; the outcome columns count joined deliveries that carried ids."
    )
    if r["notes"]:
        out.append(f"\nMost delivered notes ({r['notes_total']} in all)")
        out.append("note".ljust(44) + "".join(c.rjust(11) for c in ("deliveries", "failed", "commit", "corrected")))
        for n in r["notes"]:
            out.append(
                n["note"][:42].ljust(44)
                + "".join(
                    str(n.get(k, 0)).rjust(11) for k in ("deliveries", "failed_15m", "commit_24h", "corrected_30m")
                )
            )
    return "\n".join(out)

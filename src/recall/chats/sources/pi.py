"""Pi and the agents that write Pi's session format under a root of their own: Oh My Pi (omp), Prime Agent,
OMO, StepCode, Senpi, gajae-code (gjc), Kimchi Coding and OpenClaw.

One JSONL file per session: a `session` header (id, timestamp, cwd, sometimes a title), then entries
`{type, id, parentId, timestamp, ...}` that form a tree. A `message` entry wraps `message.role` `user`,
`assistant` or `toolResult` (`toolCallId`, `isError`), and the words are its `text` blocks. Everything else
(`thinking` and `toolCall` blocks, `bashExecution`, `custom`, `custom_message`, `compaction`, `branch_summary`,
`session_info`, model changes) is not a turn. When entries branch (`/tree`, a rewound turn) the chat is the
path from the last entry back to the root; a file with no branching is read in file order, which is the same
thing and survives a chain with a missing link.

Layouts: Pi `sessions/--<cwd>--/<ts>_<id>.jsonl`; omp `sessions/-<cwd>/...` (profiles under
`~/.omp/profiles/*/agent`, or `$XDG_DATA_HOME/omp`; a rewritable `title` line comes before the header);
Prime flat `sessions/<id>.jsonl` (children in `session-artifacts/<parent>/sub-<n>/`); Kimchi flat or
`--<cwd>--`; gjc and omp keep a sub-agent's passes in a folder named for the session; StepCode names a
sub-agent's id `subagent-...`. OpenClaw: `agents/*/sessions/<id>.jsonl`, and since 2026.8 the live chats are
rows of `agents/*/agent/openclaw-agent.sqlite` (`transcript_events.event_json`, one Pi line per row; events of
1 KiB or more may be zstd blobs from 2026.9.9, read when a zstd decoder is installed, see compressed.py).

The folder name is lossy, so the project is the header's `cwd`, never the folder's name. A root that two keys
reach (PI_CODING_AGENT_DIR pointing at Senpi's folder) is read once, by the key whose own default it is.

Format knowledge: SpecStory's PI-FORMAT.md (Apache-2.0), deja-vu's docs/registry/{pi,omp,prime,senpi,gjc,
kimchi,openclaw}.md (MIT), agentsview's internal/parser/{pi,openclaw}.go (MIT); see docs/agents.md.
"""

import contextlib
import json
import os
import re
import sqlite3
import subprocess

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, compressed, iso_ts, open_ro, read_jsonl, table_names

CHAT_SCHEMA = "2026-11-03 new source: Pi, Oh My Pi, Prime Agent, OMO, StepCode, Senpi, gajae-code, Kimchi, OpenClaw"

# Variables that move an agent's folder or only its sessions, beyond the one roots.OTHERS knows:
# key -> (agent folder variables, sessions folder variables).
_ENV = {
    "pi": ((), ("PI_CODING_AGENT_SESSION_DIR",)),
    "prime": (
        ("PRIME_AGENT_CODING_AGENT_DIR",),
        ("PRIME_AGENT_SESSION_DIR", "PRIME_AGENT_CODING_AGENT_SESSION_DIR"),
    ),
    "stepcode": (("STEP_CODING_AGENT_DIR",), ("STEP_CODING_AGENT_SESSION_DIR",)),
}
_KEYS = ("pi", "omp", "prime", "omo", "stepcode", "senpi", "gjc", "kimchi")
_DEPTH = 5  # folders below a sessions folder: pi-subagents write <project>/<parent>/<run>/run-N/session.jsonl
_NOT_SESSIONS = {"semantic-edges.jsonl"}  # Prime's event log, beside its transcripts
_SUBAGENT_ID = ("subagent-", "workflow-")  # how StepCode names the header id of a child it spawned
_SUBAGENT_ENTRY = "kimchi:subagent-session"
_OPENCLAW_CONTEXT = re.compile(r"<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>.*?<<<END_OPENCLAW_INTERNAL_CONTEXT>>>", re.S)
_OPENCLAW_DATE = re.compile(r"^\[[A-Z][a-z]{2} \d{4}-\d\d-\d\d \d\d:\d\d[^\]]*\]\s*")


def _env_dirs(*names: str) -> list[str]:
    return [v for n in names if os.path.isabs(v := os.environ.get(n, ""))]


def _children(folder: str) -> list[str]:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if os.path.isdir(os.path.join(folder, n))]


def _places(key: str) -> list[tuple[str, bool]]:
    """(folder with sessions, named by an environment variable) for each place this agent keeps them."""
    agent_vars, session_vars = _ENV.get(key, ((), ()))
    out = [(d, True) for d in _env_dirs(*session_vars)]
    env = roots.OTHERS[key].env
    own = os.path.join(os.environ.get(env[0], ""), env[1]).rstrip(os.sep) if env else None
    agents = [(d, True) for d in _env_dirs(*agent_vars)] + [(d, d == own) for d in roots.other_dirs(key)]
    if key == "omp":  # a named profile, and the XDG data home, each hold a whole user scope
        data = os.environ.get("XDG_DATA_HOME", "")
        xdg = os.path.join(data if os.path.isabs(data) else os.path.join(roots.home(), ".local", "share"), "omp")
        agents += [(os.path.join(p, "agent"), False) for p in _children(os.path.join(roots.home(), ".omp", "profiles"))]
        agents += [(xdg, False)] + [(p, False) for p in _children(os.path.join(xdg, "profiles"))]
    for d, from_env in agents:
        out.append((os.path.join(d, "sessions"), from_env))
        if key == "prime":
            out.append((os.path.join(d, "session-artifacts"), from_env))
    return [(d, e) for d, e in out if os.path.isdir(d)]


def _claims() -> dict[str, list[str]]:
    """The sessions folders each key reads, every folder (by its real path) given to one key: the one
    whose default it is before one that names it through a variable, then in the order of _KEYS."""
    wanted = sorted(((e, k, d) for k in _KEYS for d, e in _places(k)), key=lambda w: w[0])
    seen: set[str] = set()
    out: dict[str, list[str]] = {k: [] for k in _KEYS}
    for _, key, folder in wanted:
        real = os.path.realpath(folder)
        if real not in seen:
            seen.add(real)
            out[key].append(folder)
    return out


def _files(folder: str) -> list[str]:
    out: list[str] = []
    top = folder.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, names in os.walk(folder):
        if dirpath.count(os.sep) - top >= _DEPTH:
            dirs[:] = []
        out += [os.path.join(dirpath, n) for n in names if n.endswith(".jsonl") and n not in _NOT_SESSIONS]
    return sorted(out)


def detect(key: str) -> int | None:
    folders = _claims()[key]
    return sum(len(_files(f)) for f in folders) if folders else None


def items(key: str):
    for folder in _claims()[key]:
        for path in _files(folder):
            try:
                st = os.stat(path)
            except OSError:
                continue
            yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path, f=folder: load(key, p, f))


def load(key: str, path: str, folder: str) -> Session | None:
    try:
        entries = list(read_jsonl(path))
        s = build(key, entries, os.path.basename(path).removesuffix(".jsonl"), path)
        if s is not None:
            _lineage(s, key, path, folder, entries)
        return s
    except (OSError, UnicodeError):
        return None


def _header_id(path: str) -> str | None:
    with contextlib.suppress(OSError, UnicodeError):
        for d in read_jsonl(path):
            if d.get("type") == "session":
                return d.get("id") if isinstance(d.get("id"), str) and d["id"] else None
    return None


def _lineage(s: Session, key: str, path: str, folder: str, entries: list[dict]) -> None:
    """Mark a sub-agent's file, where the layout or the header says it is one. A header that names a
    `parentSession` and no more is a fork or a clone, a chat of its own."""
    head = next((d for d in entries if d.get("type") == "session"), {})
    if key == "prime" and os.path.basename(folder) == "session-artifacts":
        if isinstance(head.get("rlmDepth"), int) and head["rlmDepth"] > 0:
            s.origin, s.parent = "subagent", f"{key}:{os.path.relpath(path, folder).split(os.sep)[0]}"
        return
    if key == "stepcode" and s.native_id.startswith(_SUBAGENT_ID):
        s.origin = "subagent"
        return
    marked = any(d.get("type") == "custom" and d.get("customType") == _SUBAGENT_ENTRY for d in entries[:4])
    if key == "kimchi" and isinstance(head.get("parentSession"), str) and marked:
        stem = os.path.basename(head["parentSession"].replace("\\", "/")).removesuffix(".jsonl")
        s.origin, s.parent = "subagent", f"{key}:{stem.split('_', 1)[-1]}"
        return
    rel = os.path.relpath(path, folder).split(os.sep)
    candidates = [os.path.join(folder, rel[0] + ".jsonl")] if len(rel) >= 2 else []
    candidates += [os.path.join(folder, rel[0], rel[1] + ".jsonl")] if len(rel) >= 3 else []
    for parent in candidates:  # a file under a folder named for another session's file
        if os.path.isfile(parent) and (pid := _header_id(parent)):
            s.origin, s.parent = "subagent", f"{key}:{pid}"
            return


def _active(body: list[dict]) -> list[dict]:
    """The entries of the chat as it ended: the root-to-leaf path when entries branch, else all of them."""
    by_id: dict[str, dict] = {}
    children: dict[str | None, int] = {}
    for e in body:
        if isinstance(e.get("id"), str) and e["id"]:
            by_id[e["id"]] = e
            if "parentId" in e:
                parent = e["parentId"] if isinstance(e["parentId"], str) else None
                children[parent] = children.get(parent, 0) + 1
    if not by_id or max(children.values(), default=0) < 2:
        return body
    leaf = next(e for e in reversed(body) if e.get("id") in by_id)
    path, seen, e = [], set(), leaf
    while e is not None and e["id"] not in seen:
        seen.add(e["id"])
        path.append(e)
        e = by_id.get(e.get("parentId"))
    path.reverse()
    return path if any(p.get("type") == "message" for p in path) else body


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def build(key: str, entries: list[dict], fallback_id: str, source: str) -> Session | None:
    """A chat from a Pi-format file's records (already parsed), or None when they are not one."""
    slot, head, body = None, None, []
    for e in entries:
        if head is None:
            if e.get("type") == "title":  # omp's rewritable title line, ahead of the header
                slot = e.get("title") if isinstance(e.get("title"), str) else slot
            elif e.get("type") == "session":
                head = e
            else:
                return None
        else:
            body.append(e)
    if head is None:
        return None
    sid = head.get("id")
    s = Session(key, sid if isinstance(sid, str) and sid else fallback_id, source)
    s.root = key
    s.project = head["cwd"] if isinstance(head.get("cwd"), str) and head["cwd"] else None
    path = _active(body)
    named = [e["name"] for e in path if e.get("type") == "session_info" and isinstance(e.get("name"), str)]
    title = slot or (named[-1] if named else None) or head.get("title")
    s.title = title.strip() or None if isinstance(title, str) else None
    s.started_at = iso_ts(head.get("timestamp"))
    s.updated_at = max((t for e in body if (t := iso_ts(e.get("timestamp"))) is not None), default=s.started_at)
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    for e in path:
        m = e.get("message")
        if e.get("type") != "message" or not isinstance(m, dict):
            continue
        role, ts = m.get("role"), iso_ts(e.get("timestamp")) or iso_ts(m.get("timestamp"))
        if role == "user":
            text = _text(m.get("content"))
            if key == "openclaw":
                text = _OPENCLAW_DATE.sub("", _OPENCLAW_CONTEXT.sub("", text).strip())
            append(messages, "user", clean_user_text(text), ts)
        elif role == "assistant":
            append(messages, "assistant", clean_assistant_text(_text(m.get("content"))), ts)
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "toolCall" and isinstance(b.get("id"), str):
                    pending[b["id"]] = (b.get("name") or "", b.get("arguments"))
        elif role == "toolResult":
            name, args = pending.pop(m.get("toolCallId"), (m.get("toolName") or "", None))
            failure = (signature(_text(m.get("content"))) or "error") if m.get("isError") is True else None
            calls.add(*position(messages), ts, name, args, m.get("toolCallId"), failure)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


# --- OpenClaw ------------------------------------------------------------------------------------------------


def _agent_folders() -> list[str]:
    return [a for root in roots.other_dirs("openclaw") for a in _children(os.path.join(root, "agents"))]


_ARCHIVE = re.compile(r"^(?P<id>.+?)\.jsonl(?:\.(?:reset|deleted)\..+)?$")


def _openclaw_files(folder: str) -> list[tuple[str, str]]:
    """(path, session id) of each transcript in an agent's sessions folder: the live ones, and what a reset
    or a delete left behind (`<id>.jsonl.reset.<ts>`), unless the live file is back. Compaction checkpoints,
    compressed archives and sessions.json are not chats."""
    base = os.path.join(folder, "sessions")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    found = {m["id"] for n in names if (m := _ARCHIVE.match(n)) and n.endswith(".jsonl")}
    out = []
    for n in names:
        m = _ARCHIVE.match(n)
        if m and ".checkpoint." not in n and not n.endswith(".zst") and (n.endswith(".jsonl") or m["id"] not in found):
            out.append((os.path.join(base, n), m["id"]))
    return out


def _database(folder: str) -> str | None:
    db = os.path.join(folder, "agent", "openclaw-agent.sqlite")
    return db if os.path.isfile(db) else None


def _db_sessions(db: str) -> dict[str, str]:
    """session id -> fingerprint, for every session with events in the store."""
    con = open_ro(db)
    if con is None:
        return {}
    try:
        if "transcript_events" not in table_names(con):
            return {}
        return {
            sid: f"db:{newest}:{n}"
            for sid, newest, n in con.execute(
                "select session_id, max(created_at), count(*) from transcript_events group by session_id"
            )
        }
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def detect_openclaw() -> int | None:
    if not roots.other_dirs("openclaw"):
        return None
    total = 0
    for folder in _agent_folders():
        total += len(_openclaw_files(folder))
        db = _database(folder)
        con = open_ro(db) if db else None
        if con is not None:
            with contextlib.suppress(sqlite3.Error):
                if "session_windows" in table_names(con):
                    total += con.execute("select count(*) from session_windows").fetchone()[0]
            con.close()
    return total


def items_openclaw():
    for folder in _agent_folders():
        db = _database(folder)
        in_db = _db_sessions(db) if db else {}
        for sid, fingerprint in in_db.items():
            yield Item(f"{db}#{sid}", fingerprint, lambda d=db, i=sid: load_db(d, i))
        for path, sid in _openclaw_files(folder):
            if sid in in_db:  # the store holds it now, and is the newer copy
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path, i=sid: load_openclaw_file(p, i))


def load_openclaw_file(path: str, sid: str) -> Session | None:
    try:
        return build("openclaw", list(read_jsonl(path)), sid, path)
    except (OSError, UnicodeError):
        return None


def _inflate(blob: bytes) -> bytes | None:
    """One zstd frame, with whichever decoder compressed.py finds; None when there is none or it fails."""
    try:
        match compressed.backend():
            case "python":
                from compression import zstd

                return zstd.decompress(blob)
            case "zstandard":
                import zstandard

                return zstandard.ZstdDecompressor().decompressobj().decompress(blob)
            case "zstd command":
                return subprocess.run(
                    [compressed.command(), "-dc"], input=blob, capture_output=True, timeout=30, check=True
                ).stdout
    except Exception:
        pass
    return None


def _events(con: sqlite3.Connection, sid: str) -> list[dict]:
    columns = {r[1] for r in con.execute("pragma table_info(transcript_events)")}
    packed = "event_zstd" in columns
    out = []
    for row in con.execute(
        f"select event_json{', event_zstd' if packed else ''} from transcript_events where session_id = ? order by seq",
        (sid,),
    ):
        raw = row[0]
        if raw is None and packed and row[1]:
            raw = _inflate(row[1])
        with contextlib.suppress(TypeError, ValueError):
            d = json.loads(raw)
            if isinstance(d, dict):
                out.append(d)
    return out


def _spawned(con: sqlite3.Connection, sid: str) -> tuple[bool, str | None]:
    """(is a sub-agent's session, the session that asked for it), from session_windows when it records spawns."""
    columns = {r[1] for r in con.execute("pragma table_info(session_windows)")}
    if not {"session_key", "spawned_by"} <= columns:
        return False, None
    row = con.execute("select session_key, spawned_by from session_windows where session_id = ?", (sid,)).fetchone()
    if not row or not (row[1] or ":subagent:" in (row[0] or "")):
        return False, None
    parent = con.execute(
        "select session_id from session_windows where session_key = ? and session_id != ? "
        "order by created_at desc limit 1",
        (row[1], sid),
    ).fetchone()
    return True, parent and parent[0]


def load_db(db: str, sid: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        s = build("openclaw", _events(con, sid), sid, f"{db}#{sid}")
        if s is not None and "session_windows" in table_names(con):
            subagent, parent = _spawned(con, sid)
            if subagent:
                s.origin, s.parent = "subagent", parent and f"openclaw:{parent}"
        return s
    except (sqlite3.Error, UnicodeError):
        return None
    finally:
        con.close()


_LABELS = {
    "pi": "Pi",
    "omp": "Oh My Pi",
    "prime": "Prime Agent",
    "omo": "OMO",
    "stepcode": "StepCode",
    "senpi": "Senpi",
    "gjc": "gajae-code",
    "kimchi": "Kimchi Coding",
}
AGENTS = {k: (name, lambda k=k: detect(k), lambda k=k: items(k)) for k, name in _LABELS.items()}
AGENTS["openclaw"] = ("OpenClaw", detect_openclaw, items_openclaw)

"""Cursor keeps chats in two places.

* Agent transcripts: ~/.cursor/projects/<path-slug>/agent-transcripts/<id>/<id>.jsonl
* Older composer chats: rows in the global state.vscdb (cursorDiskKV), with a
  composerData:<id> header and one bubbleId:<id>:<bubble> row per message.

Both share composer ids, so the database also supplies titles and timestamps for
transcripts. The database is opened read-only and is never written.
"""

import contextlib
import glob
import json
import os
import re
import sqlite3
from functools import cache, lru_cache
from urllib.parse import unquote

from ..transcript import Session, clean_user_text
from . import Item, append, iso_ts, read_jsonl, text_blocks

PROJECTS = os.path.expanduser("~/.cursor/projects")  # one folder for every Cursor build (roots.py)
USER_DIR = os.path.expanduser("~/Library/Application Support/Cursor/User")  # the default build's
STATE_DB = os.path.join(USER_DIR, "globalStorage", "state.vscdb")


def _state_db(root) -> str:
    return os.path.join(root.data, "globalStorage", "state.vscdb")


def _builds(roots) -> list[tuple[str, str]]:
    """(root name, state.vscdb) for each Cursor build with a chat database, the default build first."""
    if roots is None:
        return [("cursor", STATE_DB)]
    return [(r.name, _state_db(r)) for r in roots if r.data and os.path.exists(_state_db(r))]


@lru_cache(maxsize=8)
def _db(state_db: str = STATE_DB) -> sqlite3.Connection | None:
    if not os.path.exists(state_db):
        return None
    try:
        return sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, check_same_thread=False)
    except sqlite3.Error:
        return None


def _kv(key: str, state_db: str = STATE_DB):
    con = _db(state_db)
    if con is None:
        return None
    row = con.execute("select value from cursorDiskKV where key=?", (key,)).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def _norm(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")


@cache
def slug_to_path(slug: str, parent: str = "/") -> str | None:
    """Reverse Cursor's path slug (separators and punctuation became '-') against the disk."""
    try:
        children = os.listdir(parent)
    except OSError:
        return None
    for child in sorted(children, key=len, reverse=True):
        n = _norm(child)
        full = os.path.join(parent, child)
        if slug == n:
            return full
        if slug.startswith(n + "-") and os.path.isdir(full):
            found = slug_to_path(slug[len(n) + 1 :], full)
            if found:
                return found
    return None


@lru_cache(maxsize=8)
def _workspace_folders(user_dir: str = USER_DIR) -> dict[str, str]:
    """composer id -> workspace folder, from each workspace's own state db."""
    out = {}
    for ws in glob.glob(os.path.join(user_dir, "workspaceStorage", "*")):
        try:
            with open(os.path.join(ws, "workspace.json")) as f:
                meta = json.load(f)
            folder = meta.get("folder") or meta.get("workspace") or ""
            con = sqlite3.connect(f"file:{os.path.join(ws, 'state.vscdb')}?mode=ro", uri=True)
            row = con.execute("select value from ItemTable where key='composer.composerData'").fetchone()
            con.close()
        except (OSError, json.JSONDecodeError, sqlite3.Error):
            continue
        folder = unquote(folder.removeprefix("file://"))
        for c in json.loads(row[0]).get("allComposers", []) if row else []:
            if c.get("composerId"):
                out[c["composerId"]] = folder
    return out


def composer_folder(cid: str, state_db: str = STATE_DB) -> str | None:
    """The folder a Cursor chat ran in. The workspace's own list misses most chats, so then: the
    folder its transcript sits under (a subagent's under its parent's), the folder the chat
    records itself, and for a subagent or side chat that has neither, its parent's, up a chain
    of nested subagents. A transcript folder that no longer exists is the last resort."""
    return _real_folder(cid, state_db) or _transcript_slugs().get(cid)


@cache
def _real_folder(cid: str, state_db: str = STATE_DB, depth: int = 0) -> str | None:
    folders = _workspace_folders(os.path.dirname(os.path.dirname(state_db)))  # the build's own workspaces
    if cid in folders:
        return folders[cid]
    slug = _transcript_slugs().get(cid)
    recorded, parent = _brief(cid, state_db)
    found = (slug and slug_to_path(slug)) or recorded
    if not found and parent and depth < 8:
        found = _real_folder(parent, state_db, depth + 1)  # a parent's dead slug must not stop the climb
    return found


def _fresh() -> None:
    """Forget what earlier runs learned about folders: the MCP server indexes in-process, so
    workspaces, transcripts and chats added since must be seen."""
    for cached in (_workspace_folders, _transcript_slugs, _real_folder):
        cached.cache_clear()


def _brief(cid: str, state_db: str = STATE_DB) -> tuple[str | None, str | None]:
    """A chat's recorded folder and its parent chat, without parsing the whole record."""
    con = _db(state_db)
    row = (
        con.execute(
            "select json_extract(value, '$.workspaceIdentifier.uri.fsPath'),"
            " json_extract(value, '$.subagentInfo.parentComposerId')"
            " from cursorDiskKV where key = ? and json_valid(value)",
            (f"composerData:{cid}",),
        ).fetchone()
        if con is not None
        else None
    )
    return (row[0] or None, row[1] or None) if row else (None, None)


@lru_cache(maxsize=1)
def _transcript_slugs() -> dict[str, str]:
    """composer id -> the project slug its transcript sits under, subagents' included."""
    out = {}
    for pattern in (("*.jsonl",), ("subagents", "*.jsonl")):
        for path in glob.glob(os.path.join(PROJECTS, "*", "agent-transcripts", "*", *pattern)):
            out.setdefault(os.path.basename(path)[:-6], path[len(PROJECTS) + 1 :].split(os.sep)[0])
    return out


def _spawned(header: dict) -> bool:
    """A helper chat an agent spawned (explore and the like), not a person typing. A side
    chat is also recorded as a subagent, but the person types in it."""
    info = header.get("subagentInfo") or {}
    return bool(info) and info.get("subagentTypeName") != "side-chat" and not info.get("sideChatSeedTurnCount")


def detect(roots=None) -> int | None:
    builds = _builds(roots)
    if not (os.path.isdir(PROJECTS) or builds):
        return None
    n = len(_transcript_paths())
    for _, state_db in builds:
        con = _db(state_db)
        if con is not None:
            with contextlib.suppress(sqlite3.Error):
                n += con.execute(
                    "select count(*) from cursorDiskKV where key >= 'composerData:' and key < 'composerData;'"
                ).fetchone()[0]
    return n


def _transcript_paths():
    return glob.glob(os.path.join(PROJECTS, "*", "agent-transcripts", "*", "*.jsonl"))


def transcript_items(roots=None):
    _fresh()
    builds = _builds(roots)
    if roots is not None and not roots:
        return
    for path in _transcript_paths():
        try:
            st = os.stat(path)
        except OSError:  # deleted since the listing; the index and the archive keep its chat
            continue
        yield Item(path, f"{st.st_mtime_ns}:{st.st_size}", lambda p=path, b=builds: load_transcript(p, b))


def load_transcript(path: str, builds: list[tuple[str, str]] | None = None) -> Session | None:
    """Transcripts of every build share one folder; the build whose database knows the chat owns it."""
    cid = os.path.basename(path)[:-6]
    slug = path[len(PROJECTS) + 1 :].split(os.sep)[0]
    s = Session("cursor", cid, path)
    header, owner, owner_db = {}, None, (builds or [("cursor", STATE_DB)])[0][1]
    for name, state_db in builds or [("cursor", STATE_DB)]:
        header = _kv(f"composerData:{cid}", state_db) or {}
        if header:
            owner, owner_db = name, state_db
            break
    s.root = owner or (builds[0][0] if builds else "cursor")
    s.project = composer_folder(cid, owner_db) or slug_to_path(slug) or slug
    s.title = header.get("name")
    if _spawned(header):
        s.origin = "subagent"
    st = os.stat(path)
    s.started_at = iso_ts(header.get("createdAt")) or st.st_birthtime
    s.updated_at = iso_ts(header.get("lastUpdatedAt")) or st.st_mtime
    for d in read_jsonl(path):
        role = d.get("role")
        if role not in ("user", "assistant"):
            continue
        text = text_blocks((d.get("message") or {}).get("content"), ("text",))
        if role == "user":
            text = clean_user_text(text)
        append(s.messages, role, text, None)
    return s


def legacy_items(roots=None):
    _fresh()
    have_transcript = {os.path.basename(p)[:-6] for p in _transcript_paths()}
    for name, state_db in _builds(roots):
        con = _db(state_db)
        if con is None:
            continue
        # SQLite reads the few fields a fingerprint needs, so an unchanged chat never becomes
        # Python objects. Parsing every header in Python took seconds and over 100 MB.
        rows = con.execute(
            "select key, coalesce(nullif(json_extract(value, '$.lastUpdatedAt'), 0),"
            " nullif(json_extract(value, '$.createdAt'), 0), length(value))"
            " from cursorDiskKV where key >= 'composerData:' and key < 'composerData;'"
            " and case when json_valid(value) then json_type(value) = 'object'"
            " and (json_array_length(value, '$.conversation') > 0"
            " or json_array_length(value, '$.fullConversationHeadersOnly') > 0) end"
        )
        for key, fp in rows:
            cid = key.split(":", 1)[1]
            if cid not in have_transcript:
                yield Item(f"{state_db}#{key}", str(fp), lambda c=cid, d=state_db, n=name: load_legacy(c, d, n))


def load_legacy(cid: str, state_db: str = STATE_DB, root: str | None = None) -> Session | None:
    row = _db(state_db).execute("select value from cursorDiskKV where key = ?", (f"composerData:{cid}",)).fetchone()
    header = json.loads(row[0]) if row and row[0] else None
    if not isinstance(header, dict):
        return None
    s = Session("cursor", cid, f"{state_db}#composerData:{cid}")
    s.root = root
    s.title = header.get("name")
    s.project = composer_folder(cid, state_db)
    if _spawned(header):
        s.origin = "subagent"
    s.started_at = iso_ts(header.get("createdAt"))
    s.updated_at = iso_ts(header.get("lastUpdatedAt")) or s.started_at
    bubbles = header.get("conversation")
    if not bubbles:
        order = [h.get("bubbleId") for h in header.get("fullConversationHeadersOnly") or []]
        rows = _db(state_db).execute(
            "select key, value from cursorDiskKV where key >= ? and key < ?",
            (f"bubbleId:{cid}:", f"bubbleId:{cid};"),
        )
        by_id = {}
        for key, value in rows:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                by_id[key.rsplit(":", 1)[1]] = json.loads(value)
        bubbles = [by_id[b] for b in order if b in by_id]
    for b in bubbles:
        if not isinstance(b, dict):
            continue
        role = {1: "user", 2: "assistant"}.get(b.get("type"))
        text = (b.get("text") or "").strip()
        if role == "user":
            text = clean_user_text(text)
        if role:
            append(s.messages, role, text, iso_ts(b.get("createdAt")))
    return s

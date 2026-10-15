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

from ..errors import Calls, ToolError, position, signature
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


def load_transcript(path: str, builds: list[tuple[str, str]] | None = None, errors: bool = True) -> Session | None:
    """Transcripts of every build share one folder; the build whose database knows the chat owns it.
    errors=False skips the tool errors, whose bubbles are read from that database: a caller that only
    wants the words (the status line, a hook) should not pay for them."""
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
    # Not skipped when the transcript has no tool_use block: 86 of 166 such chats on the live copy still
    # have failed calls in the database (297 errors), which the transcript did not record.
    if errors:
        s.tool_errors = _transcript_errors(cid, header, s.messages, owner_db)
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
        bubbles = [(b, by_id[b]) for b in order if b in by_id]
    else:
        bubbles = [((b.get("bubbleId") if isinstance(b, dict) else None) or f"#{i}", b) for i, b in enumerate(bubbles)]
    calls = Calls()
    for bubble_id, b in bubbles:
        if not isinstance(b, dict):
            continue
        role = {1: "user", 2: "assistant"}.get(b.get("type"))
        text = (b.get("text") or "").strip()
        if role == "user":
            text = clean_user_text(text)
        if role:
            append(s.messages, role, text, iso_ts(b.get("createdAt")))
        if isinstance(b.get("toolFormerData"), dict) and (call := _call(b["toolFormerData"])):
            name, args, failure = call
            calls.add(*position(s.messages), iso_ts(b.get("createdAt")), name, args, bubble_id, failure)
    s.tool_errors = calls.errors()
    return s


def _call(tool: dict):
    """(tool, arguments, failure) for a finished call in a bubble's toolFormerData: failure is
    its redacted error signature (status "error", or a terminal command that exited non-zero),
    or None when it succeeded. None for a call that never finished."""
    name, status = tool.get("name") or "", tool.get("status")
    if not name or status not in ("completed", "error"):
        return None
    code = output = None
    if name.startswith("run_terminal") and status == "completed":
        result = _json(tool.get("result"))
        if isinstance(result, dict):
            code, output = result.get("exitCode"), result.get("output")
    args = _json(tool.get("rawArgs")) or _json(tool.get("params")) or {}
    return name, args, _failure(status, tool.get("error"), code, output)


def _failure(status, error, code, output) -> str | None:
    if status == "error":
        error = _json(error)
        message = (
            (error.get("modelVisibleErrorMessage") or error.get("clientVisibleErrorMessage"))
            if isinstance(error, dict)
            else str(error or "")
        )
        return signature(message or "") or "error"
    if code not in (None, 0):
        return signature(str(output or "")) or f"exit code {code}"
    return None


# What the database says about a chat's bubbles, for a transcript that records calls but not their
# results. SQLite pulls out only what is needed: tool bubbles (the prefilter skips the others), and
# for a terminal command its exit code, with the output only when that is not zero.
_TOOL_BUBBLES = """
select bid, created, name, status, error, code, case when code is not null and code != 0 then output end, raw, params
from (
  select *, case when json_valid(res) then json_extract(res, '$.exitCode') end code,
            case when json_valid(res) then json_extract(res, '$.output') end output
  from (
    select substr(key, ?) bid, json_extract(value, '$.createdAt') created,
           json_extract(value, '$.toolFormerData.name') name, json_extract(value, '$.toolFormerData.status') status,
           json_extract(value, '$.toolFormerData.error') error,
           case when json_extract(value, '$.toolFormerData.name') like 'run_terminal%'
                then json_extract(value, '$.toolFormerData.result') end res,
           json_extract(value, '$.toolFormerData.rawArgs') raw, json_extract(value, '$.toolFormerData.params') params
    from cursorDiskKV
    where key >= ? and key < ? and value like '%toolFormerData%' and json_valid(value)
  )
)"""
_TEXT_BUBBLES = """
select substr(key, ?), json_extract(value, '$.type'), json_extract(value, '$.text'),
       json_extract(value, '$.createdAt')
from cursorDiskKV
where key >= ? and key < ? and value not like '%toolFormerData%' and json_valid(value)
  and json_extract(value, '$.type') in (1, 2) and json_extract(value, '$.text') is not null"""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _transcript_errors(cid: str, header: dict, messages: list, state_db: str = STATE_DB) -> list[ToolError]:
    """Failed calls of a chat read from its transcript, which records calls but not their
    results: the results are in the chat's bubbles in the database. A bubble is tied to a
    transcript message by the words of the text bubble before it: the one message that holds
    them. Where no message, or more than one, does, the call is not placed (at=None)."""
    con = _db(state_db)
    if con is None or not messages:
        return []
    order = {h.get("bubbleId"): i for i, h in enumerate(header.get("fullConversationHeadersOnly") or [])}
    args = (len(f"bubbleId:{cid}:") + 1, f"bubbleId:{cid}:", f"bubbleId:{cid};")
    try:
        tools = con.execute(_TOOL_BUBBLES, args).fetchall()
        if not any(r[3] == "error" or (r[5] not in (None, 0)) for r in tools):
            return []  # no failure: the text bubbles that would place one are not read
        texts = con.execute(_TEXT_BUBBLES, args).fetchall()
    except sqlite3.Error:
        return []

    # The header lists only some of a long chat's bubbles, so the rest are slotted in by the time they
    # were created (the listed ones are in that order 99% of the time); a tie keeps the header's order.
    def when(bid, created):
        return (iso_ts(created) or 0.0, order.get(bid, len(order)))

    rows = [(when(r[0], r[1]), r[0], "tool", r) for r in tools]
    for bid, kind, text, created in texts:
        if kind == 1:
            text = clean_user_text(text)
        if text and text.strip():
            rows.append((when(bid, created), bid, "user" if kind == 1 else "assistant", text))
    rows.sort(key=lambda r: r[0])
    said = [(m.role, _collapse(m.text)) for m in messages]
    # the next assistant text after each row, for what the agent said after a failure
    after: list[str] = [""] * len(rows)
    upcoming = ""
    for i in range(len(rows) - 1, -1, -1):
        after[i] = upcoming
        if rows[i][2] == "assistant":
            upcoming = rows[i][3]
    calls = Calls()
    before: tuple[str, str] | None = None
    placed: dict[tuple[str, str], int | None] = {}  # many calls follow one text bubble
    for i, (_, bid, kind, row) in enumerate(rows):
        if kind != "tool":
            before = (kind, _collapse(row))
            continue
        _, created, name, status, error, code, output, raw, params = row
        if not name or status not in ("completed", "error"):
            continue
        failure = _failure(status, error, code, output)
        at = None
        if before and before[1] and (failure is not None or calls.failed):
            if before not in placed:
                hits = [j for j, (role, text) in enumerate(said) if role == before[0] and before[1] in text]
                placed[before] = hits[0] if len(hits) == 1 else None
            at = placed[before]
        calls.add(at, 0, iso_ts(created), name, _json(raw) or _json(params) or {}, bid, failure, after[i])
    return calls.errors()


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[")):
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None

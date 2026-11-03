"""Zed's agent panel: `<Zed data dir>/threads/threads.db` (macOS `~/Library/Application Support/Zed`, Linux
`~/.local/share/zed`), one row per thread. `data` is one JSON document, zstd-compressed when `data_type` is
`zstd` (everything Zed writes now) and plain when it is `json`. A thread whose body cannot be decoded (no zstd
decoder on this machine, a damaged frame) is skipped, not an error. The columns after the first five arrive
by `ALTER TABLE`, so an older store may lack `parent_id`, `folder_paths` and `created_at`.

Two document generations live in one store, because Zed rewrites a thread only when it is next saved:
  0.3.0  `{"title", "messages": [{"User": {"content": [{"Text": ".."}, {"Mention": ..}]}},
          {"Agent": {"content": [{"Text"}, {"Thinking"}, {"ToolUse"}], "tool_results": {id: {"is_error", ..}}}},
          "Resume", {"Compaction": ..}]}`
  0.2.0  `{"summary", "messages": [{"role", "segments": [{"type": "text", "text"}]}]}`
Mentions (the files and selections Zed pasted in front of the model), thinking and compaction summaries
are not indexed. The documents carry no per-message time, so messages have none.
`folder_paths` is the workspace roots, newline-joined: the first is the project (a store without that column
has it in the document's `initial_project_snapshot`). A row with `parent_id` is the thread of a subagent.
Tool failures are read from 0.3.0 only: a 0.2.0 message's tool calls are not documented.

Format knowledge: deja-vu docs/registry/zed.md (MIT); agentsview internal/parser/zed*.go (MIT).
"""

import io
import json
import os
import sqlite3
import subprocess

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, compressed, iso_ts, open_ro, table_names

CHAT_SCHEMA = "2026-11-03 new source: Zed"

KEY = "zed"


def _databases() -> list[str]:
    out: dict[str, str] = {}  # a real path once, whatever it was reached through
    for folder in roots.other_dirs(KEY):
        db = os.path.join(folder, "threads", "threads.db")
        if os.path.isfile(db):
            out.setdefault(os.path.realpath(db), db)
    return list(out.values())


def _columns(con) -> set[str]:
    if "threads" not in table_names(con):
        return set()
    return {r[1] for r in con.execute("pragma table_info(threads)")}


def _threads(db: str):
    """(id, updated_at, size) of each thread, or None when the store cannot be read now."""
    con = open_ro(db)
    if con is None:
        return None
    try:
        if not {"id", "updated_at", "data"} <= _columns(con):
            return []
        return con.execute("select id, coalesce(updated_at, ''), length(data) from threads").fetchall()
    except sqlite3.Error:  # a busy or unknown store is skipped
        return None
    finally:
        con.close()


def detect() -> int | None:
    dbs = _databases()
    if not dbs:
        return None
    return sum(len(_threads(db) or []) for db in dbs)


def items():
    for db in _databases():
        for tid, updated, size in _threads(db) or []:
            yield Item(f"{db}#{tid}", f"{updated}:{size}", lambda d=db, i=tid: load(d, i))


def _unzstd(blob: bytes) -> bytes | None:
    """The decoded bytes, by whichever decoder compressed.py found; None when there is none or it fails."""
    use = compressed.backend()
    try:
        if use == "python":
            from compression import zstd

            return zstd.decompress(blob)
        if use == "zstandard":
            import zstandard

            return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(blob), read_across_frames=True).read()
        if use == "zstd command":
            run = subprocess.run(
                [compressed.command(), "-dc"], input=blob, capture_output=True, timeout=compressed.STALL_SECONDS
            )
            return run.stdout if run.returncode == 0 else None
    except Exception:  # each decoder fails with its own error type; any of them means this thread is unreadable
        return None
    return None


def _document(data_type, blob) -> dict | None:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        blob = str(blob or "").encode()
    blob = bytes(blob)
    if str(data_type or "").lower() == "zstd":
        blob = _unzstd(blob)
    try:
        doc = json.loads(blob) if blob else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def load(db: str, tid: str) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        have = _columns(con)
        extra = [c if c in have else "null" for c in ("parent_id", "folder_paths", "created_at")]
        row = con.execute(
            f"select summary, updated_at, data_type, data, {', '.join(extra)} from threads where id = ?", (tid,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    summary, updated, data_type, blob, parent, folders, created = row
    doc = _document(data_type, blob)
    if doc is None:
        return None
    s = Session(KEY, tid, f"{db}#{tid}")
    s.root = KEY
    title = (summary or doc.get("title") or doc.get("summary") or "").strip()
    s.title = title.split("\n")[0][:120] or None
    s.project = next((p.strip() for p in str(folders or "").split("\n") if p.strip()), None) or _opened_in(doc)
    s.updated_at = iso_ts(updated) or iso_ts(doc.get("updated_at"))
    s.started_at = iso_ts(created) or s.updated_at
    if parent:
        s.origin, s.parent = "subagent", f"{KEY}:{parent}"
    messages: list[Message] = []
    calls = Calls()
    for m in doc.get("messages") or []:
        if isinstance(m, dict) and ("User" in m or "Agent" in m):
            _message(m, messages, calls)
        elif isinstance(m, dict) and "segments" in m:
            _legacy(m, messages)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s


def _opened_in(doc: dict) -> str | None:
    """Where the thread was opened, as the document itself records it: for a store without `folder_paths`."""
    snapshot = doc.get("initial_project_snapshot")
    trees = snapshot.get("worktree_snapshots") if isinstance(snapshot, dict) else None
    first = trees[0] if isinstance(trees, list) and trees else None
    path = first.get("worktree_path") if isinstance(first, dict) else None
    return path.strip() or None if isinstance(path, str) else None


def _text(block) -> str:
    """The words of a `Text` block, which is a string (or, in some builds, `{"text": ..}`)."""
    value = block.get("Text") if isinstance(block, dict) else None
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) else ""


def _output(result) -> str:
    if not isinstance(result, dict):
        return ""
    if isinstance(result.get("output"), str) and result["output"]:
        return result["output"]
    content = result.get("content")
    blocks = content if isinstance(content, list) else [content]
    return "\n".join(t for b in blocks if (t := _text(b) or (b if isinstance(b, str) else "")))


def _message(m: dict, messages: list[Message], calls: Calls) -> None:
    if "User" in m:
        body = m["User"] if isinstance(m["User"], dict) else {}
        words = "\n".join(t for b in body.get("content") or [] if (t := _text(b).strip()))
        append(messages, "user", clean_user_text(words), None)
        return
    body = m["Agent"] if isinstance(m["Agent"], dict) else {}
    blocks = [b for b in body.get("content") or [] if isinstance(b, dict)]
    append(messages, "assistant", clean_assistant_text("\n".join(t for b in blocks if (t := _text(b).strip()))), None)
    used = {
        str(b["ToolUse"].get("id")): b["ToolUse"] for b in blocks if isinstance(b.get("ToolUse"), dict)
    }  # tool_results is keyed by the id of the call it answers
    results = body.get("tool_results") if isinstance(body.get("tool_results"), dict) else {}
    for call_id, result in results.items():
        use = used.get(str(call_id), {})
        failure = (
            (signature(_output(result)) or "error") if isinstance(result, dict) and result.get("is_error") else None
        )
        name = use.get("name") or (result.get("tool_name") if isinstance(result, dict) else "")
        calls.add(*position(messages), None, name or "", use.get("input") or use.get("raw_input"), call_id, failure)


def _legacy(m: dict, messages: list[Message]) -> None:
    """A 0.2.0 message: its text segments. Whether it records tool calls is not documented, so none are read."""
    role = m.get("role")
    if role not in ("user", "assistant"):
        return
    words = "\n".join(
        str(seg.get("text") or "").strip()
        for seg in m.get("segments") or []
        if isinstance(seg, dict) and seg.get("type") == "text"
    )
    append(messages, role, clean_user_text(words) if role == "user" else clean_assistant_text(words), None)


AGENTS = {KEY: ("Zed", detect, items)}

"""Codex history: rollout JSONL files under ~/.codex/sessions, titled from the Codex app's thread catalogue.

Codex compresses rollouts it has not touched for a week to `.jsonl.zst` (compressed.py). A chat is
keyed by its plain `.jsonl` path whichever form is on disk, so compressing it changes nothing."""

import glob
import os
import sqlite3

from ...roots import Root
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, compressed, iso_ts, read_jsonl, text_blocks

ROOT = os.path.expanduser("~/.codex")  # the default root's; roots.discover() finds the rest
CATALOG = os.path.join(ROOT, "sqlite", "codex-dev.db")


def _catalog(folder: str = ROOT) -> dict[str, dict]:
    """Thread titles, cwd and branch from the Codex app's own thread catalogue."""
    try:
        con = sqlite3.connect(f"file:{os.path.join(folder, 'sqlite', 'codex-dev.db')}?mode=ro", uri=True)
        rows = con.execute("select thread_id, display_title, cwd, git_branch from local_thread_catalog").fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: {"title": r[1], "cwd": r[2], "branch": r[3]} for r in rows}


def _folders(roots) -> list[tuple[str | None, str]]:
    """(root name, folder) for each root with a sessions folder, once per real folder."""
    if roots is None:
        roots = [Root("codex", "codex", ROOT, default=True)]
    out, seen = [], set()
    for r in roots:
        real = os.path.realpath(os.path.join(r.path, "sessions"))
        if os.path.isdir(os.path.join(r.path, "sessions")) and real not in seen:
            seen.add(real)
            out.append((r.name, r.path))
    return out


def _threads(folder: str = ROOT) -> dict[str, str]:
    """Every rollout in one Codex folder: its plain .jsonl path -> the file that holds it now. A chat
    in both forms (Codex is mid-way between them) counts once, as the plain file."""
    found: dict[str, str] = {}
    for d in ("sessions", "archived_sessions"):
        base = os.path.join(folder, d, "**")
        for path in glob.glob(os.path.join(base, "*.jsonl"), recursive=True):
            found[path] = path
        for path in glob.glob(os.path.join(base, "*.jsonl" + compressed.SUFFIX), recursive=True):
            found.setdefault(compressed.plain_path(path), path)
    return found


def detect(roots=None) -> int | None:
    folders = _folders(roots)
    if not folders:
        return None
    return sum(len(_threads(f)) for _, f in folders)


def unreadable(roots=None) -> int:
    """Chats that exist only as .jsonl.zst while nothing here can decode zstd, in every Codex root
    recall reads (roots: those to look in; default, all the discovered ones with indexing on)."""
    if compressed.backend():
        return 0
    return sum(1 for _, f in _folders(_indexed(roots)) for stable, path in _threads(f).items() if path != stable)


def unreadable_note(roots=None) -> str | None:
    n = unreadable(roots)
    if not n:
        return None
    return f"{n:,} Codex chats are zstd-compressed and cannot be read: {compressed.fix_hint()}"


def _indexed(roots) -> list:
    from ... import config
    from ... import roots as harness

    cfg = config.load()
    found = harness.discover() if roots is None else roots
    return [r for r in found if r.agent == "codex" and harness.option(r, "index", cfg)]


def items(roots=None):
    readable = compressed.backend() is not None
    for name, folder in _folders(roots):
        catalog = None
        for stable, path in _threads(folder).items():
            if path != stable and not readable:
                continue  # not yielded, so no "seen" row stops it being read once a decoder exists
            fingerprint = compressed.fingerprint(stable)
            if fingerprint is None:
                continue  # gone since the listing

            def load_one(p=stable, n=name, f=folder):
                nonlocal catalog
                if catalog is None:
                    catalog = _catalog(f)
                return load(p, catalog, n)

            yield Item(stable, fingerprint, load_one)


def load(path: str, catalog: dict[str, dict], root: str | None = None) -> Session | None:
    s = None
    messages: list[Message] = []
    path = compressed.plain_path(path)
    for d in read_jsonl(path):
        payload = d.get("payload") or {}
        ts = iso_ts(d.get("timestamp"))
        if d.get("type") == "session_meta" and s is None:
            s = Session("codex", payload.get("id") or os.path.basename(path), path)
            s.root = root
            s.project = payload.get("cwd")
            s.branch = (payload.get("git") or {}).get("branch")
            s.started_at = iso_ts(payload.get("timestamp")) or ts
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                s.origin = "subagent"
            elif source == "exec":
                s.origin = "headless"
            meta = catalog.get(s.native_id) or {}
            s.title = meta.get("title")
            s.project = meta.get("cwd") or s.project
            s.branch = meta.get("branch") or s.branch
            continue
        if d.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = text_blocks(payload.get("content"))
        text = clean_user_text(text) if role == "user" else clean_assistant_text(text)
        append(messages, role, text, ts)
        if s and ts:
            s.updated_at = ts
    if s is None:
        return None
    s.messages = messages
    return s

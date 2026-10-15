"""Contexts: the separate parts of a developer's life, such as an employer's work, personal
projects and a side business, whose memories must not bleed into each other. config.toml
names each context's folders:

    [contexts]
    work = ["~/Documents/Projects/Acme"]
    personal = ["~/Documents/Projects/Personal"]

A preference the nightly run judges to hold everywhere (memory/reach.py) loads in every folder;
one tied to a context loads only in the folders of the contexts it was learned in. Memory
that hooks push in mid-session (jit.py) follows the same line for past chats too: a chat is
handed only to a session in the context it ran in. With no [contexts] table, everything is one
context.
"""

import hashlib
import json
import os
import re
import sqlite3

from . import config, paths

# What the notes index was built against (see fingerprint). The index keeps it in its meta table
# under STAMP, so that whoever reads it can tell the audiences it holds from a stale or
# unreadable config.toml from the ones that match the config now.
NONE, UNKNOWN, STAMP = "none", "unknown", "notes_contexts"


def roots() -> dict[str, list[str]]:
    """Each context's folders. Entries that are not a folder or a list of folders are skipped:
    a typo in config.toml must not cost a session its memory."""
    return read()[0]


def read() -> tuple[dict[str, list[str]], str]:
    """roots() and its fingerprint, from one read of config.toml."""
    cfg, readable = config.load_checked()
    table = cfg.get("contexts")
    out = {}
    for name, folders in table.items() if isinstance(table, dict) else ():
        folders = [folders] if isinstance(folders, str) else folders if isinstance(folders, list) else []
        if kept := [os.path.normpath(os.path.expanduser(f)) for f in folders if isinstance(f, str) and f.strip()]:
            out[str(name)] = kept
    return out, fingerprint(out, readable)


def fingerprint(table: dict[str, list[str]], readable: bool = True) -> str:
    """NONE when no context is configured, UNKNOWN when config.toml exists but cannot be read
    (roots() then reports none, which would wrongly read as "everything is one context"), else
    a digest of the contexts and their folders."""
    if not readable:
        return UNKNOWN
    if not table:
        return NONE
    return hashlib.sha1(json.dumps({k: sorted(v) for k, v in table.items()}, sort_keys=True).encode()).hexdigest()[:12]


def audiences_current(con, now: str) -> bool:
    """Whether the notes index's audiences (memory/notes.py audience) were worked out against the
    contexts configured now. They are when none is configured, or when the fingerprint the index
    stored equals the current one. UNKNOWN never matches, not even itself: an unreadable
    config.toml cannot vouch for an index that was built while it was unreadable."""
    if now == NONE:
        return True
    if now == UNKNOWN:
        return False
    try:
        row = con.execute("select v from meta where k = ?", (STAMP,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == now


def of_folder(folder: str | None, table: dict[str, list[str]] | None = None) -> str | None:
    """The context a folder belongs to, or None when it is outside every context's folders.
    Also takes the path slugs some agents record in place of a folder: a slug whose folder
    still exists is matched as that folder; otherwise by its leading segments, which cannot
    tell a context's folder from a sibling that extends its name (Acme from Acme-clone)."""
    if not folder:
        return None
    table = roots() if table is None else table
    if not os.path.isabs(folder):
        from .chats.sources.cursor import slug_to_path

        folder = slug_to_path(_slug(folder)) or folder
    path = os.path.normpath(os.path.expanduser(folder)) if os.path.isabs(folder) else None
    slug = None if path else _slug(folder)
    best, depth = None, -1
    for name, folders in table.items():
        for root in folders:
            if path:
                inside = path == root or path.startswith(root.rstrip(os.sep) + os.sep)
            else:
                inside = slug == _slug(root) or slug.startswith(_slug(root) + "-")
            if inside and len(root) > depth:
                best, depth = name, len(root)
    return best


def of_chats(chat_ids, table: dict[str, list[str]] | None = None) -> set[str]:
    """The contexts a set of chats ran in, from the folders the chat index recorded."""
    table = roots() if table is None else table
    if not table:
        return set()
    return {c for _, folder in _chats(chat_ids) if (c := of_folder(folder, table))}


def projects_of_chats(chat_ids) -> set[str]:
    """The project keys a set of chats ran in."""
    return {key for key, _ in _chats(chat_ids) if key}


def _chats(chat_ids) -> list[tuple[str | None, str | None]]:
    ids = list(chat_ids)
    if not ids:
        return []
    try:
        con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        rows = con.execute(
            f"select project_key, project from sessions where id in ({','.join('?' * len(ids))})", ids
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return rows


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")

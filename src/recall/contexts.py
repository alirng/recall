"""Contexts: the separate parts of a developer's life, such as an employer's work, personal
projects and a side business, whose memories must not bleed into each other. config.toml
names each context's folders:

    [contexts]
    work = ["~/Documents/Projects/Acme"]
    personal = ["~/Documents/Projects/Personal"]

A preference learned only in work chats then loads only in work folders, while one seen in
more than one context loads everywhere. With no [contexts] table, everything is one context.
"""

import os
import re
import sqlite3

from . import config, paths


def roots() -> dict[str, list[str]]:
    table = config.load().get("contexts") or {}
    return {
        name: [os.path.normpath(os.path.expanduser(f)) for f in ([folders] if isinstance(folders, str) else folders)]
        for name, folders in table.items()
    }


def of_folder(folder: str | None, table: dict[str, list[str]] | None = None) -> str | None:
    """The context a folder belongs to, or None when it is outside every context's folders.
    Also takes the path slugs some agents record in place of a folder."""
    if not folder:
        return None
    table = roots() if table is None else table
    path = os.path.normpath(os.path.expanduser(folder)) if os.path.isabs(folder) else None
    slug = _slug(folder)
    best, depth = None, -1
    for name, folders in table.items():
        for root in folders:
            inside = path == root or (path or "").startswith(root + os.sep)
            if (inside or slug == _slug(root) or slug.startswith(_slug(root) + "-")) and len(root) > depth:
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

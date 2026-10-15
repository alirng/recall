"""Decide which project each candidate memory belongs to.

A chat's project comes from the folder it ran in, which is usually right. Two
cases need correcting before consolidation: projects whose folder has moved, and
chats in one repo that are really about another project.
"""

import os
import re

# Folder names too common to identify a project when they appear in a sentence.
GENERIC_NAMES = frozenset(
    "agents app apps api web site docs demo test tests server client scripts project projects personal src "
    "code tools data".split()
)


def unify_moved(con) -> int:
    """Chats from a folder that no longer exists move to the one live project with the
    same folder name, if there is exactly one."""
    live: dict[str, set[str]] = {}
    for row in con.execute("select distinct project_key, project_name from sessions where project_key is not null"):
        key = row["project_key"]
        if key.startswith("git:") or os.path.exists(key.removeprefix("path:")):
            live.setdefault(row["project_name"].lower(), set()).add(key)
    moved = 0
    for row in con.execute(
        "select distinct project_key, project_name from candidates where project_key like 'path:%'"
    ).fetchall():
        key, name = row["project_key"], (row["project_name"] or "").lower()
        targets = live.get(name, set())
        if len(targets) == 1 and not os.path.exists(key.removeprefix("path:")):
            target = next(iter(targets))
            moved += con.execute(
                "update candidates set project_key=?, project_name=? where project_key=?",
                (target, target.rsplit("/", 1)[-1], key),
            ).rowcount
    return moved


def reroute_mentions(con) -> int:
    """Move a candidate to the project it is about, when its text names exactly one
    other known project and not its own ("fix the ledger widget", said in another repo)."""
    known = {}
    for row in con.execute(
        "select project_name, max(project_key) k from sessions where project_key is not null group by 1"
    ):
        name = (row["project_name"] or "").lower()
        if len(name) >= 4 and name not in GENERIC_NAMES:
            known[name] = (row["k"], row["project_name"])
    moved = 0
    for c in con.execute(
        "select id, project_name, title, body from candidates where status='new' and scope='project'"
    ).fetchall():
        text = f" {c['title']} {c['body']} ".lower()
        own = (c["project_name"] or "").lower()
        if own and _names(own, text):
            continue
        hits = [target for name, target in known.items() if name != own and _names(name, text)]
        if len(hits) == 1:
            con.execute("update candidates set project_key=?, project_name=? where id=?", (*hits[0], c["id"]))
            moved += 1
    return moved


def refile(con, store, notes) -> int:
    """File a note learned in chats with no known folder under its project, once the chat
    index places every one of those chats in that same project, e.g. after a parser learns
    where an agent records the folder."""
    moved = 0
    for n in list(notes.values()):
        if n.status != "active" or n.scope == "global" or n.project or not n.sources:
            continue
        rows = [
            con.execute("select project_key, project_name from sessions where id = ?", (chat,)).fetchone()
            for chat in n.sources
        ]
        found = {(r["project_key"], r["project_name"]) for r in rows if r and r["project_key"]}
        if len(found) != 1 or not all(r and r["project_key"] for r in rows):
            continue
        note = store.reread(n)
        if not note or note.status != "active" or note.scope == "global" or note.project:
            continue  # changed by hand since the run loaded it
        (note.project, note.project_name), note.scope = found.pop(), "project"
        store.write(note)
        notes[note.id] = note
        moved += 1
    return moved


def _names(name: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None

"""Changing notes by hand from an app or the command line: edit a note's text, forget a note, and
put a forgotten note back.

Each change takes the same lock a memory run takes (notes.writing), writes through NoteStore, rewrites
the MEMORY.md indexes and commits the notes folder, so the nightly run, `recall memory link` and these
take turns and leave one history. Each returns, under "_notes" and "_digest", the notes as it left
them and their digest, taken under the lock, which the caller hands to reindex() to bring the search
index up to date without reading every note again.

An edit is read the way a hand edit of the file is (notes._edits): each rule is kept, reworded,
removed or new, and preview() shows that reading before anything is written. Forgetting deletes the
note's file and records it in the deletion ledger (title, scope and project only), which stops the
next run learning it again from the same chats; its old text stays in the notes folder's git history.
restore() writes the file back from that history, and the ledger then forgets the deletion.
"""

import copy
import os
import re
import subprocess
import sys

from .notes import (
    MERGED_BY_HAND,
    Note,
    NoteStore,
    Rule,
    _edits,
    _inside,
    _ledger_entry,
    _split,
    _write_json,
    read_note_file,
    today,
    writing,
)

LOCK_WAIT = 2.0  # seconds a change waits for a memory run to let go of the notes before saying busy


class Refused(Exception):
    """A change recall will not make now. `code` names why; `status` is the HTTP status the API answers."""

    status = 400

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Busy(Refused):
    status = 409

    def __init__(self):
        super().__init__("busy", "a memory run is writing notes; try again when it ends")


class Conflict(Refused):
    status = 409

    def __init__(self, message: str = "this note's file changed since you read it"):
        super().__init__("conflict", message)


class Missing(Refused):
    status = 404


def load_notes(store: NoteStore, include_archived: bool = False) -> dict[str, Note]:
    """store.load(), except that a note file recall cannot parse is skipped and logged, one file at
    a time, so a single malformed note does not stop every other note from being read or changed."""
    notes: dict[str, Note] = {}
    for path in store.files(include_archived):
        try:
            note = read_note_file(path)
        except Exception as e:
            print(f"recall: skipping note {path}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if note:
            notes[note.id] = note
            store.titles[note.id] = note.title
    return notes


# --- reading an edit ------------------------------------------------------------------------


def preview(path: str, body: str) -> dict:
    """What saving `body` as the note's text would record, rule by rule, without writing anything.
    `path` is the note's file, read now, so the reading is against the file as it stands."""
    note = read_note_file(path)
    if note is None:
        raise Missing("no_such_note", "the note's file is gone")
    body = _clean(body)
    if not body:
        return {"stamp": note.stamp, "empty": True, "changes": [], "counts": {}}
    before = copy.deepcopy(note.rules)
    after, _ = _reread(note, body)
    changes = reading(before, after)
    empty = not any(r.active for r in after)  # nothing in it reads as a rule ("-" alone, say)
    return {"stamp": note.stamp, "empty": empty, "changes": changes, "counts": _counts(changes)}


def _clean(body: str) -> str:
    return (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _reread(note: Note, body: str) -> tuple[list[Rule], bool]:
    """The rules once `body` replaces the note's text, dated today, as a hand edit of the file is read."""
    note.body = body
    return _edits(note, note.rules, today())


def reading(before: list[Rule], after: list[Rule]) -> list[dict]:
    """Each rule the edit touches, in the order the edited text shows them, with the rules it removed
    where they stood: kept, reworded, new, restored (a retired rule's words written back), removed (not
    learned again from the same chats) or merged (folded into another rule)."""
    was = {r.id: r for r in before}
    by_id = {r.id: r for r in after}
    shown: list[dict] = []
    for r in after:
        if not r.active:
            continue
        old = was.get(r.id)
        if old is None:
            # A note from before rules, written as a list, is split into one rule per item: its own words.
            kind = (
                "kept"
                if any(o.active and len(_split(o.text)[1]) > 1 and _inside(r.text, o.text) for o in before)
                else "new"
            )
        elif not old.active:
            kind = "restored"
        elif " ".join(old.text.split()) != " ".join(r.text.split()):
            kind = "reworded"
        else:
            kind = "kept"
        row = {"kind": kind, "rule_id": r.id, "text": r.text, "said_by": r.said_by}
        if kind == "reworded":
            row["was"] = old.text
        shown.append(row)
    order = [r.id for r in before if r.active]
    for r in after:
        old = was.get(r.id)
        if r.active or old is None or not old.active:
            continue
        row = {"kind": "merged" if r.reason == MERGED_BY_HAND else "removed", "rule_id": r.id, "text": r.text}
        if r.replaced_by and r.replaced_by in by_id:
            row["into"] = by_id[r.replaced_by].text
        # Placed after the nearest rule above it that the edit kept, so it reads where it stood.
        at = 0
        for prev in reversed(order[: order.index(r.id)]):
            hit = next((i for i, s in enumerate(shown) if s["rule_id"] == prev), None)
            if hit is not None:
                at = hit + 1
                break
        shown.insert(at, row)
    return shown


def _counts(changes: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in changes:
        out[c["kind"]] = out.get(c["kind"], 0) + 1
    return out


# --- writing --------------------------------------------------------------------------------


def edit(note_id: str, body: str, stamp: str | None, timeout: float = LOCK_WAIT) -> dict:
    """Save `body` as an active note's text. `stamp` is the note's stamp as the caller read it; a
    file changed since is not overwritten (Conflict). An emptied body is refused: forget the note."""
    body = _clean(body)
    if not stamp:
        raise Refused("bad_parameter", "stamp is required: send the note's stamp as you read it")
    if not body:
        raise Refused("empty", "a note needs at least one rule; to drop the whole note, forget it")
    with writing(timeout) as free:
        if not free:
            raise Busy()
        store = NoteStore()
        notes = load_notes(store)  # every note, so the footer's links keep their titles
        note = _current(store, notes, note_id, stamp)
        before = copy.deepcopy(note.rules)
        rules, changed = _reread(note, body)
        if not changed:
            return {"note_id": note.id, "changed": False, "commit": None, "changes": [], "counts": {}}
        if not any(r.active for r in rules):
            raise Refused("empty", "nothing in this text reads as a rule; to drop the whole note, forget it")
        note.rules = rules
        note.updated = today()
        store.write(note)
        notes[note.id] = note
        store.compile(notes)
        sha = store.commit(f"edited by hand: {note.title}")
        digest = store.digest()
    changes = reading(before, note.rules)
    return {
        "note_id": note.id,
        "changed": True,
        "commit": sha,
        "stamp": note.stamp,
        "status": note.status,
        "changes": changes,
        "counts": _counts(changes),
        "_notes": notes,
        "_digest": digest,
    }


def forget(note_id: str, stamp: str | None, timeout: float = LOCK_WAIT) -> dict:
    """Delete an active note's file and record it as rejected, so no run learns it again from the
    same chats. Returns the commit, which restore() takes to put it back."""
    if not stamp:
        raise Refused("bad_parameter", "stamp is required: send the note's stamp as you read it")
    with writing(timeout) as free:
        if not free:
            raise Busy()
        store = NoteStore()
        notes = load_notes(store)
        note = _current(store, notes, note_id, stamp)
        os.remove(note.path)
        del notes[note.id]
        _ledger(store, note, rejected=True)
        store.compile(notes)
        sha = store.commit(f"forgotten: {note.title}")
        digest = store.digest()
    return {
        "note_id": note.id,
        "title": note.title,
        "commit": sha,
        "path": os.path.relpath(note.path, store.root),
        "_notes": notes,
        "_digest": digest,
    }


def restore(note_id: str, commit: str, timeout: float = LOCK_WAIT) -> dict:
    """Put back a note that `commit` deleted (forget's commit), from the notes folder's history. The
    ledger then stops treating it as rejected."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise Refused("bad_commit", "commit is the short or full sha that forget returned")
    with writing(timeout) as free:
        if not free:
            raise Busy()
        store = NoteStore()
        rel = _deleted_in(store.root, commit, note_id)
        if not rel:
            raise Missing("not_deleted_there", f"commit {commit} did not delete note {note_id}")
        subject = (_git(store.root, "show", "-s", "--format=%s", commit) or "").strip()
        if note_id not in store._read_ledger().get("rejected", {}) and not subject.startswith("forgotten: "):
            raise Refused("not_forgotten", "only a note that was forgotten can be restored")
        if any(os.path.basename(p) == f"{note_id}.md" for p in store.files(include_archived=True)):
            raise Conflict("a note with this id is back already")
        text = _git(store.root, "show", f"{commit}^:{rel}")
        if text is None:
            raise Missing("not_in_history", "the note's text is not in the notes folder's history")
        path = os.path.join(store.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        notes = load_notes(store)
        note = notes.get(note_id)
        if note:
            _ledger(store, note, rejected=False)
        store.compile(notes)
        sha = store.commit(f"restored: {note.title if note else note_id}")
        digest = store.digest()
    return {
        "note_id": note_id,
        "title": note.title if note else note_id,
        "commit": sha,
        "path": rel,
        "_notes": notes,
        "_digest": digest,
    }


def _ledger(store: NoteStore, note: Note, rejected: bool) -> None:
    """Record in the deletion ledger what reconcile() would on its next pass, for this one note: a note
    deleted by hand moves from known to rejected (title, scope and project only), and one back on disk
    moves back. reconcile() reads every note, archived ones too; this is the same result in a few
    milliseconds."""
    ledger = store._read_ledger()
    known, gone = ledger.get("known", {}), ledger.get("rejected", {})
    entry = _ledger_entry(note.title, note.scope, note.project)
    if rejected:
        known.pop(note.id, None)
        gone[note.id] = entry
    else:
        gone.pop(note.id, None)
        known[note.id] = entry
    _write_json(store._ledger, {"known": known, "rejected": gone})


def _current(store: NoteStore, notes: dict[str, Note], note_id: str, stamp: str | None) -> Note:
    """The active note as its file is now, or why it can't be changed."""
    note = notes.get(note_id)
    if note is None:
        if note_id in load_notes(store, include_archived=True):
            raise Refused("archived", "this note is archived; only active notes can be edited or forgotten")
        if stamp:
            raise Conflict("this note's file is gone since you read it")
        raise Missing("no_such_note", f"no note {note_id!r}")
    if stamp and note.stamp != stamp:
        raise Conflict()
    return note


def _deleted_in(root: str, commit: str, note_id: str) -> str | None:
    out = _git(root, "show", "--name-only", "--diff-filter=D", "--format=", commit) or ""
    return next((p for p in out.splitlines() if os.path.basename(p) == f"{note_id}.md"), None)


def _git(root: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def reindex(con=None, notes: dict[str, Note] | None = None, digest: str | None = None) -> bool:
    """Bring the notes search tables up to date after a change, so the next search and the mid-session
    hook see it at once rather than on their next refresh. `notes` and `digest`: what a change returned,
    the notes and their digest as it left them under the lock, so they are not read again; a memory run
    that writes meanwhile changes the digest, and the next refresh rebuilds."""
    from ..chats import store
    from . import context

    own = con is None
    con = con or store.connect()
    try:
        if notes is None or digest is None or context._replaced():
            return context.refresh_notes(con)
        return context.index_notes(con, notes, f"{context.NOTES_SCHEMA}|{digest}")
    finally:
        if own:
            con.close()

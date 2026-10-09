"""Links between memory notes, derived from the notes themselves.

Two notes are related when they share distinctive words, and more so when they
were learned from the same chat or belong to the same project. No model call is
involved, so links cost nothing and follow every hand edit. Each note keeps its
closest few in `Note.related`, which `NoteStore.write` renders as wikilinks at the
foot of the file, so Obsidian's graph shows them too. A `[[note-id]]` written by
hand in a note's body is a link as well, and recall never removes it.
"""

import math
import re
from collections import Counter, defaultdict

from .notes import Note, NoteStore

PER_NOTE = 4
THRESHOLD = 0.09
MIN_SHARED_WORDS = 2
SAME_CHAT_BOOST = 1.0
SAME_PROJECT_BOOST = 0.5

# Words that say how a note is phrased rather than what it is about.
STOP = set(
    """the and for with that this from into when then than they them their there have has had not but are was
    were will would should could can use using used make makes made keep keeps also only more most other each over
    such what which while where who why how all any its via per new one two out off own same very just like about
    after before every rather prefer prefers preferred developer developers user users person people project
    projects explicitly change changes changed want wants wanted asked ask asks need needs needed instead without
    within because until unless still even being been does doing done work works working way ways thing things
    time first last next itself himself herself themselves avoid ensure clear clearly directly direct current
    currently existing exist exists available able specific general overall across show shows note notes says
    said tell tells sure etc yet already again always never often usually""".split()
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")
_HAND_LINK = re.compile(r"\[\[([^\]|#]+)")


def compute(notes: dict[str, Note]) -> dict[str, list[tuple[str, float]]]:
    """For each active note, its most related notes as (id, score), best first."""
    active = [n for n in notes.values() if n.status == "active"]
    words = {n.id: _words(f"{n.title} {n.body}") for n in active}
    df = Counter(w for ws in words.values() for w in ws)
    idf = {w: math.log(len(active) / c) for w, c in df.items()}
    mass = {i: math.sqrt(sum(idf[w] ** 2 for w in ws)) or 1.0 for i, ws in words.items()}
    # Accumulate shared-word weight through an inverted index, so cost follows the
    # pairs that share words rather than every pair of notes.
    postings = defaultdict(list)
    for note_id, ws in words.items():
        for w in ws:
            if df[w] > 1:
                postings[w].append(note_id)
    shared: dict[tuple[str, str], list[float]] = defaultdict(list)
    for w, ids in postings.items():
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                shared[(a, b) if a < b else (b, a)].append(idf[w] ** 2)
    by_id = {n.id: n for n in active}
    scored: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (a, b), weights in shared.items():
        if len(weights) < MIN_SHARED_WORDS:
            continue
        na, nb = by_id[a], by_id[b]
        score = sum(weights) / (mass[a] * mass[b])
        score *= 1 + SAME_CHAT_BOOST * same_chat(na, nb) + SAME_PROJECT_BOOST * same_place(na, nb)
        if score >= THRESHOLD:
            scored[a].append((b, score))
            scored[b].append((a, score))
    return {i: sorted(pairs, key=lambda p: -p[1])[:PER_NOTE] for i, pairs in scored.items()}


def relink(store: NoteStore, notes: dict[str, Note]) -> int:
    """Recompute every note's related links and rewrite the notes whose footer changed,
    including when a linked note was renamed. Returns how many changed."""
    best = compute(notes)
    changed = 0
    for note in notes.values():
        if note.status != "active":
            continue
        note.related = [i for i, _ in best.get(note.id, [])]
        if store.footer(note) != note.footer:
            store.write(note)
            changed += 1
    return changed


def hand_links(note: Note, notes: dict[str, Note]) -> list[str]:
    """Notes linked by hand with [[note-id]] in the body."""
    return [i for i in dict.fromkeys(m.strip() for m in _HAND_LINK.findall(note.body)) if i in notes and i != note.id]


def neighbours(note: Note, notes: dict[str, Note]) -> list[dict]:
    """Everything a note links to, and everything that links to it, with the reason."""
    out: dict[str, str] = {}
    for i in note.related:
        out[i] = "same chat" if i in notes and same_chat(note, notes[i]) else "similar"
    for i in hand_links(note, notes):
        out[i] = "linked"
    for other in notes.values():
        if other.id == note.id or other.id in out or other.status != "active":
            continue
        if note.id in other.related:
            out[other.id] = "same chat" if same_chat(note, other) else "similar"
        elif note.id in hand_links(other, notes):
            out[other.id] = "linked from"
    for i in note.supersedes:
        out[i] = "replaces"
    if note.superseded_by:
        out[note.superseded_by] = "replaced by"
    return [{"note_id": i, "reason": why} for i, why in out.items() if i in notes]


def same_chat(a: Note, b: Note) -> bool:
    return bool(set(a.sources) & set(b.sources))


def same_place(a: Note, b: Note) -> bool:
    return a.scope == b.scope and a.project == b.project


def _words(text: str) -> set[str]:
    return {w.strip(".-_") for w in _WORD.findall(text.lower())} - STOP

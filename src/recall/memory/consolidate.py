"""Reduce step ("dreaming"): fold candidate memories into notes.

Works one scope at a time (global, each project, unfiled) and oldest candidates
first, so newer information supersedes older. For each batch the model sees the
existing notes most related to it and returns operations; every operation is
validated before a file changes, and candidates the model skips are retried.
"""

import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from ..llm import Client
from . import prompts
from .notes import CONFIDENCE, Note, NoteStore, new_id, today

SYSTEM, _ = prompts.load("consolidate")
BATCH = 20
CONTEXT_NOTES = 40
MAX_ATTEMPTS = 2
OPEN_TTL_DAYS = 14
LOW_TTL_DAYS = 180

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operations"],
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "candidates", "note_id", "type", "title", "body", "confidence", "reason"],
                "properties": {
                    "op": {"enum": ["create", "update", "supersede", "discard"]},
                    "candidates": {"type": "array", "items": {"type": "integer"}},
                    "note_id": {"type": "string"},
                    "type": {"enum": ["preference", "fact", "decision", "lesson", "open"]},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

BULK_DISCARD_NUDGE = (
    "\n\nA previous answer discarded nearly every candidate with one blanket reason. Decide each "
    "candidate on its own merits: keep durable project facts, decisions, lessons and open threads."
)


@dataclass(frozen=True)
class Scope:
    kind: str  # "global" | "project" | "unfiled"
    key: str | None = None  # project key
    name: str | None = None  # project name

    @property
    def label(self) -> str:
        if self.kind == "global":
            return "global (how the developer works across all projects)"
        if self.kind == "unfiled":
            return "unfiled (project facts from chats that ran outside any project folder)"
        return f'project "{self.name}" ({self.key})'

    def holds(self, note: Note) -> bool:
        return note.scope == self.kind and note.project == self.key


# Global and unfiled candidates form one group each, whichever project they came from.
_GROUP_KEY = "case when scope='project' then coalesce(project_key, '') else '' end"


def run(con, client: Client, store: NoteStore, notes: dict[str, Note], log=print) -> Counter:
    stats = Counter()
    groups = con.execute(
        f"""select scope, {_GROUP_KEY} key, max(project_name) name, count(*) n
            from candidates where status='new' group by scope, {_GROUP_KEY} order by n desc"""
    ).fetchall()
    for g in groups:
        scope = Scope(g["scope"], g["key"] or None, g["name"]) if g["scope"] == "project" else Scope(g["scope"])
        candidates = con.execute(
            f"select * from candidates where status='new' and scope=? and {_GROUP_KEY}=? order by chat_ts, id",
            (g["scope"], g["key"]),
        ).fetchall()
        log(f"consolidating {len(candidates)} candidates into {scope.label}")
        rejected = [r for r in store.rejected() if r.get("scope") == scope.kind and r.get("project") == scope.key]
        for start in range(0, len(candidates), BATCH):
            batch = candidates[start : start + BATCH]
            existing = [n for n in notes.values() if scope.holds(n)]
            prompt = _render(scope, _related(existing, batch), batch, rejected[-30:])
            ops = client.json("consolidate", SYSTEM, prompt, SCHEMA)["operations"]
            if _bulk_discard(ops, len(batch)):
                # Small models sometimes wave a whole batch away with one blanket reason.
                ops = client.json("consolidate-retry", SYSTEM, prompt + BULK_DISCARD_NUDGE, SCHEMA)["operations"]
            _apply(con, store, notes, scope, batch, ops, stats)
            con.commit()
            stats["batches"] += 1
    stats["expired"] = expire(store, notes)
    return stats


def expire(store: NoteStore, notes: dict[str, Note]) -> int:
    """Open threads go stale quickly; weak single-source notes fade if never seen again."""
    now, n = time.time(), 0
    for note in list(notes.values()):
        try:
            age = (now - datetime.fromisoformat(note.last_seen).timestamp()) / 86400
        except ValueError:
            continue
        stale_open = note.type == "open" and age > OPEN_TTL_DAYS
        faded = note.confidence == "low" and len(note.sources) <= 1 and age > LOW_TTL_DAYS
        if stale_open or faded:
            note.status, note.updated = "expired", today()
            store.write(note)
            del notes[note.id]
            n += 1
    return n


def _apply(con, store: NoteStore, notes: dict[str, Note], scope: Scope, batch, ops, stats: Counter) -> None:
    by_number = dict(enumerate(batch, 1))
    used: set[int] = set()
    for op in ops:
        numbers = [i for i in op["candidates"] if i in by_number and i not in used]
        if not numbers:
            continue
        used.update(numbers)
        candidates = [by_number[i] for i in numbers]
        target = notes.get(op["note_id"])
        kind = op["op"]
        if kind in ("update", "supersede") and not (target and scope.holds(target)):
            kind = "create"  # an unknown or out-of-scope note id: treat as new
        if kind == "discard" or (kind != "update" and not op["title"].strip()):
            con.executemany(
                "update candidates set status='discarded', reason=? where id=?",
                [(op["reason"][:300] or "discarded", c["id"]) for c in candidates],
            )
            stats["discarded"] += len(candidates)
            continue
        if kind == "update":
            note = _update(target, op, candidates)
        else:
            note = _create(scope, op, candidates, notes)
            if kind == "supersede":
                note.supersedes = [target.id]
                target.status, target.superseded_by, target.updated = "superseded", note.id, today()
                store.write(target)
                del notes[target.id]
            notes[note.id] = note
        store.write(note)
        stats["created" if kind == "create" else "updated" if kind == "update" else "superseded"] += 1
        con.executemany(
            "update candidates set status='merged', note_id=? where id=?", [(note.id, c["id"]) for c in candidates]
        )
    skipped = [(c["id"],) for i, c in by_number.items() if i not in used]
    con.executemany("update candidates set attempts = attempts + 1 where id=?", skipped)
    con.execute(
        "update candidates set status='discarded', reason='never assigned' where status='new' and attempts>=?",
        (MAX_ATTEMPTS,),
    )


def _update(note: Note, op: dict, candidates) -> Note:
    note.title = op["title"].strip() or note.title
    note.body = op["body"].strip() or note.body
    note.sources = sorted(set(note.sources) | {c["session_id"] for c in candidates})
    note.last_seen = max(note.last_seen or "", _date(max(c["chat_ts"] or 0 for c in candidates)))
    note.updated = today()
    note.confidence = max(note.confidence, op["confidence"], key=lambda c: CONFIDENCE.get(c, 0))
    if len(note.sources) >= 3:
        note.confidence = "high"  # corroborated across several chats
    return note


def _create(scope: Scope, op: dict, candidates, notes: dict[str, Note]) -> Note:
    first = _date(min(c["chat_ts"] or 0 for c in candidates))
    note = Note(
        id=new_id(op["title"], salt=f"{scope.key}{first}"),
        type=op["type"],
        scope=scope.kind,
        title=op["title"].strip(),
        body=op["body"].strip(),
        project=scope.key,
        project_name=scope.name,
        confidence=op["confidence"],
        created=first,
        updated=today(),
        last_seen=_date(max(c["chat_ts"] or 0 for c in candidates)),
        sources=sorted({c["session_id"] for c in candidates}),
    )
    while note.id in notes:
        note.id = new_id(op["title"], salt=f"{scope.key}{first}{time.time()}")
    return note


def _render(scope: Scope, existing: list[Note], batch, rejected) -> str:
    lines = [f"Scope: {scope.label}", ""]
    if rejected:
        lines.append("Deleted by the developer (never recreate these; discard candidates that restate them):")
        lines += [f"- {r['title']}: {r['body'][:200]}" for r in rejected]
        lines.append("")
    lines.append("Existing notes:")
    for n in existing:
        lines.append(
            f"[{n.id}] ({n.type}, {n.confidence}, {len(n.sources)} source chats, last seen {n.last_seen}) "
            f"{n.title}: {' '.join(n.body.split())}"
        )
    if not existing:
        lines.append("(none yet)")
    lines += ["", "New candidates:"]
    for i, c in enumerate(batch, 1):
        why = f" Why: {c['why']}" if c["why"] else ""
        lines.append(
            f"{i}. ({_date(c['chat_ts'])}, {c['type']}, said by {c['speaker']}, {c['confidence']}) "
            f'{c["title"]}: {c["body"]}{why} Evidence: "{c["evidence"][:160]}"'
        )
    return "\n".join(lines)


def _related(notes: list[Note], batch) -> list[Note]:
    """The existing notes most likely to overlap a batch, so prompts stay small."""
    if len(notes) <= CONTEXT_NOTES:
        return notes
    wanted = _words(" ".join(f"{c['title']} {c['body']}" for c in batch))
    return sorted(notes, key=lambda n: -len(wanted & _words(f"{n.title} {n.body}")))[:CONTEXT_NOTES]


def _bulk_discard(ops, n: int) -> bool:
    discarded = sum(len(op["candidates"]) for op in ops if op["op"] == "discard")
    return n >= 6 and discarded / n > 0.6


_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else today()

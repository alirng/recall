"""Reduce step ("dreaming"): fold candidate memories into notes.

Works one scope at a time (global, each project, unfiled) and oldest candidates first. A
note is a titled list of rules (see notes.py). For each batch the model sees the existing
notes most related to it, rule by rule with ids and dates, and answers with operations on
rules: confirm, refine, retire or add one, or create a note. Plain code applies them, after
checking each one, and nothing is written until the whole batch is checked:
- a confirm, refine or retire must name a rule that exists in the scope;
- a candidate from a chat older than a rule's last sighting can neither retire nor refine
  it, so a chat read late (the nightly job reads newest first) cannot undo a newer rule;
- a rule leaves a note only through a retire that names it: one that went missing any
  other way is put back, and the log says so.
Candidates the model skips, or whose only operations were refused, are retried.
"""

import copy
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..chats.redact import redact
from ..llm import Client
from . import prompts
from .notes import CONFIDENCE, REMOVED_BY_HAND, Note, NoteStore, Rule, new_id, rule_id, sync, today

SYSTEM, _ = prompts.load("consolidate")
BATCH = 20
CONTEXT_NOTES = 40
RETIRED_SHOWN = 2  # per note: recent retired rules, so a restated old form reads as outdated
MAX_ATTEMPTS = 2
OPEN_TTL_DAYS = 14
LOW_TTL_DAYS = 180
TYPES = ["preference", "fact", "decision", "lesson", "open"]

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
                "required": [
                    "op",
                    "candidates",
                    "note_id",
                    "rule_id",
                    "type",
                    "title",
                    "rules",
                    "confidence",
                    "reason",
                ],
                "properties": {
                    "op": {"enum": ["confirm", "refine", "retire", "add", "create", "discard"]},
                    "candidates": {"type": "array", "items": {"type": "integer"}},
                    "note_id": {"type": "string"},
                    "rule_id": {"type": "string"},
                    "type": {"enum": TYPES},
                    "title": {"type": "string"},
                    "rules": {"type": "array", "items": {"type": "string"}},
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
    for note in notes.values():
        if note.edited_by_hand:  # keep the rules read back from a hand edit
            store.write(note)
            stats["edited by hand"] += 1
    removed = [
        {"title": n.title, "body": r.text, "scope": n.scope, "project": n.project}
        for n in store.load(include_archived=True).values()
        for r in n.rules
        if r.reason == REMOVED_BY_HAND
    ]
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
        rejected = [
            r for r in store.rejected() + removed if r.get("scope") == scope.kind and r.get("project") == scope.key
        ]
        for start in range(0, len(candidates), BATCH):
            batch = candidates[start : start + BATCH]
            existing = [n for n in notes.values() if scope.holds(n)]
            prompt = _render(scope, _related(existing, batch), batch, rejected[-30:])
            ops = client.json("consolidate", SYSTEM, prompt, SCHEMA)["operations"]
            if _bulk_discard(ops, len(batch)):
                # Small models sometimes wave a whole batch away with one blanket reason.
                ops = client.json("consolidate-retry", SYSTEM, prompt + BULK_DISCARD_NUDGE, SCHEMA)["operations"]
            _apply(con, store, notes, scope, batch, ops, stats, log)
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


# --- applying operations ----------------------------------------------------------------


@dataclass
class _Batch:
    """A batch's changes, made on copies of the notes until every operation is checked."""

    notes: dict[str, Note]  # the store's notes, unchanged until commit
    scope: Scope
    work: dict[str, Note] = field(default_factory=dict)  # copies of the notes operations touch
    created: dict[str, Note] = field(default_factory=dict)
    retired: set[tuple[str, str]] = field(default_factory=set)  # (note id, rule id) retired by an operation
    merged: dict[int, str] = field(default_factory=dict)  # candidate id -> note id
    discarded: dict[int, str] = field(default_factory=dict)  # candidate id -> reason

    def note(self, note_id: str) -> Note | None:
        if note_id in self.created:
            return self.created[note_id]
        if note_id not in self.work:
            original = self.notes.get(note_id)
            if not (original and self.scope.holds(original) and original.status == "active"):
                return None
            self.work[note_id] = copy.deepcopy(original)
        return self.work[note_id]

    def rule(self, note_id: str, rid: str) -> tuple[Note, Rule] | None:
        """A rule by note and id; failing that, by its id alone when only one note in scope has it."""
        note = self.note(note_id)
        hit = next((r for r in note.rules if r.id == rid and r.active), None) if note else None
        if hit:
            return note, hit
        current = [self.work.get(i, n) for i, n in self.notes.items()] + list(self.created.values())
        owners = [
            (n.id, r)
            for n in current
            if self.scope.holds(n) and n.status == "active"
            for r in n.rules
            if r.id == rid and r.active
        ]
        if len(owners) != 1:
            return None
        note = self.note(owners[0][0])
        return note, next(r for r in note.rules if r.id == rid)


def _apply(con, store: NoteStore, notes: dict[str, Note], scope: Scope, batch, ops, stats: Counter, log) -> None:
    by_number = dict(enumerate(batch, 1))
    b = _Batch(notes, scope)
    for op in ops:
        candidates = [by_number[i] for i in dict.fromkeys(op["candidates"]) if i in by_number]
        if not candidates:
            continue
        kind, texts = op["op"], _texts(op["rules"])
        op = {**op, "note_id": op["note_id"].strip().strip("[]"), "rule_id": op["rule_id"].strip().strip("[]")}
        if kind == "discard":
            for c in candidates:
                b.discarded.setdefault(c["id"], redact(op["reason"])[:300] or "discarded")
            continue
        when = _date(max(c["chat_ts"] or 0 for c in candidates))
        found = b.rule(op["note_id"], op["rule_id"]) if kind in ("confirm", "refine", "retire") else None
        if kind in ("confirm", "refine", "retire") and not found:
            if kind == "refine" and texts and b.note(op["note_id"]):
                kind = "add"  # new detail for a rule it could not name: keep it as a rule of its own
            else:
                stats["refused, unknown rule"] += 1
                log(f"  refused {kind}: no rule {op['rule_id']!r} in note {op['note_id']!r}")
                continue
        if found and kind in ("refine", "retire") and when < found[1].seen:
            # A chat read late (the nightly job reads newest first) cannot undo a newer rule.
            stats["refused, older than the rule"] += 1
            log(f"  refused {kind} of {found[1].id}: candidate from {when}, rule last seen {found[1].seen}")
            for c in candidates:
                b.discarded.setdefault(c["id"], f"older than rule {found[1].id}, last seen {found[1].seen}")
            continue
        if kind == "add" and not b.note(op["note_id"]):
            kind = "create"  # an unknown or out-of-scope note id: a new note
        if kind == "create":
            note = _create(b, op, texts or [c["body"] for c in candidates], candidates)
        elif found:
            note, rule = found
            if kind == "confirm" or (kind == "refine" and not texts):
                _support(rule, candidates)
            elif kind == "refine":
                rule.text = texts[0]
                _support(rule, candidates)
                _insert(note, rule, _new_rules(note, texts[1:], candidates))
            else:  # retire
                new = _new_rules(note, texts, candidates)
                rule.status, rule.retired, rule.reason = "retired", when, op["reason"].strip()[:300]
                rule.replaced_by = new[0].id if new else None
                b.retired.add((note.id, rule.id))
                _insert(note, rule, new)
            kind = "confirm" if kind == "refine" and not texts else kind
        else:  # add
            note = b.note(op["note_id"])
            for text in texts or [c["body"] for c in candidates]:
                same = next((r for r in note.rules if r.active and _same(r.text, text)), None)
                if same:
                    _support(same, candidates)
                else:
                    note.rules += _new_rules(note, [text], candidates)
        if kind != "create" and op["title"].strip():
            note.title = op["title"].strip()
        note.confidence = max(note.confidence, op["confidence"], key=lambda c: CONFIDENCE.get(c, 0))
        stats[
            {"confirm": "confirmed", "refine": "refined", "retire": "retired", "add": "added"}.get(kind, "created")
        ] += 1
        for c in candidates:
            b.merged.setdefault(c["id"], note.id)
    _restore(b, stats, log)
    _commit(con, store, notes, b, by_number)


def _restore(b: _Batch, stats: Counter, log) -> None:
    """A rule leaves a note only through a retire naming it: put back any that went another way."""
    for note_id, note in b.work.items():
        for before in b.notes[note_id].rules:
            if not before.active or (note_id, before.id) in b.retired:
                continue
            now = next((r for r in note.rules if r.id == before.id), None)
            if now is None:
                note.rules.append(copy.deepcopy(before))
            elif not now.active or not now.text.strip():
                now.status, now.retired, now.reason, now.replaced_by = "active", "", "", None
                now.text = now.text.strip() or before.text
            else:
                continue
            stats["restored"] += 1
            log(f"  restored rule {before.id} in {note_id}: it went missing without a retire")


def _commit(con, store: NoteStore, notes: dict[str, Note], b: _Batch, by_number: dict) -> None:
    for note in [*b.work.values(), *b.created.values()]:
        sync(note)
        if len(note.sources) >= 3:
            note.confidence = "high"  # corroborated across several chats
        if note.id in b.work and _state(note) == _state(b.notes[note.id]):
            continue  # looked at, not changed
        note.updated = today()
        store.write(note)  # also renders the body, and archives a note with no rule left
        if note.status == "active":
            notes[note.id] = note
        else:
            notes.pop(note.id, None)
    con.executemany(
        "update candidates set status='merged', note_id=? where id=?", [(n, c) for c, n in b.merged.items()]
    )
    con.executemany(
        "update candidates set status='discarded', reason=? where id=?",
        [(why, c) for c, why in b.discarded.items() if c not in b.merged],
    )
    skipped = [(c["id"],) for c in by_number.values() if c["id"] not in b.merged and c["id"] not in b.discarded]
    con.executemany("update candidates set attempts = attempts + 1 where id=?", skipped)
    con.execute(
        "update candidates set status='discarded', reason='never assigned' where status='new' and attempts>=?",
        (MAX_ATTEMPTS,),
    )


def _state(note: Note) -> tuple:
    before = copy.deepcopy(note)
    sync(before)
    return before.title, before.confidence, before.status, [asdict(r) for r in before.rules]


def _create(b: _Batch, op: dict, texts: list[str], candidates) -> Note:
    first = _date(min(c["chat_ts"] or 0 for c in candidates))
    title = op["title"].strip() or candidates[0]["title"]
    note = Note(
        id=new_id(title, salt=f"{b.scope.key}{first}"),
        type=op["type"],
        scope=b.scope.kind,
        title=title,
        body="",
        project=b.scope.key,
        project_name=b.scope.name,
        confidence=op["confidence"],
        created=first,
        updated=today(),
    )
    while note.id in b.notes or note.id in b.created:
        note.id = new_id(title, salt=f"{b.scope.key}{first}{time.time()}")
    note.rules = _new_rules(note, texts, candidates)
    sync(note)
    b.created[note.id] = note
    return note


def _new_rules(note: Note, texts: list[str], candidates) -> list[Rule]:
    taken = {r.id for r in note.rules}
    out = []
    for text in texts:
        rule = Rule(rule_id(note.id, text, taken), text, first_seen=_date(min(c["chat_ts"] or 0 for c in candidates)))
        taken.add(rule.id)
        _support(rule, candidates)
        out.append(rule)
    return out


def _support(rule: Rule, candidates) -> None:
    """Record that these candidates state the rule: dates, source chats, who said it, a quote.
    A rule the developer stated in any chat counts as said by the developer."""
    new = not rule.sources and not rule.edited
    rule.last_seen = max(rule.last_seen, _date(max(c["chat_ts"] or 0 for c in candidates)))
    rule.sources = sorted(set(rule.sources) | {c["session_id"] for c in candidates})
    said = [c for c in candidates if c["speaker"] == "user"]
    if said and rule.said_by != "developer":
        rule.said_by = "developer"
        rule.evidence = next((c["evidence"] for c in said if c["evidence"]), rule.evidence)[:200]
    elif new:  # an earlier rule's speaker is unknown (notes from before rules): leave it unset
        rule.said_by = "agent"
        rule.evidence = next((c["evidence"] for c in candidates if c["evidence"]), "")[:200]


def _insert(note: Note, after: Rule, new: list[Rule]) -> None:
    """New rules go where the rule they come from stands, so the body keeps its order."""
    at = note.rules.index(after)
    note.rules[at + 1 : at + 1] = new
    if not after.active:  # a replacement takes the retired rule's place
        note.rules[at : at + 1 + len(new)] = [*new, after]


def _texts(values: list[str]) -> list[str]:
    return [t for t in (" ".join(v.split()) for v in values) if t]


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


# --- the prompt ---------------------------------------------------------------------------


def _render(scope: Scope, existing: list[Note], batch, rejected) -> str:
    lines = [f"Scope: {scope.label}", ""]
    if rejected:
        lines.append("Deleted by the developer (never recreate these; discard candidates that restate them):")
        lines += [f"- {r['title']}: {r['body'][:200]}" for r in rejected]
        lines.append("")
    lines.append("Existing notes, each with its rules: [rule id] (first stated to last stated) text")
    for n in existing:
        lines.append(f"[{n.id}] ({n.type}, {n.confidence}, {len(n.sources)} source chats) {n.title}")
        for r in n.rules:
            if r.active:
                lines.append(f"  - [{r.id}] ({r.first_seen or '?'} to {r.seen or '?'}) {' '.join(r.text.split())}")
        retired = sorted((r for r in n.rules if not r.active), key=lambda r: r.retired)[-RETIRED_SHOWN:]
        for r in retired:
            lines.append(f"  - retired {r.retired or '?'}, no longer holds: {' '.join(r.text.split())[:160]}")
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
    discarded = {i for op in ops if op["op"] == "discard" for i in op["candidates"]}
    kept = {i for op in ops if op["op"] != "discard" for i in op["candidates"]}
    return n >= 6 and len(discarded - kept) / n > 0.6


_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else today()

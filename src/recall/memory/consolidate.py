"""Reduce step ("dreaming"): fold candidate memories into notes.

Works one scope at a time (global, each project, unfiled) and oldest candidates first. A
note is a titled list of rules (see notes.py). For each batch the model sees the existing
notes most related to it, rule by rule with ids and dates, and answers with operations on
rules: confirm, refine, retire or add one, or create a note. Plain code applies them, after
checking each one, and nothing is written until the whole batch is checked:
- a confirm, refine or retire must name a rule that exists in the scope;
- a candidate from a chat older than a rule's last sighting can neither retire nor refine
  it, and one older than a rule's retirement cannot add it back, so a chat read late (the
  nightly job reads newest first) cannot undo a newer rule;
- a rule leaves a note only through a retire that names it: one that went missing any
  other way is put back, and the log says so;
- a rule the developer stated with `recall remember` keeps their words: only a newer chat in
  which they typed something it does not say can refine or retire it (see _kept);
- a candidate from `recall remember` is already a rule (remember.py): it can retire the older
  rules it replaces and fold in a rule that says the same, and nothing else (see _remembered);
- a note whose file changed while the batch ran (a hand edit) is not written from the stale
  copy: its candidates stay for the next run;
- the same goes for a hand edit waiting to be read back into a note's rules, which is written
  first: one whose file changed since it was read, or an emptied body saved in the last ten
  minutes (an editor may be mid-rewrite), is left for the next run.
Candidates the model skips, or whose only operations were refused, are retried.
"""

import copy
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .. import contexts
from ..chats.redact import redact
from ..llm import Client
from . import prompts, remember, said
from .notes import CONFIDENCE, REMOVED_BY_HAND, Note, NoteStore, Rule, clean_title, new_id, placed, rule_id, sync, today

SYSTEM, _ = prompts.load("consolidate")
BATCH = 20
CONTEXT_NOTES = 40
RETIRED_SHOWN = 2  # per note: recent retired rules, so a restated old form reads as outdated
REJECTED_SHOWN = 30  # per kind, in a prompt: notes deleted by hand, and rules removed by hand
MAX_ATTEMPTS = 2
SETTLE_SECONDS = 600  # an emptied body is archived only once the file has been quiet this long
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
    """Fold every new candidate into notes, one group (scope) at a time."""
    fold = Consolidation(con, store, notes, log)
    for group in fold.groups:
        while (request := fold.prepare(group)) is not None:
            fold.apply(group, client.json(request.purpose, request.system, request.prompt, request.schema))
    return fold.finish()


@dataclass
class Request:
    """One model call: client.json(purpose, system, prompt, schema)."""

    purpose: str
    system: str
    prompt: str
    schema: dict


@dataclass
class Group:
    """The new candidates of one scope, taken BATCH at a time, oldest first."""

    scope: Scope
    candidates: list
    rejected: list
    start: int = 0  # the first candidate of the current batch
    prompt: str | None = None  # the current batch's prompt, once prepared
    retry: bool = False  # the answer waved the batch away: ask once more
    deleted_titles: frozenset = frozenset()  # notes the developer deleted in this scope, however many
    removed_rules: frozenset = frozenset()  # rules the developer removed by hand in this scope


class Consolidation:
    """Consolidation as steps, so a caller can send one request per group at a time and
    apply the answers in turn: prepare(group) gives the group's next request (None when the
    group is done), apply(group, answer) checks and applies it, and finish() closes the run.
    A group's batches depend on each other; groups do not, as each holds its own notes."""

    def __init__(self, con, store: NoteStore, notes: dict[str, Note], log=print):
        self.con, self.store, self.notes, self.log = con, store, notes, log
        self.stats = Counter()
        pending: set[str] = set()  # notes whose hand edit is left for the next run
        for note in list(notes.values()):
            if note.edited_by_hand:  # keep the rules read back from a hand edit
                emptied = not note.body.strip() and bool(note.rules)
                if store.changed(note):  # saved again since the notes were read: this copy is stale
                    pending.add(note.id)
                    fresh = store.reread(note)
                    if fresh:
                        notes[note.id] = fresh
                    else:
                        del notes[note.id]
                    self.stats["deferred, note changed on disk"] += 1
                    log(
                        f"  {note.title!r}: changed on disk since it was read, so its hand edit is read on the next run"
                    )
                    continue
                if emptied and _quiet_for(note) < SETTLE_SECONDS:  # an editor may be mid-rewrite
                    pending.add(note.id)
                    self.stats["deferred, emptied note still settling"] += 1
                    log(
                        f"  {note.title!r}: its body was emptied by hand less than {SETTLE_SECONDS // 60} minutes "
                        f"ago, so the note is left for the next run"
                    )
                    continue
                store.write(note)
                self.stats["edited by hand"] += 1
                if emptied:  # nothing is lost: the rules stay in the file, retired
                    log(
                        f"  {note.title!r}: its body was emptied by hand, so its rules were retired as "
                        f"removed by hand and the note archived (git revert undoes it)"
                    )
                if note.status != "active":
                    del notes[note.id]
        # What the developer deleted: whole notes (the ledger) and single rules removed from a note.
        # Each kind has its own cap in the prompt, so a long run of rule edits cannot push a deleted
        # note out; the check in _apply takes all of them.
        removed = sorted(
            (
                (r.retired, {"title": n.title, "body": r.text, "scope": n.scope, "project": n.project})
                for n in store.load(include_archived=True).values()
                if n.id not in pending  # a hand edit left for the next run is not settled: not yet a deletion
                for r in n.rules
                if r.reason == REMOVED_BY_HAND
            ),
            key=lambda pair: pair[0],
        )
        deleted = store.rejected()
        self.groups = []
        for g in con.execute(
            f"""select scope, {_GROUP_KEY} key, max(project_name) name, count(*) n
                from candidates where status='new' group by scope, {_GROUP_KEY} order by n desc"""
        ).fetchall():
            scope = Scope(g["scope"], g["key"] or None, g["name"]) if g["scope"] == "project" else Scope(g["scope"])
            candidates = con.execute(
                f"select * from candidates where status='new' and scope=? and {_GROUP_KEY}=? order by chat_ts, id",
                (g["scope"], g["key"]),
            ).fetchall()
            here = [r for r in deleted if r.get("scope") == scope.kind and r.get("project") == scope.key]
            here_rules = [r for _, r in removed if r.get("scope") == scope.kind and r.get("project") == scope.key]
            self.groups.append(
                Group(
                    scope,
                    candidates,
                    here[-REJECTED_SHOWN:] + here_rules[-REJECTED_SHOWN:],
                    deleted_titles=frozenset(_key(r.get("title") or "") for r in here) - {""},
                    removed_rules=frozenset(_key(r["body"]) for r in here_rules),
                )
            )

    def prepare(self, group: Group) -> Request | None:
        if group.start >= len(group.candidates):
            return None
        if group.prompt is None:
            if group.start == 0:
                self.log(f"consolidating {len(group.candidates)} candidates into {group.scope.label}")
            batch = group.candidates[group.start : group.start + BATCH]
            existing = [n for n in self.notes.values() if group.scope.holds(n)]
            group.prompt = _render(group.scope, _related(existing, batch), batch, group.rejected)
        if group.retry:  # small models sometimes wave a whole batch away with one blanket reason
            return Request("consolidate-retry", SYSTEM, group.prompt + BULK_DISCARD_NUDGE, SCHEMA)
        return Request("consolidate", SYSTEM, group.prompt, SCHEMA)

    def apply(self, group: Group, answer: dict) -> None:
        batch = group.candidates[group.start : group.start + BATCH]
        ops = answer["operations"]
        if not group.retry and _bulk_discard(ops, len(batch)):
            group.retry = True  # the next prepare() asks again
            return
        _apply(self.con, self.store, self.notes, group.scope, batch, ops, self.stats, self.log, group)
        self.con.commit()
        self.stats["batches"] += 1
        group.start, group.prompt, group.retry = group.start + BATCH, None, False

    def finish(self) -> Counter:
        """Close the run: expire stale notes. Call it once prepare() has returned None for every
        group. A group left unfinished (a model call failed, the caller stopped early) still
        holds candidates that may confirm a note, so while any group is unfinished nothing
        expires; a run that completes expires it."""
        unfinished = sum(g.start < len(g.candidates) for g in self.groups)
        if unfinished:
            self.log(f"expiry skipped: {unfinished} groups still have candidates to read")
        else:
            self.stats["expired"] = expire(self.store, self.notes)
        return self.stats


def _quiet_for(note: Note) -> float:
    """Seconds since the note's file was last saved."""
    try:
        return time.time() - os.path.getmtime(note.path)
    except OSError:
        return float("inf")


def expire(store: NoteStore, notes: dict[str, Note]) -> int:
    """Open threads go stale quickly; weak single-source notes fade if never seen again."""
    now, n = time.time(), 0
    for note in list(notes.values()):
        if note.edited_by_hand:  # a hand edit left for the next run: nothing is written from this copy
            continue
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
    con: object  # the index, for the chats' messages (see typed)
    work: dict[str, Note] = field(default_factory=dict)  # copies of the notes operations touch
    created: dict[str, Note] = field(default_factory=dict)
    retired: set[tuple[str, str]] = field(default_factory=set)  # (note id, rule id) retired by an operation
    merged: dict[int, str] = field(default_factory=dict)  # candidate id -> note id
    discarded: dict[int, str] = field(default_factory=dict)  # candidate id -> reason
    asked: dict[int, bool] = field(default_factory=dict, repr=False)  # candidate id -> typed()

    def typed(self, candidate) -> bool:
        """Did the developer type the candidate's evidence (said.py)? Asked once per candidate."""
        if candidate["speaker"] != "user":
            return False
        if candidate["id"] not in self.asked:
            self.asked[candidate["id"]] = said.typed_quote(self.con, candidate["session_id"], candidate["evidence"])
        return self.asked[candidate["id"]]

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


def _apply(
    con,
    store: NoteStore,
    notes: dict[str, Note],
    scope: Scope,
    batch,
    ops,
    stats: Counter,
    log,
    group: Group | None = None,
) -> None:
    by_number = dict(enumerate(batch, 1))
    b = _Batch(notes, scope, con)
    for op in ops:
        candidates = [by_number[i] for i in dict.fromkeys(op["candidates"]) if i in by_number]
        if not candidates:
            continue
        op = {**op, "note_id": op["note_id"].strip().strip("[]"), "rule_id": op["rule_id"].strip().strip("[]")}
        candidates = _remembered(b, op, candidates, stats, log)
        if not candidates:
            continue
        kind, texts = op["op"], _texts(op["rules"])
        fallback = _texts([c["body"] for c in candidates])
        if kind == "discard":
            for c in candidates:
                b.discarded.setdefault(c["id"], redact(op["reason"])[:300] or "discarded")
            continue
        if group and kind != "confirm":  # a confirm brings no words of its own
            removed = group.removed_rules
            proposed = texts or (fallback if kind in ("add", "create") else [])
            title = clean_title(op["title"]) or clean_title(candidates[0]["title"])
            makes_note = kind == "create" or (kind == "add" and not b.note(op["note_id"]))
            # A retire takes words away, so it goes through; any deleted words it offers as a replacement go below.
            if kind != "retire" and (
                (proposed and all(_key(t) in removed for t in proposed))
                or (makes_note and _key(title) in group.deleted_titles)
            ):
                stats["refused, deleted by the developer"] += 1
                log(f"  refused {kind}: the developer deleted this")
                for c in candidates:
                    b.discarded.setdefault(c["id"], "the developer deleted this")
                continue
            texts = [t for t in texts if _key(t) not in removed]
            fallback = [t for t in fallback if _key(t) not in removed]
        when = _date(max(_said(c) for c in candidates))
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
        if found and kind in ("refine", "retire") and not _kept(b, found[1], candidates):
            if kind == "retire":
                stats["refused, the developer's remembered rule"] += 1
                log(f"  refused retire of {found[1].id}: the developer stated it with recall remember")
                for c in candidates:
                    b.discarded.setdefault(c["id"], f"the developer stated rule {found[1].id} with recall remember")
                continue
            kind, texts = "confirm", []  # their words stay; the chat only confirms them
            stats["kept the developer's words"] += 1
        if kind == "add" and not b.note(op["note_id"]):
            kind = "create"  # an unknown or out-of-scope note id: a new note
        if kind == "create":
            note = _create(b, op, texts or fallback, candidates)
        elif found:
            note, rule = found
            if kind == "confirm" or (kind == "refine" and not texts):
                _support(b, rule, candidates)
            elif kind == "refine":
                fresh = not _same(rule.text, texts[0])  # new words: the old ones' speaker says nothing of them
                rule.text = texts[0]
                _support(b, rule, candidates, fresh)
                _insert(note, rule, _new_rules(b, note, texts[1:], candidates))
            else:  # retire
                new = _new_rules(b, note, texts, candidates)
                rule.status, rule.retired, rule.reason = "retired", when, redact(op["reason"].strip())[:300]
                rule.replaced_by = new[0].id if new else None
                b.retired.add((note.id, rule.id))
                _insert(note, rule, new)
            kind = "confirm" if kind == "refine" and not texts else kind
        else:  # add
            note, refused, done = b.note(op["note_id"]), None, False
            for text in texts or fallback:
                same = next((r for r in note.rules if r.active and _same(r.text, text)), None)
                gone = next((r for r in note.rules if not r.active and _same(r.text, text) and when < r.retired), None)
                if same:
                    _support(b, same, candidates)
                elif gone:  # a chat read late cannot bring back a rule that was retired after it
                    refused = gone
                    continue
                else:
                    note.rules += _new_rules(b, note, [text], candidates)
                done = True
            if not done and refused:  # every text was a retired rule an older chat tried to bring back
                stats["refused, older than the rule"] += 1
                log(f"  refused add of {refused.id}: candidate from {when}, rule retired {refused.retired}")
                for c in candidates:
                    b.discarded.setdefault(c["id"], f"older than rule {refused.id}, retired {refused.retired}")
                continue
        if kind != "create" and clean_title(op["title"]):
            note.title = clean_title(op["title"])
        note.confidence = max(note.confidence, op["confidence"], key=lambda c: CONFIDENCE.get(c, 0))
        stats[
            {"confirm": "confirmed", "refine": "refined", "retire": "retired", "add": "added"}.get(kind, "created")
        ] += 1
        for c in candidates:
            b.merged.setdefault(c["id"], note.id)
    _restore(b, stats, log)
    _commit(con, store, notes, b, by_number, stats, log)


def _kept(b: _Batch, rule: Rule, candidates) -> bool:
    """May these candidates refine or retire this rule? Any rule but one the developer stated
    with `recall remember`, which only the developer can change: a chat newer than the remember,
    other than the one it ran in, with a quote they typed. A chat that ran on after the remember
    has a later timestamp, but what it says may come from before, so that chat never counts: the
    chat must have started after the remember (a rule typed at a terminal has no source chat to
    exclude, and an older chat still open would otherwise beat it)."""
    stated = remember.stamp_of(rule)
    if stated is None or rule.said_by != "developer":
        return True
    return any(
        (c["chat_ts"] or 0) > stated
        and c["session_id"] not in rule.sources
        and _started_after(b.con, c["session_id"], stated)
        and b.typed(c)
        for c in candidates
    )


def _started_after(con, session_id: str, stamp: float) -> bool:
    try:
        row = con.execute("select started_at from sessions where id=?", (session_id,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] and row[0] > stamp)


def _remembered(b: _Batch, op: dict, candidates: list, stats: Counter, log) -> list:
    """Apply what an operation does with candidates from `recall remember`, and return the other
    candidates for the operation to go on with. Such a candidate's rule is already in the notes,
    so it is merged into that rule's note whatever the operation. On top of that, a rule the
    developer stated:
    - retires the rule the operation names when it is an older one it replaces;
    - retires the rule a confirm or refine names when that rule says the same (each covers
      most of the other's words: said.supports both ways), folding it in.
    The operation's other candidates then state the remembered rule, and support it; otherwise
    they go on with the operation. An agent's remembered rule (said_by agent) changes nothing else."""
    rest = [c for c in candidates if not remember.anchor(c["session_id"])]
    for c in candidates:
        if not (saved := remember.anchor(c["session_id"])):
            continue
        note_id, rid = saved
        own = b.rule(note_id, rid)
        if own is None:  # deleted or edited away by hand since: nothing to merge into
            b.discarded.setdefault(c["id"], f"remembered rule {rid} is no longer in the notes")
            continue
        b.merged.setdefault(c["id"], own[0].id)
        found = b.rule(op["note_id"], op["rule_id"]) if op["op"] in ("confirm", "refine", "retire") else None
        if not found or found[1] is own[1] or c["speaker"] != "user" or own[1].said_by != "developer":
            continue
        note, rule = found
        if note.scope == own[0].scope == "global":
            table = contexts.roots()
            if placed(note, table) != placed(own[0], table):
                continue  # the same words in another context are another rule: it keeps its own
        stated = remember.stamp_of(own[1]) or c["chat_ts"] or 0
        if (remember.stamp_of(rule) or 0) >= stated or _date(stated) < rule.seen:
            continue  # the other rule is as new as this one: nothing to replace
        same = said.supports(own[1].text, rule.text) and said.supports(rule.text, own[1].text)
        if op["op"] == "retire" or same:
            rule.status, rule.retired = "retired", _date(stated)
            how = "replaced" if op["op"] == "retire" else "restated"
            rule.reason = f"{how} by the developer's rule {own[1].id} in {own[0].id}"
            b.retired.add((note.id, rule.id))
            stats["retired by a remembered rule"] += 1
            log(f"  retired {rule.id} in {note.id}: {rule.reason}")
            if rest:
                _support(b, own[1], rest)
                for r in rest:
                    b.merged.setdefault(r["id"], own[0].id)
                rest = []
    return rest


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


def _commit(con, store: NoteStore, notes: dict[str, Note], b: _Batch, by_number: dict, stats: Counter, log) -> None:
    deferred: set[int] = set()  # candidates merged into a note that was not written
    for note in [*b.work.values(), *b.created.values()]:
        sync(note)
        if len(note.sources) >= 3:
            note.confidence = "high"  # corroborated across several chats
        if note.id in b.work and _state(note) == _state(b.notes[note.id]):
            continue  # looked at, not changed
        if note.id in b.work and (b.notes[note.id].edited_by_hand or store.changed(b.notes[note.id])):
            # The file changed (a hand edit) or went while the batch ran, so this copy is stale and
            # writing it would undo that; or its hand edit was left for the next run (see
            # Consolidation). Later batches work from the file as it is now; these candidates stay
            # new, and the next run folds them in.
            why = "has a hand edit waiting" if b.notes[note.id].edited_by_hand else "changed on disk during the batch"
            fresh = store.reread(b.notes[note.id])
            if fresh and fresh.status == "active":
                notes[note.id] = fresh
            else:
                notes.pop(note.id, None)
            deferred |= {c for c, n in b.merged.items() if n == note.id}
            stats["deferred, note changed on disk"] += 1
            log(f"  not written: {note.title!r} {why}; its candidates stay for the next run")
            continue
        if note.id in b.work and note.reach == "everywhere" and _new_words(b.notes[note.id], note):
            note.reach = None  # the verdict was on other words: judged again, and held to its contexts until then
            stats["reach reopened"] += 1
        note.updated = today()
        store.write(note)  # also renders the body, and archives a note with no rule left
        if note.status == "active":
            notes[note.id] = note
        else:
            notes.pop(note.id, None)
    con.executemany(
        "update candidates set status='merged', note_id=? where id=?",
        [(n, c) for c, n in b.merged.items() if c not in deferred],
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


def _new_words(before: Note, after: Note) -> bool:
    """Does the note now state something it did not: a new rule, or one reworded?"""
    had = {_key(r.text) for r in before.active_rules}
    return any(_key(r.text) not in had for r in after.active_rules)


def _state(note: Note) -> tuple:
    before = copy.deepcopy(note)
    sync(before)
    return before.title, before.confidence, before.status, [asdict(r) for r in before.rules]


def _create(b: _Batch, op: dict, texts: list[str], candidates) -> Note:
    first = _date(min(c["chat_ts"] or 0 for c in candidates))
    title = clean_title(op["title"]) or clean_title(candidates[0]["title"])
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
    note.rules = _new_rules(b, note, texts, candidates)
    sync(note)
    b.created[note.id] = note
    return note


def _new_rules(b: _Batch, note: Note, texts: list[str], candidates) -> list[Rule]:
    taken = {r.id for r in note.rules}
    out = []
    for text in texts:
        rule = Rule(rule_id(note.id, text, taken), text, first_seen=_date(min(c["chat_ts"] or 0 for c in candidates)))
        taken.add(rule.id)
        _support(b, rule, candidates, fresh=True)
        out.append(rule)
    return out


def _support(b: _Batch, rule: Rule, candidates, fresh: bool = False) -> None:
    """Record that these candidates state the rule: dates, source chats, who said it, a quote.
    `fresh`: the rule's words are new (a new rule, or a refine that rewrote it). Who said it:
    - developer: a candidate attributed to the user has a quote that the developer typed and
      that supports this rule's text (said.py), and the rule asks for no command, URL, network,
      destructive or exfiltrating action (said.risky). Each rule is judged on its own: one
      quote does not vouch for the other rules written beside it.
    - agent: no typed quote supports it, and only the agent stated it: every candidate behind
      new words is the agent's, or the rule was the agent's before it was reworded. It keeps
      the mark until a typed quote supports it, since an untyped user turn may be a paste.
    - unverified: new words stated in a user turn that no typed quote supports.
    Unset is left only on rules written before speakers were recorded (notes.pushed shows those
    as background), and confirming one changes nothing but a typed quote."""
    rule.last_seen = max(rule.last_seen, _date(max(c["chat_ts"] or 0 for c in candidates)))
    chats = {c["session_id"] for c in candidates if not remember.anchor(c["session_id"])}
    rule.sources = sorted(set(rule.sources) | chats)
    risky = said.risky(rule.text)
    typed = (
        None if risky else next((c for c in candidates if b.typed(c) and said.supports(c["evidence"], rule.text)), None)
    )
    if typed:
        if rule.said_by != "developer":
            rule.said_by, rule.evidence = "developer", said.clean_rule(typed["evidence"] or "")[:200]
    elif fresh:
        agent = rule.said_by == "agent" or all(c["speaker"] == "assistant" for c in candidates)
        rule.said_by = "agent" if agent else "unverified"
        rule.evidence = said.clean_rule(next((c["evidence"] for c in candidates if c["evidence"]), ""))[:200]
    elif risky and rule.said_by == "developer":  # a risky rule never keeps the developer's mark
        rule.said_by = "unverified"


def _insert(note: Note, after: Rule, new: list[Rule]) -> None:
    """New rules go where the rule they come from stands, so the body keeps its order."""
    at = note.rules.index(after)
    note.rules[at + 1 : at + 1] = new
    if not after.active:  # a replacement takes the retired rule's place
        note.rules[at : at + 1 + len(new)] = [*new, after]


def _texts(values: list[str]) -> list[str]:
    """Rule texts on one line each, without a literal said_by marker (said.clean_rule)."""
    return [t for t in (said.clean_rule(" ".join(v.split())) for v in values) if t]


def _said(c) -> float:
    """The latest a candidate can have been said (extract.store); a row from before said_at has only chat_ts."""
    return c["said_at"] if c["said_at"] is not None else c["chat_ts"] or 0


def _key(text: str) -> str:
    return " ".join(text.split()).lower()


def _same(a: str, b: str) -> bool:
    return _key(a) == _key(b)


# --- the prompt ---------------------------------------------------------------------------


def _render(scope: Scope, existing: list[Note], batch, rejected) -> str:
    lines = [f"Scope: {scope.label}", ""]
    if rejected:
        # A deleted note is named by its title alone (notes.py keeps no more of it); a rule
        # removed from a note by hand is still in that note's file, retired, and is quoted.
        lines.append("Deleted by the developer (never recreate these; discard candidates that restate them):")
        lines += [f"- {r['title']}" + (f": {r['body'][:200]}" if r.get("body") else "") for r in rejected]
        lines.append("")
    lines.append("Existing notes, each with its rules: [rule id] (first stated to last stated) text")
    for n in existing:
        lines.append(f"[{n.id}] ({n.type}, {n.confidence}, {len(n.sources)} source chats) {n.title}")
        for r in n.rules:
            if r.active:
                own = ", the developer's own words" if r.stated and r.said_by == "developer" else ""
                lines.append(f"  - [{r.id}] ({r.first_seen or '?'} to {r.seen or '?'}{own}) {' '.join(r.text.split())}")
        retired = sorted((r for r in n.rules if not r.active), key=lambda r: r.retired)[-RETIRED_SHOWN:]
        for r in retired:
            lines.append(f"  - retired {r.retired or '?'}, no longer holds: {' '.join(r.text.split())[:160]}")
    if not existing:
        lines.append("(none yet)")
    lines += ["", "New candidates:"]
    for i, c in enumerate(batch, 1):
        why = f" Why: {c['why']}" if c["why"] else ""
        if saved := remember.anchor(c["session_id"]):  # the developer stated it: already rule saved[1]
            lines.append(
                f"{i}. ({_date(c['chat_ts'])}, {c['type']}, stated by the "
                f"{'developer' if c['speaker'] == 'user' else 'agent'} with recall remember, already saved as rule "
                f"[{saved[1]}] in note [{saved[0]}]) {c['body']}"
            )
            continue
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

"""Memory notes: one markdown file per memory, the source of truth.

    ~/.agents/memory/
      MEMORY.md                    global index: how you work, plus a list of projects
      global/<id>.md               cross-project preferences
      projects/<name>/MEMORY.md    per-project index, what agents load at session start
      projects/<name>/<id>.md      project facts, decisions, lessons, open threads
      unfiled/<id>.md              project facts from chats outside any project folder
      archive/...                  superseded and expired notes, same layout
      .recall/                     project registry and deletion ledger

Notes are plain markdown with YAML frontmatter, so people can read and edit them,
or open the folder as an Obsidian vault. Each note ends with wikilinks to its most
related notes, which recall rewrites on every run (see links.py). The SQLite index
is rebuilt from these files, so hand edits always win. The folder is a git repository and every memory
run is one commit: `git log` shows what changed and `git revert` undoes it.

A note is a titled list of rules, each a statement that can change on its own. The
frontmatter keeps every rule with its dates, sources and status; the body is rendered
from the rules still active (one rule: its text; several: a bullet list), so whatever
reads the body sees only what holds now. A retired rule stays in the frontmatter with
the date, the reason and the rule that replaced it. Notes written before rules existed
read as a note with one rule. The body stays the place to edit: an edit there is read
back into the rules (see _edits).
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from difflib import SequenceMatcher

import yaml

from .. import contexts, paths
from ..chats.redact import redact
from .said import clean_rule, has_marker, risky

TYPES = ("preference", "fact", "decision", "lesson", "open")
SECTIONS = {
    "preference": "Preferences",
    "fact": "Facts",
    "decision": "Decisions",
    "lesson": "Lessons",
    "open": "Open threads",
}
CONFIDENCE = {"high": 1.0, "medium": 0.7, "low": 0.4}
GLOBAL_INDEX_LINES = 40
PROJECT_INDEX_LINES = {"preference": 12, "fact": 30, "decision": 20, "lesson": 20, "open": 10}
RELATED_MARK = "<!-- recall: related notes, rewritten on each run -->"
REMOVED_BY_HAND = "removed by hand"  # the only reason that feeds consolidation's never-recreate list
MERGED_BY_HAND = "merged by hand"  # the developer folded it into another rule, which replaced it
HOOK_CHARS = 160
README = (
    "# Agent memory\n\nMaintained by recall from your coding-agent chats. Start at MEMORY.md. "
    "Every note is plain markdown: edit or delete anything, and recall respects it on its next run. "
    "Each run is one git commit, so `git log` shows what changed and `git revert` undoes it.\n"
)


@dataclass
class Rule:
    """One statement in a note that can change on its own: a convention, a fact, a decision."""

    id: str  # unique within its note and never reused, so operations can name it
    text: str
    first_seen: str = ""  # date of the earliest chat that stated it
    last_seen: str = ""  # date of the latest chat that stated or confirmed it
    sources: list[str] = field(default_factory=list)  # chat ids
    said_by: str | None = (
        None  # "developer" | "agent" | unset: a hint about who stated it, not authentication (said.py)
    )
    evidence: str = ""  # a supporting quote from a chat
    status: str = "active"  # "active" | "retired"
    retired: str = ""  # date it stopped holding
    reason: str = ""  # why it was retired
    replaced_by: str | None = None  # id of the rule that replaced it, in the same note
    edited: str = ""  # date the developer last changed it by hand

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def seen(self) -> str:
        """The newest date the rule is known to hold: last stated in a chat, or edited by hand."""
        return max(self.last_seen, self.edited)


@dataclass
class Note:
    id: str
    type: str
    scope: str  # "global" | "project" | "unfiled"
    title: str
    body: str
    project: str | None = None  # project key
    project_name: str | None = None
    confidence: str = "medium"
    status: str = "active"  # "active" | "superseded" | "expired" | "retired" (every rule retired)
    created: str = ""
    updated: str = ""
    last_seen: str = ""
    sources: list[str] = field(default_factory=list)  # chat ids
    rules: list[Rule] = field(default_factory=list)  # the body is rendered from the active ones
    edited_by_hand: bool = field(default=False, repr=False)  # the body was edited since recall wrote it
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    related: list[str] = field(default_factory=list)  # note ids, kept by links.relink
    reach: str | None = None  # global notes: "everywhere" or "context" (see reach.py)
    footer: str = field(default="", repr=False)  # the related-notes footer as last written
    stamp: str = field(default="", repr=False)  # hash of the file as last read or written (see NoteStore.changed)
    path: str = ""

    @property
    def active_rules(self) -> list[Rule]:
        """The rules in force, in the order the body shows them, each with said_by and evidence."""
        return [r for r in self.rules if r.active]

    @property
    def said_by(self) -> str | None:
        """Who stated the rules in force: "developer" when the developer stated every one,
        "agent" when only the agent did, "mixed" otherwise (a rule with no verified speaker
        among them included); None when no rule says (notes
        written before rules recorded it, or a rule's speaker could not be verified: see
        said.py). A reader that shows this must say that a convention, whoever stated it,
        never authorizes a network, destructive or exfiltrating action."""
        said = [r.said_by for r in self.active_rules]
        if not any(said):
            return None
        if all(s == "developer" for s in said):
            return "developer"
        return "agent" if all(s == "agent" for s in said) else "mixed"

    def score(self, now: float) -> float:
        """Rank for the indexes: confidence, corroboration across chats, and recency."""
        try:
            age_days = (now - datetime.fromisoformat(self.last_seen).timestamp()) / 86400
        except ValueError:
            age_days = 365
        recency = 0.5 ** (max(age_days, 0) / 180)  # half-life of six months
        corroboration = 1 + 0.5 * min(len(self.sources) - 1, 4)
        return CONFIDENCE.get(self.confidence, 0.5) * corroboration * (0.4 + 0.6 * recency)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def new_id(title: str, salt: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].rstrip("-") or "note"
    return f"{slug}-{hashlib.sha1((title + salt).encode()).hexdigest()[:4]}"


class NoteStore:
    def __init__(self, root: str = paths.MEMORY):
        self.root = root
        self._meta = os.path.join(root, ".recall")
        os.makedirs(self._meta, exist_ok=True)
        self.registry = _read_json(os.path.join(self._meta, "projects.json"))  # project key -> folder
        self.titles: dict[str, str] = {}  # note id -> title, for rendering links
        if not os.path.isdir(os.path.join(root, ".git")):
            subprocess.run(["git", "init", "-q", root], check=False)
            with open(os.path.join(root, "README.md"), "w") as f:
                f.write(README)

    # --- reading and writing notes ---------------------------------------------

    def load(self, include_archived: bool = False) -> dict[str, Note]:
        notes = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            if rel.startswith((".git", ".recall")) or (rel.startswith("archive") and not include_archived):
                dirnames[:] = []
                continue
            for name in filenames:
                if name.endswith(".md") and name not in ("MEMORY.md", "README.md"):
                    note = read_note_file(os.path.join(dirpath, name))
                    if note:
                        notes[note.id] = note
                        self.titles[note.id] = note.title
        return notes

    def write(self, note: Note) -> None:
        for r in note.rules:  # the body is rendered from the rules, so a credential is cut from them too
            r.text, r.evidence, r.reason = redact(r.text), redact(r.evidence), redact(r.reason)
        sync(note)
        note.title, note.body = redact(note.title), redact(note.body)  # a note never keeps a credential
        directory = self._dir_for(note)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{note.id}.md")
        if note.path and os.path.abspath(note.path) != os.path.abspath(path) and os.path.exists(note.path):
            os.remove(note.path)  # the note moved: archived, or its project folder changed
        meta = {k: v for k, v in asdict(note).items() if k not in _NOT_FRONTMATTER and v not in (None, [], "")}
        if note.rules:
            meta["rules"] = [_rule_meta(r) for r in note.rules]
        note.edited_by_hand = False
        self.titles[note.id] = note.title
        note.footer = self.footer(note)
        text = (
            "---\n"
            + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100)
            + "---\n"
            + note.body.strip()
            + (f"\n\n{note.footer}" if note.footer else "")
            + "\n"
        )
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        note.path, note.stamp = path, _stamp(text)

    def reread(self, note: Note) -> Note | None:
        """The note as its file is now, for a step that changes one field after a long run: a
        hand edit made since the notes were loaded survives."""
        return read_note_file(note.path) if note.path else None

    def changed(self, note: Note) -> bool:
        """Has the note's file changed (or gone) since it was read or written? A step that
        holds a note for a long run asks before writing it, so a hand edit made meanwhile is
        not overwritten with the stale copy. A note with no stamp (built in memory) cannot say."""
        if not (note.path and note.stamp):
            return False
        try:
            with open(note.path, encoding="utf-8") as f:
                return _stamp(f.read()) != note.stamp
        except OSError:
            return True

    def footer(self, note: Note) -> str:
        """Wikilinks to the note's related notes, titled so they read well in Obsidian."""
        if not note.related:
            return ""
        links = " · ".join(f"[[{i}|{_link_text(self.titles.get(i, i))}]]" for i in note.related)
        return f"{RELATED_MARK}\nRelated: {links}"

    def project_folder(self, key: str, name: str) -> str:
        if key in self.registry:
            return self.registry[key]["folder"]
        folder = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"
        if folder in {e["folder"] for e in self.registry.values()}:
            folder = f"{folder}-{hashlib.sha1(key.encode()).hexdigest()[:4]}"
        self.registry[key] = {"folder": folder, "name": name}
        _write_json(os.path.join(self._meta, "projects.json"), self.registry)
        return folder

    def _dir_for(self, note: Note) -> str:
        base = self.root if note.status == "active" else os.path.join(self.root, "archive")
        if note.scope == "global":
            return os.path.join(base, "global")
        if note.scope == "unfiled" or not note.project:
            return os.path.join(base, "unfiled")
        return os.path.join(base, "projects", self.project_folder(note.project, note.project_name or "project"))

    # --- deletions --------------------------------------------------------------
    # A note the person deletes must stay deleted; the next run would otherwise
    # learn it again from the same chats. The ledger (.recall/notes.json, committed with
    # the notes) keeps only what that takes: each note's title, scope and project. Its
    # words go once the person deletes it, so they are neither kept nor sent to a model.

    def rejected(self) -> list[dict]:
        return list(self._read_ledger().get("rejected", {}).values())

    def reconcile(self, notes: dict[str, Note]) -> int:
        """Record notes deleted by hand as rejected; remember the rest as known. A rejected
        note back on disk (a git revert) is no longer rejected. Returns how many deletions
        were found."""
        ledger = self._read_ledger()
        known, rejected = ledger.get("known", {}), ledger.get("rejected", {})
        on_disk = set(notes) | set(self.load(include_archived=True))
        gone = [k for k in known if k not in on_disk]
        for k in gone:
            rejected[k] = known[k]
        rejected = {k: v for k, v in rejected.items() if k not in on_disk}
        known = {n.id: _ledger_entry(n.title, n.scope, n.project) for n in notes.values()}
        _write_json(self._ledger, {"known": known, "rejected": rejected})
        return len(gone)

    def _read_ledger(self) -> dict:
        """The ledger, rewritten without the note bodies that earlier releases kept in it."""
        ledger = _read_json(self._ledger)
        parts = {p: ledger.get(p) or {} for p in ("known", "rejected")}
        if any(isinstance(v, dict) and v.get("body") for part in parts.values() for v in part.values()):
            ledger = {
                p: {
                    k: _ledger_entry(v.get("title"), v.get("scope"), v.get("project"))
                    for k, v in part.items()
                    if isinstance(v, dict)
                }
                for p, part in parts.items()
            }
            _write_json(self._ledger, ledger)
        return ledger

    @property
    def _ledger(self) -> str:
        return os.path.join(self._meta, "notes.json")

    # --- indexes ----------------------------------------------------------------

    def compile(self, notes: dict[str, Note]) -> None:
        """Rewrite MEMORY.md and each project's MEMORY.md from the notes."""
        now = time.time()
        active = [n for n in notes.values() if n.status == "active"]
        by_project: dict[str, list[Note]] = {}
        prefs = []
        for n in active:
            if n.scope == "project" and n.project:
                by_project.setdefault(n.project, []).append(n)
            elif n.scope == "global":
                # A preference tied to a context but only ever seen in one project is that
                # project's preference, so it is listed there rather than across the context.
                seen = contexts.projects_of_chats(n.sources) if n.reach == "context" else set()
                if len(seen) == 1:
                    by_project.setdefault(seen.pop(), []).append(n)
                else:
                    prefs.append(n)
        header = (
            f"_Maintained by recall from your coding-agent chats; last updated {today()}. "
            "Each line links to a note. Edit or delete notes freely._\n"
        )
        project_lines = [
            self._compile_project(key, items, header, now)
            for key, items in sorted(by_project.items(), key=lambda kv: -len(kv[1]))
        ]
        prefs.sort(key=lambda n: -n.score(now))
        out = ["# Memory\n", header]
        for heading, items in _by_context(prefs):
            out.append(f"\n## {heading}\n")
            lines, more = _listed(items, GLOBAL_INDEX_LINES, lambda n: f"global/{n.id}.md")
            out += lines or ([] if items else ["- (nothing yet)"])
            if more:
                out.append(f"- …and {more} more in global/")
        out += ["\n## Projects\n", *(project_lines or ["- (nothing yet)"])]
        _write_text(os.path.join(self.root, "MEMORY.md"), "\n".join(out) + "\n")

    def _compile_project(self, key: str, items: list[Note], header: str, now: float) -> str:
        name = next((n.project_name for n in items if n.project_name), None) or key.rsplit("/", 1)[-1]
        folder = self.project_folder(key, name)
        repo = key.split(":", 1)[1]
        out = [f"# {name}: project memory\n", f"Repo: `{repo}`  \n{header}"]
        for t in TYPES:
            ranked = sorted((n for n in items if n.type == t), key=lambda n: -n.score(now))
            if not ranked:
                continue
            out.append(f"\n## {SECTIONS[t]}\n")
            lines, more = _listed(
                ranked,
                PROJECT_INDEX_LINES[t],
                lambda n: f"{n.id}.md" if n.scope == "project" else f"../../global/{n.id}.md",
            )
            out += lines
            if more:
                out.append(f"- …and {more} more in this folder")
        _write_text(os.path.join(self.root, "projects", folder, "MEMORY.md"), "\n".join(out) + "\n")
        return f"- **{name}**: {len(items)} notes ([index](projects/{folder}/MEMORY.md)) · `{repo}`"

    def commit(self, message: str) -> str | None:
        """Commit every change in the memory folder; returns the short sha, or None if nothing changed."""
        git = ["git", "-C", self.root]
        subprocess.run([*git, "add", "-A"], capture_output=True)
        r = subprocess.run(
            [*git, "-c", "user.name=recall", "-c", "user.email=recall@localhost", "commit", "-q", "-m", message],
            capture_output=True,
        )
        if r.returncode != 0:
            return None
        return subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()


@contextmanager
def writing(timeout: float = 0):
    """Take turns changing notes: a memory run folding in new memories, and `recall memory
    link` recomputing links. Yields False if the lock was not free within `timeout` seconds."""
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "notes.lock"), "w") as lock:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        yield True


def read_note_file(path: str) -> Note | None:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    return parse_note(text, path)


def parse_note(text: str, path: str) -> Note | None:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    fields = {k: v for k, v in meta.items() if k in Note.__dataclass_fields__ and k not in _NOT_FRONTMATTER}
    fields.setdefault("id", os.path.basename(path)[:-3])
    fields.setdefault("type", "fact")
    fields.setdefault("scope", "project")
    fields.setdefault("title", fields["id"])
    for k in ("created", "updated", "last_seen"):
        if k in fields:
            fields[k] = str(fields[k])  # YAML reads bare dates as date objects
    body, mark, footer = m.group(2).partition(RELATED_MARK)
    related = [i.strip() for i in re.findall(r"\[\[([^\]|#]+)", footer)]
    note = Note(
        **fields, body=body.strip(), related=related, footer=(mark + footer).strip(), stamp=_stamp(text), path=path
    )
    rules = [r for r in map(_rule_from, meta.get("rules") or []) if r]
    if rules:
        note.rules, note.edited_by_hand = _edits(note, rules, _edit_date(path))
    elif note.body:  # written before notes held rules: the whole body is one rule
        note.rules = [
            Rule(
                id=rule_id(note.id, "", set()),
                text=note.body,
                first_seen=note.created or note.last_seen,
                last_seen=note.last_seen,
                sources=list(note.sources),
            )
        ]
    return note


_NOT_FRONTMATTER = ("body", "path", "related", "footer", "rules", "edited_by_hand", "stamp")  # rules: see _rule_meta


def _stamp(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# --- rules ----------------------------------------------------------------------


def rule_id(note_id: str, text: str, taken: set[str]) -> str:
    """A short id for a new rule, stable for the same note and text, unused in the note."""
    n = 0
    while True:
        rid = "r" + hashlib.sha1(f"{note_id}\n{text}\n{n}".encode()).hexdigest()[:5]
        if rid not in taken:
            return rid
        n += 1


def render(rules: list[Rule]) -> str:
    """A note's body: its one active rule, or a bullet list of several. A note with no rule
    left in force (archived) shows the rules it last held, so the archive still reads."""
    shown = [r for r in rules if r.active] or rules
    if len(shown) == 1:
        return shown[0].text.strip()
    return "\n".join("- " + r.text.strip().replace("\n", "\n  ") for r in shown)


def sync(note: Note) -> None:
    """Set the body and the note-wide fields from the rules: active rules first, in order."""
    if not note.rules:
        return
    live = [r for r in note.rules if r.active]
    note.rules = live + [r for r in note.rules if not r.active]
    if not live and note.status == "active":
        note.status = "retired"  # every rule retired: the note moves to the archive
    note.body = render(note.rules)
    note.sources = sorted(set(note.sources).union(*(r.sources for r in note.rules)))
    note.last_seen = max((r.seen for r in live if r.seen), default=note.last_seen)
    note.created = min([r.first_seen for r in note.rules if r.first_seen] + [note.created], key=lambda d: d or "9")


def _rule_meta(rule: Rule) -> dict:
    meta = {k: v for k, v in asdict(rule).items() if v not in (None, [], "")}
    meta["status"] = rule.status
    return meta


def _rule_from(entry) -> Rule | None:
    if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
        return None
    known = {k: v for k, v in entry.items() if k in Rule.__dataclass_fields__}
    for k in ("first_seen", "last_seen", "retired", "edited"):
        if k in known:
            known[k] = str(known[k]) if known[k] else ""
    known["text"] = str(known["text"]).strip()
    known["evidence"] = str(known.get("evidence") or "")
    if risky(known["text"]) and known.get("said_by") == "developer":
        known["said_by"] = None  # edited into a command or a URL: no longer vouched for
    known["sources"] = [str(s) for s in known.get("sources") or []]
    known.setdefault("id", rule_id("", known["text"], set()))
    known["id"] = str(known["id"])
    return Rule(**known)


def _edit_date(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except OSError:
        return today()


_BULLET = re.compile(r"^(?:[-*+]|\d{1,3}[.)]) +")


def _split(body: str) -> tuple[str, list[str]]:
    """A body as the text above its list and the list's items; a body with no list is a single
    item with nothing above it."""
    text = body.strip()
    if not text:
        return "", []
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if _BULLET.match(line)), None)
    if first is None:
        return "", [text]
    out: list[str] = []
    for line in lines[first:]:
        if _BULLET.match(line):
            out.append(_BULLET.sub("", line, count=1))
        else:
            out[-1] += "\n" + (line[2:] if line.startswith("  ") else line)
    return "\n".join(lines[:first]).strip(), [t for t in (t.strip() for t in out) if t]


def _items(body: str) -> list[str]:
    """A body as rule texts, as written: the items of a bullet or numbered list, with the lines
    before the first item as one more; a body with no list is a single item."""
    lead, items = _split(body)
    return ([lead] if lead else []) + items


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _inside(text: str, item: str) -> bool:
    """Do the words of `text`, ignoring case and punctuation, stand in `item` in that order?"""
    words = _words(text)
    return bool(words) and f" {words} " in f" {_words(item)} "


def _overlap(words: set[str], item: str) -> float:
    """The share of `words` that are in `item`."""
    return len(words & set(_words(item).split())) / len(words)


def _close(a: str, b: str) -> float:
    return SequenceMatcher(None, " ".join(a.lower().split()), " ".join(b.lower().split())).ratio()


def _edits(note: Note, rules: list[Rule], date: str) -> tuple[list[Rule], bool]:
    """Read a hand edit of the body back into the rules, losing no words and keeping a rule's
    id, sources and dates whenever its text still stands. Each item of the body (_items) is
    matched to a rule, in this order:
    - the same words: the rule is kept as it is; a retired rule's words written back revive it;
    - a rule that is a list of two or more bullets of its own (a note from before rules) is
      split into one rule per bullet, and one for any text above the bullets, first, each
      keeping the parent's sources and dates, and matched alike;
    - an item that contains a rule's words (a paragraph or intro added, bullets turned to
      prose) is that rule, with the new text. If it holds several rules' words they are merged
      into the first, which keeps their sources, and the others are retired as merged. Text
      added above the list of a note that had several rules is the developer's own: if it quotes
      one rule inside more than twice as many words (an explanation of why a bullet was
      deleted) it is a new rule, not that rule's new text;
    - a changed line close to a rule (similarity of at least a half) is that rule, reworded;
    - any other item is a new rule in the developer's words.
    A rule left over is retired. One that went into the words of a rule the edit changed is
    retired as merged, with that rule as its replacement and its sources folded in. Any other
    was deleted, even when its words stand in a rule that stayed as it was (a shorter version
    of it), and is retired as removed by hand, the reason that stops it being learned again.
    An emptied body removes every rule.
    A hand-written rule is marked said_by developer unless it is risky (said.py); an edit that
    only adds to a rule keeps the mark only when every rule behind it had it. Returns the rules
    and whether the body had been edited."""
    if _same(render(rules), note.body):
        return rules, False
    lead, bullets = _split(note.body)
    items = ([lead] if lead else []) + bullets
    several = sum(r.active for r in rules) > 1  # the body was a list, so text above it is new
    taken = {r.id for r in rules}
    free = [r for r in rules if r.active]
    held: dict[int, Rule] = {}  # item index -> the rule it is
    hosts: dict[str, str] = {}  # rule id -> the item text of a rule that merged others

    def exact() -> None:
        for i, t in enumerate(items):
            if i not in held and (hit := next((r for r in free if _same(r.text, t)), None)):
                held[i] = hit
                free.remove(hit)

    def retire(rule: Rule, reason: str, host: Rule | None = None) -> None:
        rule.status, rule.retired, rule.reason = "retired", date, reason
        if host:
            rule.replaced_by = host.id
            host.sources = sorted({*host.sources, *rule.sources})
            host.first_seen = min([d for d in (host.first_seen, rule.first_seen) if d], default="")
            host.last_seen = max(host.last_seen, rule.last_seen)

    exact()
    for i, t in enumerate(items):  # a retired rule's words written back bring it back
        back = None if i in held else next((r for r in rules if not r.active and _same(r.text, t)), None)
        if back and back.id not in {r.id for r in held.values()}:
            back.status, back.retired, back.reason, back.replaced_by, back.edited = "active", "", "", None, date
            held[i] = back
    for r in list(free):  # a legacy list: one rule per bullet, and one for the text above it
        top, bullets_of = _split(r.text)
        if len(bullets_of) < 2:  # a bullet or two under a rule is its detail, not a list
            continue
        pieces = ([top] if top else []) + bullets_of
        units = [
            replace(
                r,
                id=r.id if k == 0 else rule_id(note.id, piece, taken),
                text=piece,
                sources=list(r.sources),
                evidence=r.evidence if k == 0 else "",
                said_by=None if risky(piece) else r.said_by,
            )
            for k, piece in enumerate(pieces)
        ]
        taken.update(u.id for u in units)
        rules = [u for x in rules for u in (units if x is r else [x])]
        free = [u for x in free for u in (units if x is r else [x])]
    exact()
    stayed = set(held)  # items that are a rule exactly as it was
    quoting: set[int] = set()  # items that only quote a rule: a new rule, not that rule reworded
    for i, t in enumerate(items):  # an item that holds a rule's words
        inside = [r for r in free if _inside(r.text, t)] if i not in held else []
        if not inside:
            continue
        only = inside[0] if len(inside) == 1 else None
        if only and i == 0 and lead and several and 2 * len(_words(only.text).split()) < len(_words(t).split()):
            quoting.add(i)  # text added above the list that quotes one rule inside much else
            continue
        at = _words(t)
        inside.sort(key=lambda r: f" {at} ".find(f" {_words(r.text)} "))  # the rule the item opens with is the host
        host, others = inside[0], inside[1:]
        host.said_by = "developer" if all(r.said_by == "developer" for r in inside) and not risky(t) else None
        host.text, host.edited = t if has_marker(host.text) else clean_rule(t), date
        for r in others:
            retire(r, MERGED_BY_HAND, host)
        if others:
            hosts[host.id] = t
        held[i] = host
        free = [r for r in free if r is not host and not any(r is o for o in others)]
    for r in list(free):  # its words stand in a rule that stayed as it was: this one was deleted
        if any(_inside(r.text, items[i]) for i in stayed):
            retire(r, REMOVED_BY_HAND)
            free.remove(r)
    for i, t in enumerate(items):
        if i in held:
            continue
        close = None if i in quoting else max(free, key=lambda r: _close(r.text, t), default=None)
        if close and _close(close.text, t) >= 0.5:  # a changed line: the developer reworded the rule
            close.text, close.edited, close.evidence = t if has_marker(close.text) else clean_rule(t), date, ""
            close.said_by = None if risky(close.text) else "developer"
            free.remove(close)
            held[i] = close
        elif text := clean_rule(t):
            said = None if risky(text) else "developer"  # a command or URL keeps no mark, however it got here
            held[i] = Rule(rule_id(note.id, text, taken), text, date, date, said_by=said, edited=date)
            taken.add(held[i].id)
    for r in free:
        mine = {w for w in _words(r.text).split() if len(w) > 2}
        host = next(  # most of its words went into an item that merged other rules: it was folded in too
            (h for h in held.values() if h.id in hosts and mine and _overlap(mine, hosts[h.id]) >= 0.5), None
        )
        if host:
            retire(r, MERGED_BY_HAND, host)
        else:
            retire(r, REMOVED_BY_HAND)
    order = [held[i] for i in range(len(items)) if i in held]
    return order + [r for r in rules if not any(r is o for o in order)], True


def _link_text(title: str) -> str:
    return re.sub(r"[\[\]|#]", "", title).strip()


def _by_context(prefs: list[Note]) -> list[tuple[str, list[Note]]]:
    """Group working preferences by where they apply. One judged to hold everywhere, or not yet
    judged (memory/reach.py), loads in every folder, as before contexts; one tied to a context
    loads only in the contexts its chats ran in; one whose chats ran outside every context's
    folders cannot be placed, so it loads nowhere by itself and stays searchable."""
    table = contexts.roots()
    if not table:
        return [("How you work", prefs)]
    groups: dict[str, list[Note]] = {"How you work": []}
    for n in prefs:
        if n.reach != "context":
            headings = ["How you work"]
        else:
            seen = contexts.of_chats(n.sources, table)
            headings = [f"How you work: {c}" for c in sorted(seen)] or ["How you work: unplaced"]
        for heading in headings:
            groups.setdefault(heading, []).append(n)
    return list(groups.items())


def audience(n: Note, table: dict[str, list[str]]) -> str | None:
    """Who may be handed a global preference mid-session (jit.py), by the rule session start
    applies when it compiles the index (compile and _by_context above). None: everyone, for a
    preference judged to hold everywhere or not judged yet. Otherwise a JSON list of who:
    `project:<key>` when its chats ran in a single project (it is that project's preference),
    else `ctx:<name>` for each context they ran in; empty when they ran outside every one."""
    if n.scope != "global" or n.reach != "context":
        return None
    seen = contexts.projects_of_chats(n.sources)
    if len(seen) == 1:
        return json.dumps([f"project:{next(iter(seen))}"])
    if not table:
        return None
    return json.dumps([f"ctx:{c}" for c in sorted(contexts.of_chats(n.sources, table))])


def pushed(note: Note) -> list[Rule] | None:
    """What of a note may reach an agent that did not ask for it, at session start (the
    indexes) and mid-session (jit.py): None when nothing may, an empty list when only its title
    may, else the rules whose words may. A rule that asks for a command, a URL, or a network,
    destructive or exfiltrating action, or for switching off a check (said.risky) never does,
    nor does a note whose title is such a rule. A rule only the agent stated is how a page the
    agent read becomes a note, so its words stay out and the title alone points to it. A rule
    no one verified (said_by unset, as for every rule written before 2026-10-15) is shown, and
    session start frames all of it as background. memory_search and read_note find everything."""
    if note.status != "active" or risky(note.title):
        return None
    live = [r for r in note.active_rules if not risky(r.text)]
    return [r for r in live if r.said_by != "agent"] if live else None


def _index_line(note: Note, href: str) -> str:
    """The note's title and the first sentence of each rule pushed() shows; the title alone
    when it shows none. Notes pushed() leaves out are not listed."""
    shown = pushed(note) or []
    if not shown:
        return f"- **{note.title}** ([note]({href}))"
    if len(shown) > 1:  # each rule's first sentence; past the cap below, the later rules are cut
        first = "; ".join(_first_sentence(r.text).rstrip(".") for r in shown) + "."
    else:
        first = _first_sentence(shown[0].text)
    hook = first if len(first) <= HOOK_CHARS else first[: HOOK_CHARS - 3] + "…"
    return f"- **{note.title}**: {hook} ([note]({href}))"


def _listed(items: list[Note], limit: int, href) -> tuple[list[str], int]:
    """Index lines for the first `limit` notes pushed() lets through, and how many of the
    items are left out of the index, for its "…and N more" line."""
    lines = [_index_line(n, href(n)) for n in items if pushed(n) is not None][:limit]
    return lines, len(items) - len(lines)


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]


def _ledger_entry(title: str | None, scope: str | None, project: str | None) -> dict:
    """What the deletion ledger keeps of a note. The body stays empty: earlier releases read
    one, and nothing writes a note's words there any more."""
    return {"title": title or "", "body": "", "scope": scope, "project": project}


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    _write_text(path, json.dumps(data, indent=1, sort_keys=True))


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

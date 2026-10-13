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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

import yaml

from .. import contexts, paths
from ..chats.redact import redact

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
REMOVED_BY_HAND = "removed by hand"
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
    said_by: str | None = None  # "developer" | "agent": who stated it in the chats
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
    path: str = ""

    @property
    def active_rules(self) -> list[Rule]:
        """The rules in force, in the order the body shows them, each with said_by and evidence."""
        return [r for r in self.rules if r.active]

    @property
    def said_by(self) -> str | None:
        """Who stated the rules in force: "developer" when the developer stated every one,
        "agent" when only the agent did, "mixed" otherwise; None when no rule says (notes
        written before rules recorded it)."""
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
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100))
            f.write("---\n" + note.body.strip() + (f"\n\n{note.footer}" if note.footer else "") + "\n")
        os.replace(tmp, path)
        note.path = path

    def reread(self, note: Note) -> Note | None:
        """The note as its file is now, for a step that changes one field after a long run: a
        hand edit made since the notes were loaded survives."""
        return read_note_file(note.path) if note.path else None

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
    # learn it again from the same chats.

    def rejected(self) -> list[dict]:
        return list(_read_json(self._ledger).get("rejected", {}).values())

    def reconcile(self, notes: dict[str, Note]) -> int:
        """Record notes deleted by hand as rejected; remember the rest as known.
        Returns how many deletions were found."""
        ledger = _read_json(self._ledger)
        known, rejected = ledger.get("known", {}), ledger.get("rejected", {})
        on_disk = set(notes) | set(self.load(include_archived=True))
        gone = [k for k in known if k not in on_disk]
        for k in gone:
            rejected[k] = known[k]
        known = {
            n.id: {"title": n.title, "body": n.body[:300], "scope": n.scope, "project": n.project}
            for n in notes.values()
        }
        _write_json(self._ledger, {"known": known, "rejected": rejected})
        return len(gone)

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
            out += [_index_line(n, f"global/{n.id}.md") for n in items[:GLOBAL_INDEX_LINES]] or ["- (nothing yet)"]
            if len(items) > GLOBAL_INDEX_LINES:
                out.append(f"- …and {len(items) - GLOBAL_INDEX_LINES} more in global/")
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
            limit = PROJECT_INDEX_LINES[t]
            out.append(f"\n## {SECTIONS[t]}\n")
            out += [
                _index_line(n, f"{n.id}.md" if n.scope == "project" else f"../../global/{n.id}.md")
                for n in ranked[:limit]
            ]
            if len(ranked) > limit:
                out.append(f"- …and {len(ranked) - limit} more in this folder")
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
    note = Note(**fields, body=body.strip(), related=related, footer=(mark + footer).strip(), path=path)
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


_NOT_FRONTMATTER = ("body", "path", "related", "footer", "rules", "edited_by_hand")  # rules: see _rule_meta


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
    known["sources"] = [str(s) for s in known.get("sources") or []]
    known.setdefault("id", rule_id("", known["text"], set()))
    known["id"] = str(known["id"])
    return Rule(**known)


def _edit_date(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except OSError:
        return today()


_BULLET = re.compile(r"^[-*+] +")


def _items(body: str) -> list[str]:
    """A body as rule texts: the items of a bullet list, else the whole body as one."""
    lines = body.strip().split("\n")
    if not _BULLET.match(lines[0]):
        return [body.strip()] if body.strip() else []
    out: list[str] = []
    for line in lines:
        if _BULLET.match(line):
            out.append(_BULLET.sub("", line, count=1))
        elif out:
            out[-1] += "\n" + (line[2:] if line.startswith("  ") else line)
    return [t.strip() for t in out if t.strip()]


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def _edits(note: Note, rules: list[Rule], date: str) -> tuple[list[Rule], bool]:
    """Read a hand edit of the body back into the rules. A line that matches a rule keeps it,
    a changed line updates the rule it is closest to, a new line is a new rule in the
    developer's words, and a rule whose line is gone is retired as removed by hand.
    Returns the rules and whether the body had been edited."""
    if _same(render(rules), note.body):
        return rules, False
    texts = _items(note.body)
    free = [r for r in rules if r.active]
    placed: dict[int, Rule] = {}
    for i, t in enumerate(texts):
        match = next((r for r in free if _same(r.text, t)), None)
        if match:
            placed[i] = match
            free.remove(match)
    for i, t in enumerate(texts):  # a retired rule's text written back brings it back
        back = None if i in placed else next((r for r in rules if not r.active and _same(r.text, t)), None)
        if back and back not in placed.values():
            back.status, back.retired, back.reason, back.replaced_by, back.edited = "active", "", "", None, date
            placed[i] = back
    taken = {r.id for r in rules}
    for i, t in enumerate(texts):
        if i in placed:
            continue
        close = max(free, key=lambda r: SequenceMatcher(None, r.text, t).ratio(), default=None)
        if close and SequenceMatcher(None, close.text, t).ratio() >= 0.5:
            close.text, close.edited, close.said_by = t, date, "developer"
            free.remove(close)
            placed[i] = close
        else:
            placed[i] = Rule(rule_id(note.id, t, taken), t, date, date, said_by="developer", edited=date)
            taken.add(placed[i].id)
    for r in free:
        r.status, r.retired, r.reason = "retired", date, REMOVED_BY_HAND
    order = [placed[i] for i in range(len(texts))]
    return order + [r for r in rules if r not in order], True


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


def _index_line(note: Note, href: str) -> str:
    live = [r for r in note.rules if r.active]
    if len(live) > 1:  # each rule's first sentence, so every rule can reach the index
        first = "; ".join(_first_sentence(r.text).rstrip(".") for r in live) + "."
    else:
        first = _first_sentence(note.body)
    hook = first if len(first) <= HOOK_CHARS else first[: HOOK_CHARS - 3] + "…"
    return f"- **{note.title}**: {hook} ([note]({href}))"


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]


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

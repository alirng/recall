"""Memory notes: one markdown file per memory, the source of truth.

    ~/.agents/memory/
      MEMORY.md                    global index: how you work, plus a list of projects
      global/<id>.md               cross-project preferences
      projects/<name>/MEMORY.md    per-project index, what agents load at session start
      projects/<name>/<id>.md      project facts, decisions, lessons, open threads
      unfiled/<id>.md              project facts from chats outside any project folder
      archive/...                  superseded and expired notes, same layout
      .recall/                     project registry and the deletion ledger

Notes are plain markdown with YAML frontmatter, so people can read and edit them,
or open the folder as an Obsidian vault. The SQLite index is rebuilt from these
files, so hand edits always win. The folder is a git repository and every memory
run is one commit: `git log` shows what changed and `git revert` undoes it.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import yaml

from .. import paths

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
README = (
    "# Agent memory\n\nMaintained by recall from your coding-agent chats. Start at MEMORY.md. "
    "Every note is plain markdown: edit or delete anything, and recall respects it on its next run. "
    "Each run is one git commit, so `git log` shows what changed and `git revert` undoes it.\n"
)


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
    status: str = "active"  # "active" | "superseded" | "expired"
    created: str = ""
    updated: str = ""
    last_seen: str = ""
    sources: list[str] = field(default_factory=list)  # chat ids
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    path: str = ""

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
        return notes

    def write(self, note: Note) -> None:
        directory = self._dir_for(note)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{note.id}.md")
        if note.path and os.path.abspath(note.path) != os.path.abspath(path) and os.path.exists(note.path):
            os.remove(note.path)  # the note moved: archived, or its project folder changed
        meta = {k: v for k, v in asdict(note).items() if k not in ("body", "path") and v not in (None, [], "")}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100))
            f.write("---\n" + note.body.strip() + "\n")
        os.replace(tmp, path)
        note.path = path

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
        for n in active:
            if n.scope == "project" and n.project:
                by_project.setdefault(n.project, []).append(n)
        header = (
            f"_Maintained by recall from your coding-agent chats; last updated {today()}. "
            "Each line links to a note. Edit or delete notes freely._\n"
        )
        project_lines = [
            self._compile_project(key, items, header, now)
            for key, items in sorted(by_project.items(), key=lambda kv: -len(kv[1]))
        ]
        prefs = sorted((n for n in active if n.scope == "global"), key=lambda n: -n.score(now))
        out = ["# Memory\n", header, "\n## How you work\n"]
        out += [_index_line(n, f"global/{n.id}.md") for n in prefs[:GLOBAL_INDEX_LINES]] or ["- (nothing yet)"]
        if len(prefs) > GLOBAL_INDEX_LINES:
            out.append(f"- …and {len(prefs) - GLOBAL_INDEX_LINES} more in global/")
        out += ["\n## Projects\n", *(project_lines or ["- (nothing yet)"])]
        _write_text(os.path.join(self.root, "MEMORY.md"), "\n".join(out) + "\n")

    def _compile_project(self, key: str, items: list[Note], header: str, now: float) -> str:
        name = items[0].project_name or key
        folder = self.project_folder(key, name)
        repo = key.split(":", 1)[1]
        out = [f"# {name}: project memory\n", f"Repo: `{repo}`  \n{header}"]
        for t in TYPES:
            ranked = sorted((n for n in items if n.type == t), key=lambda n: -n.score(now))
            if not ranked:
                continue
            limit = PROJECT_INDEX_LINES[t]
            out.append(f"\n## {SECTIONS[t]}\n")
            out += [_index_line(n, f"{n.id}.md") for n in ranked[:limit]]
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


def read_note_file(path: str) -> Note | None:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    fields = {k: v for k, v in meta.items() if k in Note.__dataclass_fields__ and k not in ("body", "path")}
    fields.setdefault("id", os.path.basename(path)[:-3])
    fields.setdefault("type", "fact")
    fields.setdefault("scope", "project")
    fields.setdefault("title", fields["id"])
    for k in ("created", "updated", "last_seen"):
        if k in fields:
            fields[k] = str(fields[k])  # YAML reads bare dates as date objects
    return Note(**fields, body=m.group(2).strip(), path=path)


def _index_line(note: Note, href: str) -> str:
    first = re.split(r"(?<=[.!?])\s", " ".join(note.body.split()), maxsplit=1)[0]
    hook = first if len(first) <= 160 else first[:157] + "…"
    return f"- **{note.title}**: {hook} ([note]({href}))"


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

"""Your projects: each one groups the repos and folders its chats ran in.

chats/projects.py gives every folder a key: its git remote, else its path. A project you work
on usually spans several keys (clones, worktrees, folders that moved, one repo per platform)
plus chats that ran in no folder at all. projects.yaml in the memory folder lists your
projects, so recall can follow each one across all of that and across every agent:

    harbor:
      name: Harbor
      kind: work              # work | personal
      status: active          # active | paused | done | dropped
      from: null              # the project this one grew out of
      paths:                  # git remotes (host/owner/repo), or folders with everything inside
        - github.com/acme/harbor
        - ~/code/harbor
        - ~/.cursor/worktrees/harbor
      words: [harbor]          # a chat with no project folder goes here when its title or
                              # first message names one of these

`recall projects suggest` drafts entries from your chat history. The file is yours: recall
only ever appends to it, and the memory folder's git history keeps every version.
"""

import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from . import paths
from .chats.projects import normalize_remote

STATUSES = ("active", "paused", "done", "dropped")
KINDS = ("work", "personal")
QUIET_DAYS = 30  # a suggested project with no chats for this long starts out paused
HEADER = """\
# Your projects. Each one groups the repos and folders its chats ran in, so recall can
# follow a project across clones, worktrees, moved folders and agents. Edit freely:
# rename, merge two entries, move a path, set kind and status. recall only appends.
#
#   kind:   work | personal
#   status: active | paused | done | dropped
#   from:   the project this one grew out of
#   paths:  git remotes (host/owner/repo), or folders with everything inside them
#   words:  a chat with no project folder goes here when its title or first message
#           names one of these
"""
# Folder names that say nothing about which project a chat is about.
_GENERIC = frozenset(
    "agents agent app apps api web site docs doc demo test tests server client scripts project projects "
    "personal src code tools data repo repos main dev work ui systems search".split()
)


@dataclass
class Project:
    id: str
    name: str
    kind: str | None = None
    status: str = "active"
    parent: str | None = None  # "from" in the file
    paths: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)


def file() -> str:
    return os.path.join(paths.MEMORY, "projects.yaml")


def load() -> dict[str, Project]:
    """The projects in projects.yaml; empty if there is no file yet or it does not parse."""
    try:
        with open(file(), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out = {}
    for pid, entry in data.items() if isinstance(data, dict) else ():
        entry = entry if isinstance(entry, dict) else {}
        out[str(pid)] = Project(
            id=str(pid),
            name=str(entry.get("name") or pid),
            kind=entry.get("kind") if entry.get("kind") in KINDS else None,
            status=entry.get("status") if entry.get("status") in STATUSES else "active",
            parent=str(entry["from"]) if entry.get("from") else None,
            paths=[str(p) for p in _as_list(entry.get("paths"))],
            words=[str(w).lower() for w in _as_list(entry.get("words"))],
        )
    return out


def find(name: str | None, projects: dict[str, Project] | None = None) -> Project | None:
    """A project by its id or name, ignoring case."""
    if not name:
        return None
    projects = load() if projects is None else projects
    low = name.lower()
    return next((p for p in projects.values() if low in (p.id.lower(), p.name.lower())), None)


# --- which project a chat or note belongs to -------------------------------------


def match(key: str | None, folder: str | None, projects: dict[str, Project]) -> str | None:
    """The project whose paths hold this folder key or folder; the most specific path wins."""
    best, best_len = None, 0
    for p in projects.values():
        for entry in p.paths:
            n = _match_len(entry, key, folder)
            if n > best_len:
                best, best_len = p.id, n
    return best


def for_folder(folder: str | None, projects: dict[str, Project] | None = None) -> Project | None:
    """The project a folder belongs to, e.g. the one an agent session started in."""
    if not folder:
        return None
    from .chats.projects import resolve

    projects = load() if projects is None else projects
    resolved = resolve(folder)
    pid = match(resolved.key if resolved else None, folder, projects)
    return projects.get(pid) if pid else None


def note_keys(project: Project, keys) -> list[str]:
    """Which of these folder keys (as notes record them) belong to the project."""
    return [k for k in keys if k and match(k, None, {project.id: project})]


def _match_len(entry: str, key: str | None, folder: str | None) -> int:
    entry = entry.strip().rstrip("/")
    if not entry:
        return 0
    if not entry.startswith(("/", "~", ".")) and "/" in entry and "." in entry.split("/", 1)[0]:
        # a git remote; any spelling of it works (ssh, https, .git)
        if key and key.startswith("git:") and key[4:].lower() == normalize_remote(entry).lower():
            return 10_000  # a repo beats any folder
        return 0
    path = os.path.normpath(os.path.expanduser(entry))
    candidates = [folder, key[5:] if key and key.startswith("path:") else None]
    for c in filter(None, candidates):
        if os.path.isabs(c):
            if c == path or c.startswith(path + "/"):
                return len(path)
        else:
            # Cursor names a folder that no longer exists by its path with punctuation
            # turned into dashes, so compare in that form.
            slug = _slug(path)
            if c == slug or c.startswith(slug + "-"):
                return len(path)
    return 0


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-")


def _words_re(project: Project) -> re.Pattern | None:
    words = [w for w in project.words if w.strip()]
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


def assign(rows, projects: dict[str, Project]) -> dict[str, tuple[str, str]]:
    """chat id -> (project id, "path" | "words") for each chat that belongs to a project.

    rows: sessions with id, project_key, project (the folder), title and first_prompt.
    """
    patterns = {pid: r for pid, p in projects.items() if (r := _words_re(p))}
    out = {}
    for r in rows:
        pid = match(r["project_key"], r["project"], projects)
        if pid:
            out[r["id"]] = (pid, "path")
            continue
        text = f"{r['title'] or ''}\n{r['first_prompt'] or ''}"
        hits = Counter({p: len(rx.findall(text)) for p, rx in patterns.items()})
        top = hits.most_common(2)
        if top and top[0][1] and (len(top) == 1 or top[0][1] > top[1][1]):
            out[r["id"]] = (top[0][0], "words")
    return out


def refresh(con) -> None:
    """Keep the chat_projects table in step with projects.yaml and the chat index. Cheap
    when nothing changed: one stat and one aggregate query."""
    con.execute("create table if not exists chat_projects (session_id text primary key, project text, how text)")
    try:
        st = os.stat(file())
        file_stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        file_stamp = "none"
    n, last = con.execute("select count(*), max(updated_at) from sessions").fetchone()
    stamp = f"{file_stamp}|{n}|{last}"
    row = con.execute("select v from meta where k='projects_stamp'").fetchone()
    if row and row[0] == stamp:
        return
    projects = load()
    rows = con.execute("select id, project_key, project, title, first_prompt from sessions").fetchall()
    assigned = assign(rows, projects)
    con.execute("delete from chat_projects")
    con.executemany("insert into chat_projects values (?,?,?)", [(sid, p, how) for sid, (p, how) in assigned.items()])
    con.execute("insert or replace into meta values ('projects_stamp', ?)", (stamp,))
    con.commit()


# --- the overview: every project over time -----------------------------------------


def overview(con, months: int = 6, include_all: bool = False) -> list[dict]:
    """Every project with its activity per month over the last `months` months, parents
    first with the projects that grew out of them right after. Projects with no chats in
    that window are left out unless include_all."""
    from .memory.notes import NoteStore

    refresh(con)
    projects = load()
    window = _months(months)
    stats: dict[str, dict] = {}
    for r in con.execute(
        "select cp.project, cp.how, s.agent, s.updated_at from chat_projects cp join sessions s on s.id = cp.session_id"
    ):
        st = stats.setdefault(r["project"], {"chats": 0, "agents": Counter(), "how": Counter(), "months": Counter()})
        st["chats"] += 1
        st["agents"][r["agent"]] += 1
        st["how"][r["how"]] += 1
        if r["updated_at"]:
            st["months"][datetime.fromtimestamp(r["updated_at"]).strftime("%Y-%m")] += 1
            st["first"] = min(st.get("first", r["updated_at"]), r["updated_at"])
            st["last"] = max(st.get("last", 0), r["updated_at"])
    note_counts: Counter = Counter()
    for n in NoteStore().load().values():
        if n.scope == "project" and n.project and (pid := match(n.project, None, projects)):
            note_counts[pid] += 1
    now = time.time()
    rows = []
    for p in projects.values():
        st = stats.get(p.id, {"chats": 0, "agents": Counter(), "how": Counter(), "months": Counter()})
        per_month = [st["months"].get(m, 0) for m in window]
        if not include_all and not any(per_month):
            continue
        last = st.get("last")
        rows.append(
            {
                "id": p.id,
                "name": p.name,
                "kind": p.kind,
                "status": p.status,
                "from": p.parent,
                "chats": st["chats"],
                "by_agent": dict(st["agents"].most_common()),
                "by_folder": st["how"].get("path", 0),
                "by_words": st["how"].get("words", 0),
                "notes": note_counts.get(p.id, 0),
                "first": _day(st.get("first")),
                "last": _day(last),
                "quiet_days": int((now - last) / 86400) if last else None,
                "months": dict(zip(window, per_month, strict=True)),
            }
        )
    return _family_order(rows)


def _family_order(rows: list[dict]) -> list[dict]:
    """Most recently active first, each project followed by the ones that grew out of it."""
    rank = {"active": 0, "paused": 1, "done": 2, "dropped": 3}
    rows.sort(key=lambda r: (rank.get(r["status"], 9), r["quiet_days"] is None, r["quiet_days"] or 0))
    ids = {r["id"] for r in rows}
    children: dict[str, list[dict]] = {}
    for r in rows:
        if r["from"] in ids and r["from"] != r["id"]:
            children.setdefault(r["from"], []).append(r)
    out, placed = [], set()

    def place(r: dict) -> None:
        if r["id"] in placed:
            return
        placed.add(r["id"])
        out.append(r)
        for child in children.get(r["id"], []):
            place(child)

    for r in rows:
        if r["from"] not in ids or r["from"] == r["id"]:
            place(r)
    for r in rows:  # a loop of from: entries still shows every project
        place(r)
    return out


def render_lanes(rows: list[dict], width: int = 6) -> str:
    """The overview as git-graph lanes: one row per project, one column per month.
    ● a busy month (10+ chats), ○ a quieter one, ┄ a stretch with no chats at all."""
    if not rows:
        return "No projects with chats in this window. Try --all, or `recall projects suggest`."
    window = list(rows[0]["months"])
    label = max(len(r["id"]) for r in rows) + 2
    kind = max((len(r["kind"] or "") for r in rows), default=0)
    kind = kind + 2 if kind else 0
    head = " " * (label + kind) + "".join(datetime.strptime(m, "%Y-%m").strftime("%b").ljust(width) for m in window)
    lines = [head.rstrip()]
    for r in rows:
        counts = [r["months"][m] for m in window]
        active = [i for i, c in enumerate(counts) if c]
        cells = []
        for i, c in enumerate(counts):
            if not active or i < active[0] or i > active[-1]:
                cells.append(" " * width)
            else:
                glyph = "●" if c >= 10 else "○" if c else "┄"
                link = " " if i == active[-1] else "─" if c and counts[i + 1] else "┄"
                cells.append(glyph + link * (width - 1))
        if r["from"] and active and active[0] > 0:
            cells[active[0] - 1] = "└" + "─" * (width - 1)  # branches off the lane above
        lane = "".join(cells)
        notes = [f"{r['chats']} chats"]
        if r["from"]:
            notes.append(f"from {r['from']}")
        if r["status"] != "active":
            notes.append(r["status"])
        elif r["quiet_days"] is not None and r["quiet_days"] >= 14:
            notes.append(f"quiet since {datetime.strptime(r['last'], '%Y-%m-%d').strftime('%-d %b')}")
        lines.append(r["id"].ljust(label) + (r["kind"] or "").ljust(kind) + lane + "  " + " · ".join(notes))
    return "\n".join(lines)


def detail(con, project: Project) -> dict:
    """One project: where its chats ran, which agents, its notes, and its latest chats."""
    refresh(con)
    rows = con.execute(
        """select s.*, cp.how from chat_projects cp join sessions s on s.id = cp.session_id
           where cp.project = ? order by s.updated_at desc""",
        (project.id,),
    ).fetchall()
    folders = Counter(r["project_key"] or "(no folder)" for r in rows)
    from .chats.store import session_row

    return {
        "id": project.id,
        "name": project.name,
        "kind": project.kind,
        "status": project.status,
        "from": project.parent,
        "paths": project.paths,
        "words": project.words,
        "chats": len(rows),
        "by_agent": dict(Counter(r["agent"] for r in rows).most_common()),
        "by_folder": dict(folders.most_common()),
        "placed_by_words": sum(1 for r in rows if r["how"] == "words"),
        "recent": [session_row(r) for r in rows[:8]],
    }


def roots(con, project: Project) -> list[str]:
    """The repos on disk that the project's chats ran in, plus folders its paths name;
    worktrees and subfolders count as their repo. Busiest first."""
    from .chats.projects import resolve

    refresh(con)
    folders = Counter(
        r[0]
        for r in con.execute(
            "select s.project from chat_projects cp join sessions s on s.id = cp.session_id where cp.project = ?",
            (project.id,),
        )
        if r[0] and os.path.isabs(r[0])
    )
    for entry in project.paths:
        path = os.path.expanduser(entry)
        if os.path.isabs(path):
            folders[path] += 0
    found: Counter = Counter()
    for folder, n in folders.items():
        repo = resolve(folder)  # a worktree or subfolder counts as its main checkout
        if repo and repo.root and os.path.isdir(repo.root) and os.path.exists(os.path.join(repo.root, ".git")):
            found[repo.root] += n
    return [root for root, _ in found.most_common()]


# --- suggesting projects from chat history ------------------------------------------


def suggest(con) -> list[Project]:
    """Draft projects for the folders in your chat history that no project covers yet.

    Folders are grouped by the repo they belong to: worktrees and '.git' and '.wt' folders
    join their repo, Cursor's names for deleted folders are read back into folder names,
    and names that extend another (tidepool, TidepoolV2, tidepool-ios) join it. A folder
    holding several projects (~/code) is not a project.
    """
    projects = load()
    units = _units(con, projects)
    if not units:
        return []
    containers = _containers(units)
    known = {u["base"] for k, u in units.items() if not u["slug"] and k not in containers}
    for u in units.values():
        if u["slug"]:
            u["base"] = _slug_base(u["key"][5:], u["base"], known)
    groups = _group({k: u for k, u in units.items() if k not in containers})
    taken = {p.id for p in projects.values()}
    out = []
    for members in sorted(groups, key=lambda m: -sum(u["chats"] for u in m)):
        name = _group_name(members)
        pid = _unique_id(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project", taken)
        taken.add(pid)
        last = max(u["last"] or 0 for u in members)
        folders = [f for u in members for f in u["folders"]]
        out.append(
            Project(
                id=pid,
                name=name,
                kind="personal" if any("/personal/" in f.lower() + "/" for f in folders) else None,
                status="active" if time.time() - last < QUIET_DAYS * 86400 else "paused",
                paths=_paths_for(members),
                words=[],
            )
        )
    _suggest_words(con, out, projects)
    return out


def _units(con, projects: dict[str, Project]) -> dict[str, dict]:
    """Each folder key no project covers yet, with its folders, chat count and dates."""
    units: dict[str, dict] = {}
    for r in con.execute(
        """select project_key key, project_name name, project folder, count(*) n,
                  max(updated_at) last from sessions where project_key is not null
           group by project_key, project"""
    ):
        if match(r["key"], r["folder"], projects):
            continue
        u = units.setdefault(
            r["key"],
            {
                "key": r["key"],
                "slug": r["key"].startswith("path:") and not os.path.isabs(r["key"][5:]),
                "base": _base(r["key"], r["name"]),
                "folders": [],
                "chats": 0,
                "last": 0,
            },
        )
        if r["folder"] and os.path.isabs(r["folder"]):
            u["folders"].append(os.path.normpath(r["folder"]))
        u["chats"] += r["n"]
        u["last"] = max(u["last"], r["last"] or 0)
    return units


def _base(key: str, name: str) -> str:
    """The repo a folder belongs to, by name."""
    if key.startswith("git:"):
        return name
    p = key[5:]
    for pattern in (r"/\.cursor/worktrees/([^/]+)", r"/\.codex/worktrees/[^/]+/([^/]+)", r"/([^/]+)\.wt(?:/|$)"):
        if m := re.search(pattern, p):
            return m.group(1)
    base = os.path.basename(p) if os.path.isabs(p) else name
    return re.sub(r"\.(git|code-workspace)$", "", base)


def _slug_base(slug: str, name: str, known: set[str]) -> str:
    """For a folder Cursor only names by its slug, the repo it belonged to."""
    if m := re.search(r"-cursor-worktrees-(.+)$", slug):
        rest = m.group(1)
        hits = [k for k in known if rest == _slug(k) or rest.startswith(_slug(k) + "-")]
        return max(hits, key=len) if hits else rest.split("-")[0]
    padded = f"-{slug}-"
    hits = [(padded.rfind(f"-{_slug(k)}-"), len(k), k) for k in known if f"-{_slug(k)}-" in padded]
    return max(hits)[2] if hits else name


def _containers(units: dict[str, dict]) -> set[str]:
    """Folders that hold two or more other projects' folders, such as ~/code."""
    folders = {f: k for k, u in units.items() for f in u["folders"]}
    out = set()
    for key, u in units.items():
        inside = {
            units[k]["base"].lower()
            for f, k in folders.items()
            if k != key and any(f.startswith(mine + "/") for mine in u["folders"])
        }
        workspace = {units[k]["base"].lower() for f, k in folders.items() if f.endswith(".code-workspace")}
        if len(inside - workspace) >= 2:
            out.add(key)
    return out


def _group(units: dict[str, dict]) -> list[list[dict]]:
    """Union units whose names match or extend one another, and a folder with the
    workspace file or worktrees inside it."""
    keys = list(units)
    parent = {k: k for k in keys}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        parent[root(a)] = root(b)

    norm = {k: re.sub(r"[^a-z0-9]", "", units[k]["base"].lower()) for k in keys}
    for a in keys:
        for b in keys:
            if a < b and norm[a] and norm[b]:
                short, long_ = sorted((norm[a], norm[b]), key=len)
                if short == long_ or (len(short) >= 5 and long_.startswith(short)):
                    union(a, b)
    # A workspace file stands for the folder it sits in.
    for a in keys:
        for f in units[a]["folders"]:
            if f.endswith(".code-workspace"):
                for b in keys:
                    if b != a and os.path.dirname(f) in units[b]["folders"]:
                        union(a, b)
    # A folder whose subfolders all belong to one other group joins it.
    for a in keys:
        groups_inside = {
            root(b)
            for b in keys
            if b != a and any(fb.startswith(fa + "/") for fa in units[a]["folders"] for fb in units[b]["folders"])
        }
        groups_inside.discard(root(a))
        if len(groups_inside) == 1:
            union(a, groups_inside.pop())
    groups: dict[str, list[dict]] = {}
    for k in keys:
        groups.setdefault(root(k), []).append(units[k])
    return list(groups.values())


def _group_name(members: list[dict]) -> str:
    """A workspace file's name if there is one (people name those after the product), else
    the folder the others sit in, else the name every other member extends, else the
    busiest member's."""
    for u in members:
        for f in u["folders"]:
            if f.endswith(".code-workspace"):
                return re.sub(r"\.code-workspace$", "", os.path.basename(f))
    placed = [u for u in members if u["folders"]]
    for u in placed:  # the folder the others sit in, such as a client folder holding a repo
        others = [o for o in placed if o is not u]
        if others and all(any(f.startswith(mine + "/") for mine in u["folders"] for f in o["folders"]) for o in others):
            return u["base"]
    norm = {u["base"]: re.sub(r"[^a-z0-9]", "", u["base"].lower()) for u in members}
    for u in sorted(members, key=lambda u: len(norm[u["base"]])):
        if all(n.startswith(norm[u["base"]]) for n in norm.values()):
            return u["base"]
    return max(members, key=lambda u: u["chats"])["base"]


def _paths_for(members: list[dict]) -> list[str]:
    out: list[str] = []
    home = os.path.expanduser("~")
    for u in sorted(members, key=lambda u: -u["chats"]):
        if u["key"].startswith("git:"):
            entries = [u["key"][4:]]
        elif u["slug"]:
            m = re.search(r"^(.*-cursor-worktrees-)", u["key"][5:])
            entries = [f"~/.cursor/worktrees/{u['base']}" if m else u["key"][5:]]
        else:
            folder = u["key"][5:]
            if m := re.search(r"^(.*/\.cursor/worktrees/[^/]+|.*?\.wt)(/|$)", folder):
                folder = m.group(1)
            entries = [folder]
        for e in entries:
            if e.startswith(home + "/"):
                e = "~" + e[len(home) :]
            if e not in out:
                out.append(e)
    # A folder inside another listed folder adds nothing.
    folders = [e for e in out if e.startswith(("/", "~"))]
    return [e for e in out if not any(e != f and e.startswith(f + "/") for f in folders)]


def _suggest_words(con, drafts: list[Project], existing: dict[str, Project]) -> None:
    """Give each draft up to three words that mark a chat as belonging to it, so chats with
    no project folder can be placed by what they talk about.

    A word qualifies when nine in ten of the chats that use it (among those with a known
    project) belong to this one, and it reads as a name rather than an ordinary word: people
    capitalise it mid-sentence ("ask Harbor"), or no dictionary knows it ("tidepoolr"). That
    keeps project names and drops "release" or "results", which every project uses.
    """
    every = existing | {p.id: p for p in drafts}
    rows = con.execute("select id, project_key, project, title, first_prompt from sessions").fetchall()
    owner = {r["id"]: match(r["project_key"], r["project"], every) for r in rows}
    chats_with: dict[str, Counter] = {}
    for r in rows:
        if owner[r["id"]]:
            for tok in set(re.findall(r"[a-z][a-z0-9]{3,}", f"{r['title'] or ''}\n{r['first_prompt'] or ''}".lower())):
                chats_with.setdefault(tok, Counter())[owner[r["id"]]] += 1
    size = Counter(o for o in owner.values() if o)
    prompts = [r["first_prompt"] or "" for r in rows]
    dictionary = _dictionary()
    for p in drafts:
        named = {p.id.replace("-", ""), *re.findall(r"[a-z][a-z0-9]{3,}", p.name.lower())}
        floor = max(8, size[p.id] * 0.04)
        picks = []
        for tok, hits in chats_with.items():
            n, total = hits[p.id], sum(hits.values())
            if tok in _GENERIC or n / total < 0.9 or n < (3 if tok in named else floor):
                continue
            if _ordinary(tok, dictionary) and _capitalised(tok, prompts) < 0.3:
                continue
            picks.append((tok not in named, -n, tok))
        p.words = [tok for _, _, tok in sorted(picks)[:3]]


def _ordinary(word: str, dictionary: set[str]) -> bool:
    stems = {word, word[:-1], word[:-2], word[:-3] + "y"} if word.endswith("s") else {word}
    return any(w in dictionary for w in stems)


def _capitalised(word: str, texts: list[str]) -> float:
    """How often the word is capitalised away from the start of a sentence."""
    cap = low = 0
    for t in texts:
        for m in re.finditer(rf"(?<![\w.]){re.escape(word)}\b", t, re.I):
            before = t[: m.start()].rstrip()
            if before and before[-1] not in ".!?:#-*\n":
                cap += m.group(0)[0].isupper()
                low += m.group(0)[0].islower()
    return cap / (cap + low) if cap + low else 0.0


def _dictionary() -> set[str]:
    """The system word list where there is one. Without it every word counts as ordinary,
    so only capitalised names are suggested."""
    try:
        with open("/usr/share/dict/words", encoding="utf-8", errors="replace") as f:
            return {w.strip().lower() for w in f}
    except OSError:
        return _AlwaysIn()


class _AlwaysIn(set):
    def __contains__(self, item) -> bool:
        return True


def render(drafts: list[Project]) -> str:
    """Draft projects as YAML to append to projects.yaml."""
    blocks = []
    for p in drafts:
        entry: dict = {"name": p.name}
        entry["kind"] = p.kind
        entry["status"] = p.status
        if p.parent:
            entry["from"] = p.parent
        entry["paths"] = p.paths
        if p.words:
            entry["words"] = p.words
        text = yaml.safe_dump({p.id: entry}, sort_keys=False, allow_unicode=True, width=100)
        blocks.append(text.replace("  kind: null\n", "  kind:                # work or personal\n"))
    return "\n".join(blocks)


def append(drafts: list[Project]) -> str:
    """Add draft projects to the end of projects.yaml, creating it if needed."""
    os.makedirs(paths.MEMORY, exist_ok=True)
    new = not os.path.exists(file())
    with open(file(), "a", encoding="utf-8") as f:
        if new:
            f.write(HEADER + "\n")
        elif drafts:
            f.write("\n")
        f.write(render(drafts))
    return file()


# --- helpers ------------------------------------------------------------------------


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _months(n: int) -> list[str]:
    now = datetime.now()
    out = []
    y, m = now.year, now.month
    for _ in range(max(1, n)):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _day(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else None


def _unique_id(pid: str, taken: set[str]) -> str:
    if pid not in taken:
        return pid
    i = 2
    while f"{pid}-{i}" in taken:
        i += 1
    return f"{pid}-{i}"

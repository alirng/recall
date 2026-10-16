"""Which chats produced which commits, worked out on this machine alone.

For each chat recall reads the files its agent edited and the lines it wrote into them
(chats/work.py, from the same tool calls status.py reads). It then matches them with commits
in that repo: made after the edits, touching the same files, and adding the same lines. When
two chats wrote the same line, the one that wrote it last before the commit gets it. Lines
many commits add (boilerplate) are no evidence, and a line that an earlier commit on the same
history already added was moved by this commit, not written for it. A chat that ran `git
commit` (or revert, or cherry-pick) itself is matched by the sha git printed, or by the
commit's subject when the agent keeps no command output (Cursor).

Private by design. Links live only in recall's index. Git is only read (rev-parse, rev-list,
log, show, blame), with optional locks, lazy fetching and textconv off, so nothing is
written to any repo: no commit messages, trailers, notes, refs, branches, hooks or objects,
and nothing is pushed or fetched. `[links] enabled = false` in config.toml stops linking and
drops every table here.

What was read from transcripts (the KEPT tables) cannot be rebuilt once an agent deletes a
transcript (Claude Code does after 30 days; the archive keeps cleaned text, not tool calls),
so it is migrated, never dropped, and a re-read merges with what was read before. The rest
(DERIVED) is worked out from it and git, and is rebuilt freely.

Projects are keyed on their git remote (chats/projects.py), so worktrees and clones of one
repo share their links. Squash merges and rebases need nothing special: the new commit adds
the same lines, so it links to the same chats.
"""

import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from . import config, paths
from .chats.projects import resolve
from .chats.sources import compressed
from .chats.work import READERS, Work

KEPT = ("link_chats", "link_edits", "link_events", "link_checkouts")
KEPT_VERSION = 1  # a change to a KEPT table migrates it in _migrate; never drop one
DERIVED = ("link_repos", "link_todo", "link_commits", "link_commit_files", "commit_links")
DERIVED_VERSION = 3  # bump to rebuild the DERIVED tables
READ_VERSION = 3  # bump when chats/work.py reads differently: chats still on disk are read again
SCHEMA = """
create table if not exists link_chats (session_id text primary key, fingerprint text, size int, read_at real);
create table if not exists link_edits (
  session_id text, repo text, path text, first_at real, last_at real, lines blob, weak blob, n int,
  first_ask text, ask text, primary key (session_id, repo, path)
);
create index if not exists link_edits_repo on link_edits(repo, path);
create table if not exists link_events (session_id text, n int, at real, kind text, text text, detail text);
create index if not exists link_events_session on link_events(session_id, n);
create table if not exists link_checkouts (repo text, top text, primary key (repo, top));
create table if not exists link_repos (repo text primary key, linked_at real);
create table if not exists link_todo (repo text, sha text, primary key (repo, sha));
create table if not exists link_commits (
  repo text, sha text, parent text, at real, authored real, author text, subject text, gitdir text,
  diffed int default 0, n int, primary key (repo, sha)
);
create index if not exists link_commits_at on link_commits(repo, at);
create table if not exists link_commit_files (
  repo text, sha text, path text, lines blob, n int, primary key (repo, sha, path)
);
create index if not exists link_commit_files_path on link_commit_files(repo, path);
create table if not exists commit_links (
  repo text, sha text, session_id text, score real, how text, lines int, commit_lines int, chat_lines int,
  files text, ask text, primary key (repo, sha, session_id)
);
create index if not exists commit_links_session on commit_links(session_id);
"""

SLACK = 10 * 60  # a commit this long before an edit's recorded time can still hold it (clocks, coarse times)
WINDOW = 30 * 86400  # a commit this long after a chat's last edit to a file can still hold it
MIN_LINES = 3  # distinctive lines a chat must have written into a commit to link by content
MIN_SHARE = 0.2  # ...and the share of the commit they make up,
MIN_KEPT = 0.3  # ...or the share of what the chat wrote to those files that the commit holds
RECENT = 6 * 3600  # ...or the commit came this soon after the chat's last edit
COMMON = 3  # a line more commits than this add is boilerplate, not evidence
MAX_FILE_LINES = 5000  # a file adding more lines than this in one commit is generated or vendored
# Files whose lines say nothing about who wrote the code: lockfiles and build output.
_GENERATED = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|"
    r"go\.sum|composer\.lock|Podfile\.lock|Package\.resolved)$|\.(min\.js|min\.css|map|snap|lock)$"
)
_EMPTY = "0" * 40
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_ENOUGH = re.compile(r"[^\W_](?:.*?[^\W_]){3}")  # four letters or digits


# --- lines -------------------------------------------------------------------------


def _norm(line: str) -> bytes | None:
    """A line as evidence: whitespace collapsed, hashed. Short and punctuation-only lines say
    nothing about who wrote them, so they are dropped."""
    s = " ".join(line.split())
    if len(s) < 8 or not _ENOUGH.search(s):
        return None
    return hashlib.blake2b(s.encode(), digest_size=8).digest()


def _hashes(lines) -> set[bytes]:
    return {h for x in lines if (h := _norm(x))}


def _written(e) -> set[bytes]:
    """The lines an edit wrote: what it adds, less the unchanged lines around the change."""
    return _hashes(e.added) - _hashes(e.removed)


def _pack(hs: set[bytes]) -> bytes:
    return b"".join(sorted(hs))


def _unpack(blob: bytes | None) -> set[bytes]:
    return {blob[i : i + 8] for i in range(0, len(blob or b""), 8)}


# --- git, read-only -------------------------------------------------------------------

_GIT = ("-c", "core.quotePath=false", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false")
# No inherited GIT_DIR and the like; no optional locks (index refresh), no fetching of objects
# a partial clone lacks (that writes packs), no prompts.
_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


def git(where: str, *args: str, stdin: str | None = None, timeout: int = 120, gitdir: bool = False) -> str | None:
    """Run a read-only git command in a checkout (or against a git dir); None if it fails or
    times out. Callers pass --no-textconv to anything that diffs: a textconv cache writes notes."""
    loc = ("--git-dir", where) if gitdir else ("-C", where)
    try:
        r = subprocess.run(
            ["git", *_GIT, *loc, *args],
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _top(path: str, cache: dict) -> str | None:
    from .status import _top as top  # the walk up to a checkout that status.py already does

    return top(path, cache)


def _gitdir(top: str) -> str | None:
    from .status import _repo_dir

    return _repo_dir(top)


def place(path: str, folder: str | None, cache: dict) -> tuple[str, str, str] | None:
    """(repo key, path in the repo, checkout) for a file an agent edited; None outside a repo
    or in a scratch folder. A file in a worktree that has since been deleted still finds its
    repo: the walk up lands in the main checkout, and the worktree's folders are dropped."""
    full = os.path.normpath(path if os.path.isabs(path) or not folder else os.path.join(folder, path))
    if os.path.exists(os.path.dirname(full)):
        full = os.path.realpath(full)
    top = _top(full, cache)
    if not top:
        return None
    rel = os.path.relpath(full, top)
    if rel.startswith(".."):
        return None
    if not os.path.exists(os.path.dirname(full)) and (m := re.match(r"(?:.*/)?worktrees/[^/]+/(.+)", rel)):
        rel = m.group(1)
    key = cache.get(("key", top))
    if key is None:
        project = resolve(top)
        key = cache[("key", top)] = project.key if project else ""
    return (key, rel, top) if key else None


# --- the tables ---------------------------------------------------------------------


def enabled() -> bool:
    return bool(config.load().get("links", {}).get("enabled", True))


def present(con) -> bool:
    """Whether the link tables exist; reading them must not create them."""
    return bool(con.execute("select 1 from sqlite_master where name='link_chats'").fetchone())


def ensure(con) -> None:
    con.execute("create table if not exists meta (k text primary key, v text)")
    meta = dict(con.execute("select k, v from meta where k like 'links_%'").fetchall())
    if "links_schema" in meta:  # a development build before the KEPT/DERIVED split
        clear(con)
        meta = {}
    if meta.get("links_kept") and int(meta["links_kept"]) < KEPT_VERSION:
        _migrate(con, int(meta["links_kept"]))
    if meta.get("links_derived") != str(DERIVED_VERSION):
        for t in DERIVED:
            con.execute(f"drop table if exists {t}")
    con.executescript(SCHEMA)
    con.executemany(
        "insert or replace into meta values (?, ?)",
        [("links_kept", str(KEPT_VERSION)), ("links_derived", str(DERIVED_VERSION))],
    )
    con.commit()


def _migrate(con, version: int) -> None:
    """Bring the KEPT tables from an older version to KEPT_VERSION, keeping every row."""


def clear(con) -> bool:
    """Forget every link and everything read for them, for when linking is turned off. The
    database is vacuumed and the write-ahead log truncated, so no deleted row lingers in free
    pages either (rows rewritten in normal use leave some)."""
    if not con.execute(
        f"select 1 from sqlite_master where name in ({','.join('?' * len(KEPT + DERIVED))})", (*KEPT, *DERIVED)
    ).fetchone():
        return False
    for t in (*KEPT, *DERIVED):
        con.execute(f"drop table if exists {t}")
    con.execute("delete from meta where k like 'links_%'")
    con.commit()
    with contextlib.suppress(Exception):  # another process holding the database: it runs next time too
        con.execute("vacuum")
        con.execute("pragma wal_checkpoint(TRUNCATE)")
    return True


@contextlib.contextmanager
def _locked():
    """One link update at a time across recall's processes; a second one skips."""
    os.makedirs(paths.HOME, exist_ok=True)
    with open(os.path.join(paths.HOME, "links.lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def prune(con) -> None:
    """Drop what is kept about repos no chat touches any more, such as after `recall forget`:
    their commit subjects, authors and paths."""
    keep = "(select distinct repo from link_edits)"
    for t in ("link_checkouts", *DERIVED):
        con.execute(f"delete from {t} where repo not in {keep}")
    con.commit()


# --- reading chats -------------------------------------------------------------------


def _fingerprint(source: str) -> tuple[str, int] | None:
    got = compressed.fingerprint(source)  # "<mtime_ns>:<size>", whichever form Codex keeps the file in
    if got is None:
        return None
    fp = f"{READ_VERSION}:{got}"
    if source.endswith(".jsonl"):  # Claude Code keeps a chat's subagents beside it
        subs = glob.glob(os.path.join(source[:-6], "subagents", "*.jsonl"))
        if subs:
            fp += f":{len(subs)}:{max(os.stat(s).st_mtime_ns for s in subs)}"
    return fp, int(got.rsplit(":", 1)[1])


def read_chats(con, log=None) -> int:
    """Read the edits of every chat that changed since the last run; returns how many."""
    seen = {r[0]: (r[1], r[2] or 0) for r in con.execute("select session_id, fingerprint, size from link_chats")}
    rows = con.execute("select id, agent, source, project, started_at, updated_at from sessions").fetchall()
    cache: dict = {}
    done = 0
    for r in rows:
        reader = READERS.get(r["agent"])
        if not reader or not r["source"] or "#" in r["source"]:
            continue  # Cursor's older chats live in its database and keep no tool calls
        got = _fingerprint(r["source"])
        if got is None or seen.get(r["id"], (None,))[0] == got[0]:
            continue
        try:
            work = reader(r["source"])
        except Exception:  # one unreadable chat must not stop the run
            continue
        _store_chat(con, r, work, cache, shrank=got[1] < seen.get(r["id"], (None, 0))[1])
        con.execute("insert or replace into link_chats values (?,?,?,?)", (r["id"], got[0], got[1], time.time()))
        con.commit()  # per chat, so no write lock is held while the next transcript is read
        done += 1
        if log and done % 200 == 0:
            log(f"links: read {done} chats…")
    return done


def _store_chat(con, r, work: Work, cache: dict, shrank: bool) -> None:
    """Store what a chat wrote, per file. A transcript only grows, so a file missing from a
    new read no longer resolves to its repo (its worktree is gone): what was read before is
    kept. A transcript that shrank was cut short, so the two reads are merged."""
    sid = r["id"]
    folder = work.folder or r["project"]
    files: dict[tuple[str, str], dict] = {}
    tops = set()
    for e in work.edits:
        where = place(e.path, folder, cache)
        if not where:
            continue
        repo, rel, top = where
        tops.add((repo, top))
        at = e.at or r["started_at"] or r["updated_at"]
        f = files.setdefault((repo, rel), {"lines": set(), "weak": set(), "first": at, "last": at, "ask": e.ask})
        f["first_ask"] = f.get("first_ask") or e.ask
        added, removed = _hashes(e.added), _hashes(e.removed)
        if e.whole:  # the file as a whole: what stays of the chat's own lines, the rest is weak
            f["lines"] &= added
            f["weak"] = added - f["lines"]
        else:
            f["lines"] = (f["lines"] - (removed - added)) | (added - removed)
            f["weak"] -= removed - added
        if at:
            f["first"] = min(x for x in (f["first"], at) if x)
            f["last"] = max(x for x in (f["last"], at) if x)
        f["ask"] = e.ask or f["ask"]
    old = {(o["repo"], o["path"]): dict(o) for o in con.execute("select * from link_edits where session_id=?", (sid,))}
    rows = {}
    for key, f in files.items():
        rows[key] = {**f, "lines": _pack(f["lines"]), "weak": _pack(f["weak"]), "n": len(f["lines"] | f["weak"])}
    for key, o in old.items():
        if key not in rows:
            rows[key] = {**o, "first": o["first_at"], "last": o["last_at"]}
        elif shrank:
            new = rows[key]
            lines = _unpack(o["lines"]) | _unpack(new["lines"])
            weak = (_unpack(o["weak"]) | _unpack(new["weak"])) - lines
            new.update(lines=_pack(lines), weak=_pack(weak), n=len(lines | weak), first_ask=o["first_ask"])
            new.update(first=min(o["first_at"], new["first"]), last=max(o["last_at"], new["last"]))
    con.execute("delete from link_edits where session_id=?", (sid,))
    con.executemany(
        "insert into link_edits values (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                sid,
                repo,
                rel,
                f["first"],
                f["last"] or r["updated_at"],
                f["lines"],
                f["weak"],
                f["n"],
                f["first_ask"],
                f["ask"],
            )
            for (repo, rel), f in rows.items()
            if f["n"]
        ],
    )
    events = [(at or r["updated_at"], kind, text, detail) for at, kind, text, detail in work.events]
    if shrank:
        kept = [
            tuple(e) for e in con.execute("select at, kind, text, detail from link_events where session_id=?", (sid,))
        ]
        before = set(kept)
        events = kept + [e for e in events if e not in before]
    con.execute("delete from link_events where session_id=?", (sid,))
    con.executemany("insert into link_events values (?,?,?,?,?,?)", [(sid, i, *e) for i, e in enumerate(events)])
    # A chat that ran `git commit` in its folder may have committed edits made by hand.
    home = place(folder, None, cache) if folder and any(e[1] == "commit" for e in events) else None
    if home:
        tops.add((home[0], home[2]))
    con.executemany("insert or ignore into link_checkouts values (?,?)", sorted(tops))
    # The commits this chat could be behind, before and after this read, are worked out again.
    spans = {key: (o["first_at"] or 0, o["last_at"] or 0) for key, o in old.items()}
    for key, f in rows.items():
        a, b = spans.get(key, (f["first"] or 0, f["last"] or 0))
        spans[key] = (min(a, f["first"] or a), max(b, f["last"] or b))
    for (repo, rel), (a, b) in spans.items():
        _todo(con, repo, "f.path = ?", (rel,), a - SLACK, b + WINDOW)
    for at, kind, _, _ in events:
        if kind == "commit" and at:
            for repo in {repo for repo, _ in rows} | ({home[0]} if home else set()):
                _todo(con, repo, "1", (), at - SLACK, at + 12 * 3600)


def _todo(con, repo: str, where: str, args: tuple, start: float, end: float) -> None:
    con.execute(
        "insert or ignore into link_todo select distinct c.repo, c.sha from link_commits c "
        f"join link_commit_files f on f.repo = c.repo and f.sha = c.sha where c.repo = ? and {where} "
        "and c.at between ? and ?",
        (repo, *args, start, end),
    )


# --- commits -------------------------------------------------------------------------

_FORMAT = "%x1e%H%x1f%P%x1f%ct%x1f%at%x1f%an%x1f%s"


def _since(con, repo: str) -> int | None:
    row = con.execute("select min(first_at) from link_edits where repo=?", (repo,)).fetchone()
    return int(row[0] - 2 * 86400) if row and row[0] else None


def scan(con, repo: str) -> int:
    """Record every commit in the repo's clones since its chats began; returns how many are new."""
    since = _since(con, repo)
    if since is None:
        return 0
    known = {r[0] for r in con.execute("select sha from link_commits where repo=?", (repo,))}
    new = 0
    for gitdir in _gitdirs(con, repo):
        out = git(gitdir, "rev-list", "--exclude=refs/stash", "--all", "--no-merges", f"--since={since}", gitdir=True)
        fresh = [s for s in (out or "").split() if s not in known]
        if not fresh:
            continue
        out = git(
            gitdir, "log", "--no-walk=unsorted", "--stdin", "--name-only", "--no-renames", f"--format={_FORMAT}",
            stdin="\n".join(fresh), gitdir=True, timeout=300,
        )  # fmt: skip
        rows, files = [], []
        for block in (out or "").split("\x1e")[1:]:
            head, _, names = block.partition("\n")
            parts = head.split("\x1f")
            if len(parts) < 6:
                continue
            sha, parents, ct, at, author, subject = parts[:6]
            rows.append((repo, sha, (parents.split() or [None])[0], float(ct), float(at), author, subject, gitdir))
            files += [(repo, sha, n) for n in names.splitlines() if n.strip()]
            known.add(sha)
        con.executemany(
            "insert or ignore into link_commits(repo, sha, parent, at, authored, author, subject, gitdir) "
            "values (?,?,?,?,?,?,?,?)",
            rows,
        )
        con.executemany("insert or ignore into link_commit_files(repo, sha, path) values (?,?,?)", files)
        con.executemany("insert or ignore into link_todo values (?,?)", [(repo, x[1]) for x in rows])
        con.commit()  # git ran with no write lock held, and the next clone's will too
        new += len(rows)
    return new


def _gitdirs(con, repo: str) -> list[str]:
    """Every clone of the repo on disk that a chat worked in; worktrees share their clone's.
    A clone that was moved or deleted drops out."""
    out = []
    for (top,) in con.execute("select top from link_checkouts where repo=?", (repo,)).fetchall():
        if os.path.isdir(top) and (g := _gitdir(top)) and os.path.isdir(g) and g not in out:
            out.append(g)
    return out


def live_gitdir(con, repo: str, gitdir: str | None) -> str | None:
    """The clone a commit was recorded from, or another live one if it has gone."""
    if gitdir and os.path.isdir(gitdir):
        return gitdir
    live = _gitdirs(con, repo)
    if live and gitdir:
        con.execute("update link_commits set gitdir=? where repo=? and gitdir=?", (live[0], repo, gitdir))
        con.commit()
    return live[0] if live else None


def _diff(con, repo: str, shas: list[str]) -> None:
    """Read the lines each commit adds, for commits not read yet. A commit git cannot show
    without fetching (a partial clone) or within the time limit is marked failed, not retried."""
    by_dir: dict[str, list[str]] = {}
    for sha in shas:
        row = con.execute("select gitdir, diffed from link_commits where repo=? and sha=?", (repo, sha)).fetchone()
        if row and not row["diffed"] and (g := live_gitdir(con, repo, row["gitdir"])):
            by_dir.setdefault(g, []).append(sha)
    for gitdir, todo in by_dir.items():
        for i in range(0, len(todo), 100):
            batch = todo[i : i + 100]
            got, tried = _patches(gitdir, batch), set(batch)
            if got is None:  # one commit can fail a batch: read them one at a time, for a while
                got, tried, until = {}, set(), time.time() + 120
                for sha in batch:
                    if time.time() < until:
                        got.update(_patches(gitdir, [sha], timeout=60) or {})
                        tried.add(sha)
            rows, done = [], []
            for sha in [s for s in batch if s in tried]:  # the rest are tried on the next run
                total = 0
                for path, lines in got.get(sha, {}).items():
                    hs = _hashes(lines) if len(lines) <= MAX_FILE_LINES and not _GENERATED.search(path) else set()
                    total += len(hs)
                    rows.append((repo, sha, path, _pack(hs), len(hs)))
                done.append((1 if sha in got else -1, total, repo, sha))
            con.executemany("insert or replace into link_commit_files values (?,?,?,?,?)", rows)
            con.executemany("update link_commits set diffed=?, n=? where repo=? and sha=?", done)
            con.commit()


def _patches(gitdir: str, shas: list[str], timeout: int = 300) -> dict[str, dict[str, list[str]]] | None:
    out = git(
        gitdir, "log", "--no-walk=unsorted", "--stdin", "-p", "-U0", "--no-color", "--no-ext-diff",
        "--no-textconv", "--format=%x1e%H", stdin="\n".join(shas), gitdir=True, timeout=timeout,
    )  # fmt: skip
    if out is None:
        return None
    return dict(_parse_patch(block) for block in out.split("\x1e")[1:])


def _parse_patch(block: str) -> tuple[str, dict[str, list[str]]]:
    sha, _, body = block.partition("\n")
    files: dict[str, list[str]] = {}
    cur, header = None, False
    for line in body.split("\n"):
        if line.startswith("diff --git "):
            cur, header = None, True
        elif header and line.startswith("+++ "):
            name = line[4:]
            cur = None if name == "/dev/null" else name[2:] if name.startswith("b/") else name
            header = False
            if cur is not None:
                files.setdefault(cur, [])
        elif line.startswith("@@"):
            header = False
        elif cur is not None and not header and line.startswith("+"):
            files[cur].append(line[1:])
    return sha.strip(), files


def _history(con, repo: str) -> dict[str, tuple[float, list[str]]]:
    """sha -> (commit time, parents) for every commit in the repo's clones since its chats
    began, merges included: one rev-list per clone."""
    out: dict[str, tuple[float, list[str]]] = {}
    since = _since(con, repo)
    for gitdir in _gitdirs(con, repo) if since else []:
        text = git(
            gitdir, "rev-list", "--exclude=refs/stash", "--all", "--timestamp", "--parents", f"--since={since}",
            gitdir=True, timeout=300,
        )  # fmt: skip
        for line in (text or "").split("\n"):
            parts = line.split()
            if len(parts) >= 2:
                out[parts[1]] = (float(parts[0]), parts[2:])
    return out


def _ancestor(history: dict, a: str, b: str, known: dict) -> bool:
    """Whether commit a is an ancestor of commit b, walking b's parents no further back than a."""
    if (a, b) not in known:
        floor = history.get(a, (0.0,))[0] - 86400  # committer clocks drift a little
        stack, seen, found = [b], set(), False
        while stack and not found:
            x = stack.pop()
            if x in seen or x not in history:
                continue
            seen.add(x)
            ts, parents = history[x]
            found = a in parents
            if ts >= floor:
                stack += parents
        known[(a, b)] = found
    return known[(a, b)]


# --- linking ---------------------------------------------------------------------------


@dataclass
class _Claim:
    session: str
    first: float
    last: float
    strong: set[bytes]
    weak: set[bytes]
    ask: str


def link(con, repo: str, only: set[str] | None = None) -> int:
    """Work out the repo's links again from its chats' edits and its commits: every commit, or
    only these."""
    found, ran, commits = evidence(con, repo, only)
    scope = set(commits) if only is None else only & set(commits)
    rows = []
    for (sha, sid), x in found.items():
        made = (sha, sid) in ran
        if not made and not confident(x):
            continue
        score = 1.0 if made else round(min(1.0, max(x["share"], x["kept"])) * min(1.0, x["lines"] / 10), 3)
        how = "ran" if made else "wrote"
        rows.append((repo, sha, sid, score, how, x["lines"], x["total"], x["chat"], json.dumps(x["files"]), x["ask"]))
    for sha, sid in ran - set(found):
        if sha in scope:
            rows.append((repo, sha, sid, 1.0, "ran", 0, commits[sha]["n"] or 0, 0, "[]", ""))
    if only is None:
        con.execute("delete from commit_links where repo=?", (repo,))
        con.execute("delete from link_todo where repo=?", (repo,))
    else:
        con.executemany("delete from commit_links where repo=? and sha=?", [(repo, s) for s in only])
        con.executemany("delete from link_todo where repo=? and sha=?", [(repo, s) for s in only])
    con.executemany("insert or replace into commit_links values (?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("insert or replace into link_repos values (?, ?)", (repo, time.time()))
    con.commit()
    return len(rows)


def confident(x: dict) -> bool:
    """Whether a chat's lines in a commit are enough to say it wrote part of it: a few lines
    no other commit adds, making up enough of the commit or of what the chat wrote to those
    files, or committed within hours of the chat's last edit."""
    big = x["share"] >= MIN_SHARE or x["kept"] >= MIN_KEPT
    return x["distinct"] >= MIN_LINES and (big or 0 <= x["gap"] <= RECENT)


def evidence(con, repo: str, only: set[str] | None = None) -> tuple[dict, set, dict]:
    """For each (commit, chat) that could be linked, among `only` if given: the lines the chat
    wrote into the commit and their share of each. Also the (commit, chat) pairs where the
    chat ran the commit, and the repo's commits."""
    claims: dict[str, list[_Claim]] = {}
    for r in con.execute("select * from link_edits where repo=?", (repo,)):
        c = _Claim(r["session_id"], r["first_at"], r["last_at"], _unpack(r["lines"]), _unpack(r["weak"]), r["ask"])
        claims.setdefault(r["path"], []).append(c)
    commits = {r["sha"]: r for r in con.execute("select * from link_commits where repo=?", (repo,))}
    touching: dict[str, list[str]] = {}
    for r in con.execute("select sha, path from link_commit_files where repo=?", (repo,)):
        if r["path"] in claims and r["sha"] in commits:
            touching.setdefault(r["sha"], []).append(r["path"])

    def admits(c: _Claim, at: float) -> bool:
        return c.first - SLACK <= at <= c.last + WINDOW

    # A revert brings back lines someone wrote before; it is the reverter's, so only a chat
    # that ran it links to it.
    candidates = sorted(
        (
            s
            for s, ps in touching.items()
            if not (commits[s]["subject"] or "").startswith('Revert "')
            and any(admits(c, commits[s]["at"]) for p in ps for c in claims[p])
        ),
        key=lambda s: commits[s]["at"],
    )
    focus = candidates if only is None else [s for s in candidates if s in only]
    _diff(con, repo, focus)
    added: dict[str, dict[str, tuple[set, int]]] = {}
    for r in con.execute(
        "select sha, path, lines, n from link_commit_files where repo=? and lines is not null", (repo,)
    ):
        if r["sha"] in commits:
            added.setdefault(r["sha"], {})[r["path"]] = (_unpack(r["lines"]), r["n"] or 0)
    every = {sha: set().union(*(x for x, _ in files.values())) for sha, files in added.items()}
    seen_in: Counter = Counter(h for lines in every.values() for h in lines)  # boilerplate is in many
    moved = _moved(con, repo, focus, every, seen_in, commits)
    found: dict[tuple[str, str], dict] = {}
    for sha in focus:
        at = commits[sha]["at"]
        total = sum(n for _, n in added.get(sha, {}).values()) - len(moved[sha])
        for path in touching[sha]:
            lines = added.get(sha, {}).get(path, (set(), 0))[0] - moved[sha]
            if not lines:
                continue
            # The chat that wrote a line last before the commit gets it; a chat that went on
            # editing after the commit comes after those that stopped before it. Lines a chat
            # only wrote as part of a whole file count after every chat's own edits.
            live = sorted((c for c in claims[path] if admits(c, at)), key=lambda c: (c.last > at + SLACK, -c.last))
            taken: set[bytes] = set()
            for tier in ("strong", "weak"):
                for c in live:
                    got = (getattr(c, tier) & lines) - taken
                    taken |= got
                    x = found.setdefault((sha, c.session), {"got": set(), "chat": 0, "files": [], "ask": "", "last": 0})
                    x["total"] = total
                    x["got"] |= got
                    if tier == "strong":
                        x["chat"] += len(c.strong | c.weak)
                        if c.last >= x["last"]:
                            x["last"], x["ask"] = c.last, c.ask
                    if got and path not in x["files"]:
                        x["files"].append(path)
    for (sha, _), x in found.items():
        got = x.pop("got")
        x["lines"], x["distinct"] = len(got), sum(seen_in[h] <= COMMON for h in got)
        x["share"] = x["lines"] / x["total"] if x["total"] > 0 else 0.0
        x["kept"] = x["lines"] / x["chat"] if x["chat"] else 0.0
        x["gap"] = commits[sha]["at"] - x["last"]  # seconds from the chat's last edit to the commit
    return found, _ran(con, repo, commits), commits


def _moved(con, repo: str, focus: list[str], every: dict, seen_in: Counter, commits: dict) -> dict[str, set]:
    """Per commit, the lines an earlier commit on the same history already added: moved or
    re-indented here, not written. A squash merge or a rebase copies lines onto another
    history, so it keeps them."""
    out: dict[str, set] = {sha: set() for sha in focus}
    adders: dict[bytes, list[str]] = {}
    for sha, lines in every.items():
        for h in lines:
            if seen_in[h] > 1:
                adders.setdefault(h, []).append(sha)
    if not adders:
        return out
    history, known = _history(con, repo), {}
    for sha in focus:
        at = commits[sha]["at"]
        earlier = {s for h in every.get(sha, ()) for s in adders.get(h, ()) if s != sha and commits[s]["at"] <= at}
        for s in earlier:
            if _ancestor(history, s, sha, known):
                out[sha] |= every[sha] & every[s]
    return out


def _ran(con, repo: str, commits: dict) -> set[tuple[str, str]]:
    """Commits a chat made itself: git printed the sha, or the command carried the subject."""
    out: set[tuple[str, str]] = set()
    if not commits:
        return out
    by_prefix = {sha[:7]: sha for sha in commits}
    here = {r[0] for r in con.execute("select distinct session_id from link_edits where repo=?", (repo,))}
    here |= {r[0] for r in con.execute("select id from sessions where project_key=?", (repo,))}
    subjects = [(sha, " ".join((c["subject"] or "").split()), c["at"]) for sha, c in commits.items()]
    subjects = [x for x in subjects if len(x[1]) >= 12]
    for r in con.execute("select session_id, at, text, detail from link_events where kind='commit'"):
        at = r["at"] or 0
        said = " ".join((r["detail"] or "").split())
        for tok in set(_SHA.findall(said)):
            sha = by_prefix.get(tok[:7])
            # Made by this command, so not before it started, and printed with its subject
            # ("[main 4c613c9] Fix the login" or "4c613c9 Fix the login"): output often lists
            # other commits too.
            if not sha or not sha.startswith(tok) or not at - 120 <= commits[sha]["at"] <= at + 3600:
                continue
            subject = " ".join((commits[sha]["subject"] or "").split())
            if re.search(rf"\[[^\]]* {tok}\]", said) or (len(subject) >= 8 and subject[:24] in said):
                out.add((sha, r["session_id"]))
        if r["session_id"] in here:
            cmd = " ".join((r["text"] or "").split())
            for sha, subject, when in subjects:
                if at - SLACK <= when <= at + 12 * 3600 and subject in cmd:
                    out.add((sha, r["session_id"]))
    return out


def update(con, repo: str | None = None, log=None) -> dict:
    """Bring the links up to date: read the chats that changed, record new commits, and work
    out again only the links those touch. With repo, only that repo's commits are scanned. No
    model calls. If another process is updating, this one skips and the links stay as they are."""
    if not enabled():
        clear(con)
        return {"enabled": False}
    started = time.time()
    with _locked() as mine:
        if not mine:
            return {"busy": True}
        ensure(con)
        read = read_chats(con, log)
        prune(con)
        repos = [repo] if repo else [r[0] for r in con.execute("select distinct repo from link_edits")]
        linked = {r[0] for r in con.execute("select repo from link_repos")}
        changed = 0
        for name in repos:
            scan(con, name)
            todo = {r[0] for r in con.execute("select sha from link_todo where repo=?", (name,))}
            if name not in linked:
                changed += link(con, name)
            elif todo:
                changed += link(con, name, todo)
            if log and (name not in linked or todo):
                log(f"links: {name}: {len(todo) if name in linked else 'all'} commits worked out again")
    return {
        "chats_read": read,
        "repos": len(repos),
        "links_written": changed,
        "seconds": round(time.time() - started, 1),
    }


def stats(con) -> dict:
    if not present(con):
        return {"enabled": enabled(), "links": 0}

    def one(sql: str) -> int:
        return con.execute(sql).fetchone()[0]

    return {
        "chats_read": one("select count(*) from link_chats"),
        "chats_with_edits": one("select count(distinct session_id) from link_edits"),
        "repos": one("select count(distinct repo) from link_edits"),
        "commits_seen": one("select count(*) from link_commits"),
        "commits_linked": one("select count(*) from (select distinct repo, sha from commit_links)"),
        "made_by_a_chat": one("select count(*) from (select distinct repo, sha from commit_links where how='ran')"),
        "links": one("select count(*) from commit_links"),
    }


# --- recall why ------------------------------------------------------------------------


def _chat(con, sid: str) -> dict:
    r = con.execute(
        "select id, agent, title, first_prompt, started_at, updated_at from sessions where id=?", (sid,)
    ).fetchone()
    if not r:
        return {"chat_id": sid}
    return {
        "chat_id": sid,
        "agent": r["agent"],
        "title": " ".join((r["title"] or r["first_prompt"] or "").split())[:120],
        "date": datetime.fromtimestamp(r["updated_at"] or r["started_at"] or 0).strftime("%Y-%m-%d"),
    }


def chats_for(con, repo: str, sha: str) -> list[dict]:
    rows = con.execute(
        "select * from commit_links where repo=? and sha=? order by how='ran' desc, score desc, lines desc", (repo, sha)
    ).fetchall()
    out = []
    for r in rows:
        c = _chat(con, r["session_id"])
        c.update(how=r["how"], lines=r["lines"], of=r["commit_lines"], score=r["score"], asked=r["ask"] or None)
        out.append(c)
    return out


def _commit_meta(con, repo: str, sha: str, top: str) -> dict:
    r = con.execute("select * from link_commits where repo=? and sha=?", (repo, sha)).fetchone()
    if r:
        at, author, subject, parent = r["at"], r["author"], r["subject"], r["parent"]
    else:
        out = (git(top, "show", "-s", "--no-textconv", "--format=%ct%x1f%an%x1f%s%x1f%P", sha) or "").strip()
        parts = out.split("\x1f")
        if len(parts) < 4:
            return {"sha": sha}
        at, author, subject, parent = float(parts[0]), parts[1], parts[2], (parts[3].split() or [None])[0]
    return {
        "sha": sha,
        "date": datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M"),
        "author": author,
        "subject": subject,
        "parent": parent,
    }


def why(
    con,
    target: str | None = None,
    commit: str | None = None,
    cwd: str | None = None,
    top_n: int = 5,
    refresh: bool = True,
) -> dict:
    """The chats behind a file, a line (path:line or path:start-end) or a commit: git blame
    finds the commits, and the links name the chats. Links are brought up to date first."""
    cwd = cwd or os.getcwd()
    path, lines = target, None
    m = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", target or "")
    if m and not os.path.exists(os.path.join(cwd, os.path.expanduser(target))):
        path, lines = m.group(1), (int(m.group(2)), int(m.group(3) or m.group(2)))
    where = os.path.realpath(os.path.join(cwd, os.path.expanduser(path))) if path else os.path.realpath(cwd)
    top = (git(where if os.path.isdir(where) else os.path.dirname(where), "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        return {"error": f"{path or cwd} is not in a git repo"}
    project = resolve(top)
    if not project:
        return {"error": f"{top} is a scratch folder: recall links no chats there"}
    if not enabled():
        return {"error": "commit links are off: set [links] enabled = true in recall's config.toml"}
    if refresh:  # without it, the links as they stand: for a caller that cannot wait for the index's write lock
        with _locked() as mine:
            if mine:
                ensure(con)
                con.execute("insert or ignore into link_checkouts values (?,?)", (project.key, top))
                con.commit()
        update(con, project.key)
    if not present(con):
        return {"error": "commit links are not built yet: another recall process is building them"}
    out: dict = {"repo": project.key}
    if commit:
        sha = (git(top, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}") or "").strip()
        if not sha:
            return {"error": f"no commit {commit!r} in {top}"}
        out["commits"] = [{**_commit_meta(con, project.key, sha, top), "chats": chats_for(con, project.key, sha)}]
        return out
    rel = os.path.relpath(where, top)
    out["file"] = rel + (f":{lines[0]}" + (f"-{lines[1]}" if lines[1] != lines[0] else "") if lines else "")
    span = ["-L", f"{lines[0]},{lines[1]}"] if lines else []
    blame = git(top, "blame", "--porcelain", "--no-textconv", *span, "--", rel)
    if blame is None:
        return {"error": f"git blame failed for {rel}: is it tracked, and is the line range inside the file?"}
    texts: dict[str, list[str]] = {}
    sha = None
    for line in blame.split("\n"):
        if re.match(r"^[0-9a-f]{40} ", line):
            sha = line.split()[0]
        elif line.startswith("\t") and sha:
            texts.setdefault(sha, []).append(line[1:])
    found = []
    for sha, said in sorted(texts.items(), key=lambda x: -len(x[1]))[:top_n]:
        if sha == _EMPTY:
            meta, chats = {"sha": None, "subject": "not committed yet"}, _writers(con, project.key, rel, said)
        else:
            meta, chats = _commit_meta(con, project.key, sha, top), chats_for(con, project.key, sha)
            if not chats and lines:  # no confident link: the chats that wrote these very lines, if any
                chats = [dict(c, how="wrote the line") for c in _writers(con, project.key, rel, said)]
        if lines and lines[1] - lines[0] < 10:  # for a few lines, the request that led to them
            for c in chats[:3]:
                c.update(_line_ask(con, c["chat_id"], rel, said) or {})
        found.append({**meta, "lines": len(said), "chats": chats})
    out["commits"] = found
    out["blamed_lines"] = sum(len(x) for x in texts.values())
    return out


def _writers(con, repo: str, rel: str, lines: list[str]) -> list[dict]:
    """Chats that wrote any of these lines into the file, latest first."""
    want = _hashes(lines)
    out = []
    for r in con.execute(
        "select session_id, lines, weak, ask from link_edits where repo=? and path=? order by last_at desc", (repo, rel)
    ):
        if want & (_unpack(r["lines"]) | _unpack(r["weak"])):
            out.append(dict(_chat(con, r["session_id"]), how="wrote", asked=r["ask"] or None))
    return out[:5]


def _line_ask(con, sid: str, rel: str, lines: list[str]) -> dict | None:
    """What the person asked right before the chat wrote these lines, read again from its
    transcript (the index keeps one request per file)."""
    r = con.execute("select agent, source from sessions where id=?", (sid,)).fetchone()
    reader = READERS.get(r["agent"]) if r else None
    if not reader or not r["source"] or not compressed.resolve(r["source"]):
        return None
    want = _hashes(lines)
    with contextlib.suppress(Exception):
        for e in reversed(reader(r["source"]).edits):
            if (e.path == rel or e.path.endswith("/" + rel)) and want & _written(e) and e.ask:
                return {"asked": e.ask, "asked_at": e.at}
    return None


def why_rows(con, file: str | None, commit: str | None, refresh: bool = True) -> list[dict]:
    """`why` for an agent: one row per (commit, chat), or one row with an error or a note."""
    if commit:
        base = os.path.expanduser(re.sub(r":\d+(-\d+)?$", "", file or "")) or os.getcwd()
        result = why(con, None, commit, cwd=base if os.path.isdir(base) else os.path.dirname(base), refresh=refresh)
    else:
        result = why(con, file, refresh=refresh)
    if result.get("error"):
        return [{"error": result["error"]}]
    rows = []
    for c in result.get("commits") or []:
        head = {
            "sha": c.get("sha"),
            "committed": c.get("date"),
            "subject": c.get("subject"),
            "lines_here": c.get("lines"),
        }
        for x in c.get("chats") or [{"note": "no chat linked: written by hand, or by an agent recall does not read"}]:
            row = {**head, **{k: v for k, v in x.items() if k not in ("score", "asked_at", "date")}}
            if x.get("date"):
                row["chat_date"] = x["date"]
            if row.get("asked"):
                row["asked"] = row["asked"][:200]
            rows.append(row)
    return rows


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}


def render(result: dict) -> str:
    """`recall why` as text: each commit, then the chats behind it."""
    if result.get("error"):
        return result["error"]
    repo = re.sub(r"^(git|path):", "", result["repo"])
    out = [f"{result.get('file') or 'commit'}  ({repo})"]
    for c in result.get("commits") or []:
        here = f"  ({c['lines']} line{'s' if c['lines'] != 1 else ''} here)" if result.get("file") else ""
        sha = (c.get("sha") or "")[:8] or "working copy"
        out.append(f"\n{sha}  {(c.get('date') or '')[:10]}  {c.get('subject', '')}{here}")
        chats = c.get("chats") or []
        if not chats:
            out.append(
                "  no chat linked: written by hand, through a shell command, or by an agent recall does not read"
            )
        elif c.get("sha") and not any(x.get("how") == "ran" for x in chats):
            out.append("  committed outside these chats: by hand, or by a tool recall does not read")
        for x in chats:
            agent = AGENT_NAMES.get(x.get("agent"), x.get("agent") or "?")
            out.append(f"  {agent} · {x.get('date', '?')} · {x.get('title') or '(untitled)'}")
            how = x.get("how")
            wrote = f"wrote {x['lines']} of its {x['of']} lines" if x.get("lines") and x.get("of") else ""
            what = {
                "ran": "made this commit" + (f" and {wrote}" if wrote else ""),
                "wrote": wrote or "wrote these lines",
                "wrote the line": "wrote these lines (the commit itself is not linked with confidence)",
            }.get(how, how or "")
            out.append(f"    {what} · {x['chat_id']}")
            if x.get("asked"):
                out.append(f'    asked: "{x["asked"][:160]}"')
    return "\n".join(out)


# --- failure, then fix: the start of a personal regression gym ----------------------------


FAILURES = "('check', 'command', 'correction')"


def episodes(con, repo: str | None = None, days: int = 7) -> list[dict]:
    """Commits that fixed something that had failed: one episode per fixing commit, each
    failure counted once, for the first commit that followed it.

    A failure is a check or command that exited non-zero while the chat was editing, a
    message from the person saying the work is wrong or still broken, or a revert of an
    earlier commit to the same files. It must come after the chat's previous commit to those
    files and before its last edit to them, so that work followed it. When the fixing chat
    shows none, an earlier chat on the same files (within `days`) whose work never reached
    a commit can supply one. Each episode gives the repo, the parent commit (where to start
    from), the person's request, the failures and the fixing commit. Rough heuristics, with
    no check that the failure and the fix are about the same thing: see the precision
    measured in the PR that added this.
    """
    if not present(con):
        return []
    sql = (
        "select l.*, c.at, c.parent, c.subject from commit_links l "
        "join link_commits c on c.repo=l.repo and c.sha=l.sha where l.files != '[]'"
        + (" and l.repo=?" if repo else "")
        + " order by c.at"
    )
    used: set = set()
    before: dict[tuple[str, str], list[tuple[float, set]]] = {}  # (chat, repo) -> its earlier commits
    out: dict[tuple[str, str], dict] = {}
    for lk in con.execute(sql, (repo,) if repo else ()).fetchall():
        files, sid, at = set(json.loads(lk["files"])), lk["session_id"], lk["at"]
        q = ",".join("?" * len(files))
        last = con.execute(
            f"select max(last_at) from link_edits where session_id=? and repo=? and path in ({q})",
            (sid, lk["repo"], *files),
        ).fetchone()[0]
        mine = before.setdefault((sid, lk["repo"]), [])
        prev = max((t for t, fs in mine if fs & files), default=float("-inf"))
        mine.append((at, files))
        if not last:
            continue
        found = [
            dict(e)
            for e in con.execute(
                f"select * from link_events where session_id=? and kind in {FAILURES} and at > ? and at <= ? "
                "order by n",
                (sid, prev, min(last, at)),
            )
        ]
        for other in [] if found else _earlier(con, lk["repo"], files, sid, at, days):
            found = [
                dict(e)
                for e in con.execute(
                    f"select * from link_events where session_id=? and kind in {FAILURES} order by n", (other,)
                )
            ]
            if found:
                break
        if not found and (r := _reverted(con, lk["repo"], files, max(prev, at - days * 86400), at)):
            found = [r]
        found = [f for f in found if (f["session_id"], f.get("n"), f["kind"], f["at"]) not in used]
        if not found:
            continue
        used |= {(f["session_id"], f.get("n"), f["kind"], f["at"]) for f in found}
        ep = out.setdefault(
            (lk["repo"], lk["sha"]),
            {
                "repo": lk["repo"],
                "parent": lk["parent"],
                "fix_commit": lk["sha"],
                "fix_subject": lk["subject"],
                "fix_chat": sid,
                "files": sorted(files),
                "prompt": _prompt(con, found[-1]["session_id"] or sid, lk["repo"], files) or lk["ask"],
                "failures": [],
            },
        )
        ep["failures"] += [{k: f.get(k) for k in ("session_id", "kind", "text", "detail", "at")} for f in found]
        ep["same_chat"] = all(f["session_id"] == sid for f in ep["failures"])
    for ep in out.values():
        # The one to show: the latest failed check or correction, which say more about the code
        # than any command that exited non-zero; the latest failure of any kind otherwise.
        strong = [f for f in ep["failures"] if f["kind"] in ("check", "correction", "revert")]
        ep["failure"] = (strong or ep["failures"])[-1]
        ep["failure_count"] = len(ep["failures"])
        ep["failure_kinds"] = dict(Counter(f["kind"] for f in ep["failures"]))
        ep["failures"] = ep["failures"][-10:]
    return list(out.values())


def _reverted(con, repo: str, files: set[str], start: float, end: float) -> dict | None:
    """A revert (git's own `Revert "…"`, as linking reads it) of an earlier commit to these files."""
    q = ",".join("?" * len(files))
    row = con.execute(
        "select c.sha, c.subject, c.at from link_commits c join link_commit_files f on f.repo=c.repo and f.sha=c.sha "
        f"where c.repo=? and f.path in ({q}) and c.subject like 'Revert \"%' and c.at > ? and c.at < ? "
        "order by c.at desc limit 1",
        (repo, *files, start, end),
    ).fetchone()
    if not row:
        return None
    return {
        "session_id": None,
        "n": None,
        "kind": "revert",
        "text": row["subject"],
        "detail": row["sha"],
        "at": row["at"],
    }


def _earlier(con, repo: str, files: set[str], sid: str, at: float, days: int) -> list[str]:
    q = ",".join("?" * len(files))
    rows = con.execute(
        f"select session_id, max(last_at) last from link_edits where repo=? and path in ({q}) and session_id != ? "
        "group by session_id having last < ? and last > ? order by last desc",
        (repo, *files, sid, at, at - days * 86400),
    ).fetchall()
    linked = {r[0] for r in con.execute("select session_id from commit_links where repo=?", (repo,))}
    return [r["session_id"] for r in rows if r["session_id"] not in linked]


def _prompt(con, sid: str, repo: str, files: set[str]) -> str | None:
    q = ",".join("?" * len(files))
    row = con.execute(
        f"select first_ask from link_edits where session_id=? and repo=? and path in ({q}) and first_ask != '' "
        "order by first_at limit 1",
        (sid, repo, *files),
    ).fetchone()
    return row["first_ask"] if row else None

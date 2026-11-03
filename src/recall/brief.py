"""A brief: what recall knows that bears on one task, packed small enough to hand an agent as
it starts.

`recall brief "<task>" [--cwd PATH] [--budget N]` prints one, and the subagent hooks hand one to
every subagent as it starts (on_subagent_start). A subagent starts with none of its session's
memory: no subagent sees what the session-start hook loaded for its parent, and Claude Code's
Explore and Plan skip CLAUDE.md too. Agents almost never ask recall on their own (0 voluntary
memory calls in 114 turns, October 2026), so what reaches a subagent is what its harness hands it.

A brief holds, within a budget (about 1,000 tokens by default):
  rules   the rules in force for the project and for the files the task names: notes' rules
          ranked against the task, each logged with its note, rule id and version, and the
          repo's rule files this agent does not load itself
  chats   the two or three earlier chats that best match the task, subagents' included, each
          as a dated window around its best match (windows(), which other readers reuse), with
          the files it edited, the checks it ran and the commits it made (commits.py)
  open    the project's open threads that match the task

Receipts. Every brief delivered is logged (usage.record, tool "brief") with the ids it carried:
rules as "<note id>/<rule id>@<version>", repo rule files as "file:<path>@<version>", where a
version is the first 8 hex digits of the SHA-1 of the rule's text (version()), so it changes
exactly when the wording does; and the note and chat ids. Which rules, at which version, reached
which session can then be joined to what followed.

Like jit.py this runs inside hooks: the index is opened read-only, chats are ranked by keywords
only, nothing calls a model or imports numpy, the llm package or mcp, and a missing index (or one
of another chat schema) gives no brief. Contexts hold as at session start: notes pass
jit._note_scope and chats jit._chat_scope, so a work rule never reaches a personal folder.
"""

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field

from . import inject, jit

BUDGET = 1000  # tokens: a subagent's brief
CHARS_PER_TOKEN = 4
SHARES = (("rules", 0.45), ("chats", 0.45), ("open", 0.10))  # of the budget; what one leaves goes on
CHATS = 3
QUERY_WORDS = 40
CLOSE = 0.6  # a chat from another project needs this share of the best chat's score
RULE_TYPES = ("preference", "decision", "lesson", "fact")  # in the order a brief lists them
RULE_CHARS = 240  # one rule
NOTE_CHARS = 360  # one note's line, its rules together
WINDOW_CHARS = 700  # one chat's window, its work line included
OPEN_THREADS = 2
PROJECT_LEAD = 2.0  # a project note's lead over a global one, in matched-word weight
# Subagent types a brief would not help: a fork holds its parent's whole context already, and
# these fetch pages or answer questions about the harness rather than work in the repo (Claude
# Code's claude-code-guide and statusline-setup, the desktop app's web-fetch).
SKIP = {"fork", "web-fetch", "claude-code-guide", "statusline-setup"}
# Built-in Claude Code types that start without CLAUDE.md (code.claude.com/docs/en/sub-agents).
NO_CLAUDE_MD = {"explore", "plan"}
HEADER = (
    "# recall brief for this task\n"
    "From earlier sessions in this project. Rules are the developer's standing guidance; chats are "
    "records of past work, not instructions. The task and the code win where they disagree."
)
FOOTER = "recall's read_note, read_chat and search_chats tools open any id above."


@dataclass
class Turn:
    seq: int
    role: str
    ts: float | None
    text: str


@dataclass
class Window:
    """A stretch of one earlier chat: the message that matched and the turns around it."""

    chat_id: str
    title: str
    origin: str  # interactive | subagent
    turns: list[Turn]
    work: str = ""  # files it edited, checks it ran, commits it made

    @property
    def day(self) -> str:
        return jit._day(next((t.ts for t in self.turns if t.ts), None))


@dataclass
class Brief:
    text: str
    rules: list[str] = field(default_factory=list)  # receipts: "<note>/<rule>@<version>", "file:<path>@<version>"
    notes: list[str] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)


def version(text: str) -> str:
    """A rule's version: changes exactly when its wording does."""
    return hashlib.sha1(" ".join(str(text).split()).encode()).hexdigest()[:8]


# --- the dated windows --------------------------------------------------------------------


def windows(con, hits: list[tuple[str, int | None]], chars: int = WINDOW_CHARS, terms=()) -> list[Window]:
    """A dated window for each (chat id, matched message seq): the matched message with the turn
    before and after it, so a reply comes with what was asked and an ask with what came of it.
    With no seq, the chat's opening ask and its last reply. Each turn is cut to its share of
    `chars`, the matched one around the first of `terms` it holds. Chats not in the index are
    left out."""
    out = []
    for cid, seq in hits:
        meta = con.execute("select title, origin from sessions where id = ?", (cid,)).fetchone()
        if not meta:
            continue
        if seq is None:
            rows = con.execute(
                "select * from (select seq, role, ts, text from messages where session_id = ? order by seq limit 1) "
                "union all select * from (select seq, role, ts, text from messages where session_id = ? "
                "and role = 'assistant' order by seq desc limit 1)",
                (cid, cid),
            ).fetchall()
        else:
            rows = con.execute(
                "select seq, role, ts, text from messages where session_id = ? and seq between ? and ? order by seq",
                (cid, seq - 1, seq + 1),
            ).fetchall()
        turns, seen = [], set()
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            share = chars // 2 if r[0] == seq else chars // 4
            text = _around(r[3], terms, share) if r[0] == seq else jit._gist(r[3], share)
            turns.append(Turn(r[0], r[1], r[2], text))
        if turns:
            out.append(Window(cid, meta[0] or "", meta[1] or "", turns))
    return out


def render_window(w: Window, chars: int = WINDOW_CHARS) -> str:
    """A window as brief lines: the chat's date, id and title, then one line per turn."""
    kind = "subagent " if w.origin == "subagent" else ""
    at = next((t.seq for t in w.turns if t.role == "user"), w.turns[0].seq if w.turns else 0)
    head = f'- {w.day}, {kind}chat {short_id(w.chat_id)} "{jit._clip(w.title, 70)}", from message {at}'
    lines = [head]
    for t in w.turns:
        who = "asked" if t.role == "user" else "agent"
        lines.append(f"  {who}: {t.text}")
    if w.work:
        lines.append(f"  {w.work}")
    text = "\n".join(inject.line(ln) if i == 0 else "  " + inject.line(ln) for i, ln in enumerate(lines))
    return text if len(text) <= chars else text[: chars - 1].rsplit(" ", 1)[0] + "…"


def short_id(chat_id: str) -> str:
    """A chat id short enough to show and long enough for read_chat to resolve: a generated id
    cut to its first 8 hex digits (past the "agent-" a Claude Code subagent's id starts with);
    any other id whole, since a prefix of a name says nothing."""
    agent, _, native = chat_id.partition(":")
    m = re.match(r"(agent-)?[0-9a-f]{8}", native)
    return f"{agent}:{m.group(0)}" if m and len(native) > len(m.group(0)) + 4 else chat_id


def _around(text: str, terms, n: int) -> str:
    """The part of a message around the first query word it holds, on one line."""
    flat = " ".join(re.sub(r"```.*?```", " [code] ", text or "", flags=re.S).split())
    low = flat.lower()
    at = min((i for t in terms if len(t) > 2 and (i := low.find(t)) >= 0), default=0)
    start = max(0, at - n // 3)
    piece = flat[start : start + n]
    return ("…" if start else "") + jit._clip(piece, n - (1 if start else 0))


# --- what goes in ------------------------------------------------------------------------


def _chats(con, task: str, cwd: str | None, project: dict | None, own: list[str]) -> list[tuple[str, int | None]]:
    """The best few earlier chats for the task, each with the message to open its window at:
    interactive chats and subagents' (headless runs hold copies), none of this session's own
    thread and none still running; the project's own first, and from another project only when
    close to the best. A
    chat's window opens at its best-matching reply, where findings are, rather than at an ask
    that merely repeats the task's words."""
    from .chats import store

    explain: dict = {}
    rows = store.search(
        con, task, limit=10, exclude=own, vector=False, explain=explain, where=jit._chat_scope(con, cwd)
    )
    if not rows:
        return []
    ids = [r["chat_id"] for r in rows]
    meta = {
        r[0]: r
        for r in con.execute(
            f"select id, origin, project_key, updated_at from sessions where id in ({','.join('?' * len(ids))})", ids
        )
    }
    threads = store._subagent_threads(con) if own else {}
    scores = explain.get("scores", {})
    best = max((scores.get(i, 0.0) for i in ids), default=0.0) or 1.0
    live = time.time() - jit.LIVE_SECONDS
    out = []
    for r in rows:
        cid = r["chat_id"]
        origin, key, updated = meta[cid][1], meta[cid][2], meta[cid][3] or 0
        if origin not in ("interactive", "subagent") or threads.get(cid) in own or updated > live:
            continue
        same = project is not None and jit._same_project(project, key)
        if not same and scores.get(cid, 0.0) < CLOSE * best:
            continue
        seqs = [h["seq"] for h in r.get("hits") or [] if h.get("seq") is not None]
        roles = (
            dict(
                con.execute(
                    f"select seq, role from messages where session_id = ? and seq in ({','.join('?' * len(seqs))})",
                    [cid, *seqs],
                ).fetchall()
            )
            if seqs
            else {}
        )
        out.append((not same, len(out), cid, next((q for q in seqs if roles.get(q) == "assistant"), r.get("match_at"))))
    return [(cid, seq) for *_, cid, seq in sorted(out)[:CHATS]]


def _work(con, cid: str) -> str:
    """What a chat did to code, from the commit links: the files it edited (most edited first),
    the last checks it ran, and the commits it made."""
    from . import commits

    if not commits.present(con):
        return ""
    try:
        files = [
            r[0]
            for r in con.execute("select path from link_edits where session_id = ? order by n desc limit 6", (cid,))
        ]
        checks = [
            r[0].split("\n")[0]
            for r in con.execute(
                "select text from link_events where session_id = ? and kind = 'check' order by n desc limit 2", (cid,)
            )
        ]
        made = [
            f"{r[0][:7]} {jit._clip(r[1] or '', 50)}"
            for r in con.execute(
                "select l.sha, c.subject from commit_links l join link_commits c on c.repo = l.repo and c.sha = l.sha "
                "where l.session_id = ? order by c.at desc limit 2",
                (cid,),
            )
        ]
    except Exception:  # an older index without these tables
        return ""
    parts = []
    if files:
        parts.append("edited " + ", ".join(os.path.basename(f) if len(f) > 60 else f for f in files))
    if checks:
        parts.append("ran " + "; ".join(jit._clip(c, 80) for c in dict.fromkeys(checks)))
    if made:
        parts.append("committed " + "; ".join(made))
    return " · ".join(parts)


def _notes(con, task: str, project: dict | None, cwd: str | None, kinds: tuple[str, ...]) -> list:
    """Notes of these kinds a session in cwd may be handed, best first. A note scores the task
    words its title and rules hold, each weighted by how few notes hold it (so "recall" in a
    recall project counts for little); the project's own notes get a lead and fill what is left
    by confidence, corroboration and recency, and a global one goes in only when it holds two
    of the task's words."""
    import math

    scope, args = jit._note_scope(con, project, cwd)
    marks = ",".join("?" * len(kinds))
    try:
        pool = con.execute(
            f"""select n.id, n.path, n.type, n.scope, n.title, n.brief, n.last_seen, n.confidence, n.n_sources
                from notes n where ({scope}) and n.type in ({marks}) and n.brief != ''""",
            [*args, *kinds],
        ).fetchall()
    except Exception:  # no notes index yet
        return []
    stems = list(dict.fromkeys(w[:6] for w in jit.content_words(task) if len(w) > 2))
    held = {r[0]: {t for t in stems if t in f"{r[4]} {r[5]}".lower()} for r in pool}
    df = {t: sum(t in h for h in held.values()) for t in stems}
    now = time.time()

    def rank(r) -> float:
        weight = sum(math.log((1 + len(pool)) / (1 + df[t])) for t in held[r[0]])
        return weight + (PROJECT_LEAD if r[3] == "project" else 0.0) + _score(r, now)

    keep = [r for r in pool if r[3] == "project" or len(held[r[0]]) >= 2]
    return sorted(keep, key=rank, reverse=True)


def _score(row, now: float) -> float:
    """notes.Note.score, from the index's columns."""
    from .memory.notes import CONFIDENCE

    age = (now - jit._ts(row[6], now - 365 * 86400)) / 86400
    return (
        CONFIDENCE.get(row[7], 0.5) * (1 + 0.5 * min((row[8] or 1) - 1, 4)) * (0.4 + 0.6 * 0.5 ** (max(age, 0) / 180))
    )


def _rule_lines(rows, chars: int) -> tuple[list[str], list[str], list[str]]:
    """Each note's rules in force, as lines grouped under the note's title, until `chars` is
    spent; and the receipts for what went in. The rules are read from the note's file, where
    their ids are (the index keeps only the rendered text); a rule notes.pushed holds back, or
    one only the agent stated, never goes."""
    from .memory.notes import pushed, read_note_file

    lines, receipts, notes, used = [], [], [], 0
    for row in rows:
        note = read_note_file(row[1]) if row[1] else None
        shown = pushed(note) if note else None
        if note and not shown:
            continue
        head = f"- {jit._clip(row[4], 90)}: "
        tail = f" (note {row[0]})"
        texts, pieces = [], []  # the rules that fit on the note's line, whole or cut; receipts name only those
        for rid, text in [(r.id, r.text) for r in shown] if shown else [(None, row[5])]:
            piece = jit._clip(text, RULE_CHARS).rstrip(".")
            if pieces and len(head + "; ".join([*pieces, piece]) + tail) > NOTE_CHARS:
                break
            texts.append((rid, text))
            pieces.append(piece)
        line = inject.line(head + jit._clip("; ".join(pieces), NOTE_CHARS - len(head) - len(tail)) + tail)
        if used + len(line) + 1 > chars:
            short = inject.line(head.rstrip(": ") + tail)
            if used + len(short) + 1 > chars:
                break
            line, texts = short, []  # the title alone: read_note has the rules
        lines.append(line)
        used += len(line) + 1
        notes.append(row[0])
        receipts += [f"{row[0]}/{rid or '-'}@{version(t)}" for rid, t in texts]
    return lines, receipts, notes


def _file_rules(cwd: str | None, agent: str, subagent: str | None, files: list[str]) -> tuple[list[str], list[str]]:
    """The repo's rule files this agent does not load itself: always-on ones another agent
    gets, CLAUDE.md and AGENTS.md for the Claude Code types that skip them, and rules scoped to
    a file the task names. Lines name the file to read; receipts carry each file's version."""
    from . import rules

    root = rules.repo_root(cwd)
    if not root or agent not in rules.AGENTS:
        return [], []
    try:
        repo = rules.scan(root)
    except Exception:
        return [], []
    skips_md = agent == "claude" and (subagent or "").lower() in NO_CLAUDE_MD
    picked = []
    for s in repo.sources:
        if s.apply == "always" and (agent not in s.seen_by or (skips_md and s.kind in ("agents", "claude"))):
            picked.append((s, ""))
        elif s.apply == "files" and agent not in s.seen_by and s.globs:
            rels = [os.path.relpath(f, root) if os.path.isabs(f) else f for f in files]
            patterns = [g.strip() for g in re.split(r"[,\s]+", s.globs) if g.strip()]
            if any(jit._glob(rel, g) for rel in rels for g in patterns):
                picked.append((s, f", for {inject.line(s.globs, 60)}"))
    lines, receipts = [], []
    for s, scope in picked:
        what = inject.line((s.description or rules._first_line(s.body)).replace("`", ""), 140).rstrip(".")
        path = inject.line(s.path.replace("`", ""), 120)
        lines.append(f"- {what or 'project instructions'} (read `{path}`{scope})")
        receipts.append(f"file:{s.path}@{version(s.body)}")
    return lines, receipts


def _query(task: str) -> str:
    """The task as a search: its first QUERY_WORDS distinct content words. A subagent's prompt
    runs to thousands of characters and puts what it is for first; OR-ing every word in it would
    rank chats by its boilerplate. Harness tags are stripped rather than the prompt refused, as a
    person's prompt opening with one would be (jit.askable): a subagent's prompt is the agent's."""
    from .chats.transcript import clean_user_text

    words = jit.content_words(clean_user_text(task or ""))
    return " ".join(words[:QUERY_WORDS]) if len(words) >= jit.MIN_WORDS else ""


def build(
    task: str,
    cwd: str | None,
    budget: int = BUDGET,
    agent: str = "claude",
    subagent: str | None = None,
    own: list[str] | None = None,
    con=None,
) -> Brief:
    """The brief for a task in a folder, within `budget` tokens. own: chat ids to leave out (the
    session the brief is for, and with it the subagents of its thread). An empty Brief when
    recall knows nothing that bears on it, or has no index to read."""
    con = con or jit._connect()
    if con is None:
        return Brief("")
    own = own or []
    query = _query(task)
    project = jit._project(con, cwd)
    room = budget * CHARS_PER_TOKEN - len(HEADER) - len(FOOTER) - 80
    shares = {name: int(room * share) for name, share in SHARES}
    out = Brief("")

    file_lines, file_receipts = _file_rules(cwd, agent, subagent, jit.files_named(task))
    note_lines, note_receipts, out.notes = _rule_lines(
        _notes(con, query, project, cwd, RULE_TYPES), shares["rules"] - sum(len(x) + 1 for x in file_lines)
    )
    rule_lines = file_lines + note_lines
    out.rules = file_receipts + note_receipts
    left = shares["rules"] - sum(len(x) + 1 for x in rule_lines)

    chat_lines, chars = [], shares["chats"] + max(0, left)
    if query:
        terms = jit.content_words(query)
        found = windows(con, _chats(con, query, cwd, project, own), WINDOW_CHARS, terms)
        for w in found:
            w.work = _work(con, w.chat_id)
            text = render_window(w, min(WINDOW_CHARS, chars // max(1, len(found))))
            chat_lines.append(text)
            out.chats.append(w.chat_id)
    left = chars - sum(len(x) + 1 for x in chat_lines)

    open_lines = []
    room_open = shares["open"] + max(0, left)
    for row in _notes(con, query, project, cwd, ("open",))[:OPEN_THREADS]:
        line = jit._note_line(row[0], row[4], row[5], row[6])
        if sum(len(x) + 1 for x in open_lines) + len(line) + 1 > room_open:
            break
        open_lines.append(line)
        out.notes.append(row[0])

    sections = [
        ("## Rules in force", rule_lines),
        ("## Earlier chats", chat_lines),
        ("## Open threads", open_lines),
    ]
    body = "\n\n".join(h + "\n" + "\n".join(lines) for h, lines in sections if lines)
    if not body:
        return Brief("")
    # The tag lets every chat parser strip the brief, so it is never indexed as the subagent's words.
    out.text = f"<recall-memory>\n{HEADER}\n\n{body}\n\n{FOOTER}\n</recall-memory>"
    return out


# --- delivery -------------------------------------------------------------------------------
#
# The hook dispatcher (integrations/hooks.py) calls on_subagent_start for every subagent_start
# event. Where each agent's brief goes (checked October 2026):
#
#   claude  PreToolUse on Agent|Task, for every subagent type: tool_input holds the prompt and
#           subagent_type, and the brief goes back appended to the prompt through updatedInput
#           (tested live on 2.1.287 for the explore cache). SubagentStart is not used: it
#           carries no prompt, so its brief could not be shaped by the task, and both would
#           deliver twice.
#   codex   SubagentStart (0.163's own hook schema: agent_id, agent_type, cwd, session_id,
#           transcript_path; no task), answered with hookSpecificOutput.additionalContext. The
#           task is read from the parent's rollout: its last spawn_agent call's task_name and the
#           person's last ask (the spawn's message is encrypted there). That transcript_path is
#           the parent's, and that the call is written before the hook runs, are inferred, not
#           run live; without them the brief is the project's rules alone.
#   cursor  preToolUse on its subagent tool (Task, Subagent or mcp_task), whose updated_input
#           is merged into the call (3.24.18's bundle, not run live). subagentStart carries the
#           task in 3.24 but its additional_context is still dropped, so it is only observed.


def enabled() -> bool:
    """`[brief] subagents = false` in config.toml stops briefs at subagent start."""
    from . import config

    return config.load().get("brief", {}).get("subagents", True) is not False


def on_subagent_start(e):
    """subagent_start: the brief for the subagent's task, logged with what it carried."""
    if e.observe or (e.subagent or "").lower() in SKIP or not enabled():
        return None
    task = e.prompt or ""
    if "<recall-memory>" in task:  # its parent passed one on, or another hook already added it
        return None
    about = (e.raw.get("tool_input") or {}).get("description") if e.raw else None
    task = f"{about}. {task}" if about and task else task
    if not task and e.agent == "codex":
        task = _codex_task(e.transcript)
    started = time.perf_counter()
    b = build(task, e.cwd, agent=e.agent, subagent=e.subagent, own=jit._own(e))
    _log(e, b, started, bool(task))
    if not b.text:
        return None
    from .integrations.hooks import Reply

    return Reply(context=b.text)


def _log(e, b: Brief, started: float, shaped: bool) -> None:
    from . import usage

    raw = e.raw or {}
    usage.record(
        "hook",
        "brief",
        {"cwd": e.cwd, "agent_type": e.subagent},
        b.text or None,
        client=e.agent,
        session=e.session,
        subagent=raw.get("agent_id") or raw.get("tool_use_id"),
        task=shaped,
        rules=b.rules,
        notes=b.notes,
        chats=b.chats,
        ms=usage.ms_since(started),
    )


TAIL = 512 * 1024  # of the parent's rollout read for the task


def _codex_task(path: str | None) -> str:
    """What a Codex subagent was started for, from its parent's rollout: the last spawn_agent
    call's task name (its message is encrypted) and the person's last ask before it."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    name = ask = ""
    for line in reversed(lines):
        if '"spawn_agent"' not in line and '"role":"user"' not in line:
            continue
        try:
            p = json.loads(line).get("payload") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        if not name and p.get("type") == "function_call" and p.get("name") == "spawn_agent":
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                name = str(json.loads(p.get("arguments") or "{}").get("task_name") or "").replace("_", " ")
        elif not ask and p.get("type") == "message" and p.get("role") == "user":
            text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
            if text.strip() and not text.lstrip().startswith("<"):
                ask = text[:1500]
        if ask and name:
            break
    return f"{name}. {ask}".strip(". ") if name or ask else ""

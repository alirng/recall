"""`recall remember "<rule>"`: a rule the developer states, written into the notes at once.

The nightly run learns rules from chats a day late, and only as a model's summary. A rule the
developer states outright is the best input memory gets, so it goes straight into the notes as a
note of its own: one rule, with an id, the person's words, a scope (the project of the folder it
ran in, or global with --global) and, when global, the folder it ran in, which routes it to that
folder's context ([contexts] in config.toml) as a chat there would be. The same text remembered
again is the same rule: its id is kept and its date moved on.

It also leaves a candidate for the nightly run, already merged into its rule (see consolidate.py),
so the run can retire an older rule it replaces and fold duplicates into it. Consolidation keeps
the person's words: a remembered rule changes only through the person (a hand edit, a newer
remember, or what they type in a later chat).

Who said it (said.py): `developer` when the person ran the command at their own terminal, or an
agent ran it in reply to the person's own message, with the person's words as `--quote` found in
one of their latest typed turns of the live chat. Anything else an agent runs is saved as
`said_by: agent`: a candidate no session is handed unasked (notes.pushed), at low confidence so
that it fades unless the nightly run finds the person typed it. An agent cannot save a risky rule
(said.risky) at all. recall's MCP tools stay read-only: only this command writes a rule.
"""

import glob
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

from .. import contexts
from ..chats.projects import resolve
from . import said
from .notes import Note, NoteStore, Rule, clean_title, new_id, rule_id, sync

# Set in the commands an agent runs: Claude Code (documented: CLAUDECODE; observed:
# CLAUDE_CODE_SESSION_ID), Codex (its source: CODEX_THREAD_ID, CODEX_SANDBOX, CODEX_CI),
# Cursor (CURSOR_AGENT, reported in its forum, not documented).
AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI", "CURSOR_AGENT")
# Process names of an agent above the command (Cursor's agent shell is a terminal, so a person's
# terminal is told from it by what started it). The desktop apps are "Claude" and "Codex".
AGENT_PROCESSES = ("claude", "codex", "cursor-agent")
LATEST_TURNS = 3  # the person's turns a quote may come from: the reply is to what they just said
TITLE_CHARS = 72
# How Claude Code records a typed `/recall ...`: at the very start of the turn, never inside other text.
_COMMAND_ARGS = re.compile(
    r"\s*(?:<command-message>[^<]*</command-message>\s*)?<command-name>/?recall</command-name>\s*"
    r"<command-args>(.*?)</command-args>",
    re.S,
)
CURSOR_LIVE_SECONDS = 900  # a Cursor transcript written this recently, in this folder, is the live chat


@dataclass
class Remembered:
    note: Note
    rule: Rule
    new: bool  # False: the same rule was already there and was confirmed
    contexts: list[str] = field(default_factory=list)  # where a global rule loads; empty: everywhere
    changed: bool = (
        True  # False: the rule was already there and this run could not vouch for it, so nothing was written
    )


class Refused(Exception):
    pass


# --- who said it -------------------------------------------------------------------------


def speaker(rule: str, quote: str | None, folder: str) -> tuple[str, str | None, str]:
    """(said_by, the chat it was verified in, how it was decided) for a rule about to be saved.
    `folder` is the command's own working folder when an agent runs it (cmd_remember)."""
    agent = _under_agent()
    if not agent:
        return ("unverified" if said.risky(rule) else "developer"), None, "typed at your terminal"
    if said.risky(rule):
        raise Refused(
            "an agent cannot save a rule with a command, a URL, or a network, destructive or exfiltrating "
            "step; type it yourself with `recall remember` at your terminal"
        )
    in_cursor = "Cursor" in agent or any(os.environ.get(k) for k in ("CURSOR_AGENT", "CURSOR_TRACE_ID"))
    # The chat that vouches for the quote is the one the harness says this command runs in. An agent
    # cannot name another (--chat), or it could pick a chat whose latest turns suit its rule.
    chat = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    found = live_chat(chat) if chat else _cursor_chat(folder) if in_cursor else None
    if found is None:
        return "agent", None, f"run by {agent}; no chat to check the quote against, so the nightly run confirms it"
    chat_id, turns = found
    if said.stated(rule if quote is None else quote, rule, turns[-LATEST_TURNS:]):
        return "developer", chat_id, f"your words, found in your latest message in {chat_id}"
    return "agent", chat_id, f"run by {agent}; your latest messages in {chat_id} do not state it, so it waits"


def _under_agent() -> str | None:
    """The agent the command runs under, or None when a person ran it at their own terminal."""
    if not sys.stdin.isatty():
        return "a script or an agent"
    if env := next((k for k in AGENT_ENV if os.environ.get(k)), None):
        return f"an agent ({env} is set)"
    pid = os.getppid()
    for _ in range(40):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return "an agent (the process tree could not be read)"  # unknown: treated as the agent, not the person
        parent, _, comm = out.stdout.strip().partition(" ")
        name = os.path.basename(comm.strip())
        if name in AGENT_PROCESSES or name.startswith("Cursor"):
            return f"an agent ({name})"
        if not parent.strip().isdigit() or int(parent) <= 1:
            return None
        pid = int(parent)
    return None


def live_chat(chat: str) -> tuple[str, list[str]] | None:
    """The person's typed turns, oldest first, in the live chat an agent runs the command in,
    read from the agent's own file (read-only). A Claude Code chat records a typed `/recall ...`
    command as wrapper tags that typed_text cuts, so the command's arguments are taken as typed
    words. Codex keeps `$recall ...` as written. Cursor gives a command no chat id (_cursor_chat)."""
    from .. import roots
    from ..chats.sources import codex, read_jsonl, text_blocks
    from ..chats.sources.claude import _by_person

    chat = chat.split(":", 1)[-1]
    if not re.fullmatch(r"[\w-]{8,80}", chat):
        return None
    found = roots.discover()
    homes = {a: [r.path for r in found if r.agent == a] or [os.path.expanduser(f"~/.{a}")] for a in ("claude", "codex")}
    paths = [p for home in homes["claude"] for p in glob.glob(os.path.join(home, "projects", "*", f"{chat}.jsonl"))]
    for path in paths[:1]:
        turns = []
        for d in read_jsonl(path):
            att = d.get("attachment") or {}
            if d.get("type") == "attachment" and att.get("type") == "queued_command":
                if att.get("commandMode") == "prompt" and _human(att) and _by_person(att) and _by_person(d):
                    turns.append(said.typed_text(str(att.get("prompt") or "")))
                continue
            if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
                continue
            if not _human(d) or not _by_person(d):
                continue
            text = text_blocks((d.get("message") or {}).get("content"), ("text",))
            if text:
                args = _COMMAND_ARGS.match(text)
                turns.append(" ".join([args.group(1) if args else "", said.typed_text(text)]))
        return f"claude:{chat}", [t for t in turns if t.strip()]
    for folder in homes["codex"]:
        for plain, path in codex._threads(folder).items():
            if plain.endswith(f"{chat}.jsonl"):
                s = codex.load(path, {})
                turns = [said.typed_text(m.text) for m in (s.messages if s else []) if m.role == "user"]
                return f"codex:{chat}", [t for t in turns if t.strip()]
    return None


def _human(entry: dict) -> bool:
    """Did Claude Code record this record as typed by the person? Only a record it tags so counts:
    the desktop app's PR auto-fix events (review comments) arrive as user records with
    turnOrigin "sdk", or as queued commands with no origin, and a transcript from before the tags
    existed cannot say. Both are left out, so the quote is then not found and the rule waits."""
    origin = entry.get("origin")
    return entry.get("turnOrigin") == "human" or (isinstance(origin, dict) and origin.get("kind") == "human")


def _cursor_chat(folder: str) -> tuple[str, list[str]] | None:
    """Cursor tells a command nothing of its chat, so the live one is the agent transcript of this
    folder (or a folder above it) written in the last minutes, newest first. Picking a wrong one
    costs nothing but the mark: the quote must still stand in the person's own turns there."""
    from ..chats.sources import cursor

    slugs = set()
    path = os.path.abspath(folder)
    while path not in ("/", ""):
        slugs.add(contexts._slug(path))
        path = os.path.dirname(path)
    now, live = time.time(), []
    for p in glob.glob(os.path.join(cursor.PROJECTS, "*", "agent-transcripts", "*", "*.jsonl")):
        slug = p[len(cursor.PROJECTS) + 1 :].split(os.sep)[0]
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if slug in slugs and age < CURSOR_LIVE_SECONDS:
            live.append((age, p))
    for _, p in sorted(live)[:1]:
        s = cursor.load_transcript(p, errors=False)
        turns = [said.typed_text(m.text) for m in (s.messages if s else []) if m.role == "user"]
        return f"cursor:{os.path.basename(p)[:-6]}", [t for t in turns if t.strip()]
    return None


# --- writing the rule --------------------------------------------------------------------


def remember(
    store: NoteStore,
    text: str,
    folder: str,
    scope: str = "project",
    said_by: str = "developer",
    quote: str | None = None,
    chat: str | None = None,
    now: datetime | None = None,
    project=None,
) -> Remembered:
    """Write the rule into the notes now, as its own note, or confirm the active rule in its
    scope that already says it (merge by rule id). Takes no lock and does not commit: callers
    hold notes.writing and commit (see save). `project`: a chats.projects.Project to file a
    project rule under instead of the folder's (the memory eval's chats ran in folders gone since)."""
    text = said.clean_rule(" ".join(text.split()))
    if not text:
        raise Refused("the rule is empty")
    now = now or datetime.now()
    stamp, day = now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d")
    if scope != "project":
        project = None
    elif project is None:
        project = resolve(folder)
        if project is None:
            raise Refused(f"{folder} is not in a project; pass --global, or --project with the project's folder")
    key = project.key if project else None
    evidence = said.clean_rule(quote if quote is not None else text)[:200]
    notes = store.load()
    table = contexts.roots()
    here = contexts.of_folder(folder, table)
    same = next(
        (
            (n, r)
            for n in notes.values()
            if n.status == "active"
            and n.scope == scope
            and n.project == key
            # the same words said in another context are another rule: it must not move the first
            and (scope != "global" or contexts.of_folder(n.stated_in, table) == here)
            for r in n.active_rules
            if _same(r.text, text)
        ),
        None,
    )
    if same and said_by != "developer":
        # Only the person can confirm a rule: a run that cannot vouch for its words would move the
        # date the nightly run compares newer chats with, and count as the person's for it.
        note, rule = same
        return Remembered(note, rule, False, sorted(_placed(note)) if note.reach == "context" else [], changed=False)
    if same:
        note, rule = same
        rule.last_seen, rule.stated = max(rule.last_seen, day), stamp
        if said_by == "developer" and rule.said_by != "developer":
            rule.said_by, rule.evidence = "developer", evidence
        if chat and chat not in rule.sources:
            rule.sources = sorted({*rule.sources, chat})
        new = False
    else:
        title = _title(text)
        note = Note(
            id=new_id(title, salt=key or "global"),
            type="preference",
            scope=scope,
            title=title,
            body="",
            project=key,
            project_name=project.name if project else None,
            confidence="high" if said_by == "developer" else "low",
            created=day,
            updated=day,
        )
        taken = store.load(include_archived=True).keys() | notes.keys()  # an archived file keeps its id
        salt = 0
        while note.id in taken:
            salt += 1
            note.id = new_id(title, salt=f"{key or 'global'}{stamp}{salt}")
        rule = Rule(
            rule_id(note.id, text, set()),
            text,
            first_seen=day,
            last_seen=day,
            sources=[chat] if chat else [],
            said_by=said_by,
            evidence=evidence,
            stated=stamp,
        )
        note.rules = [rule]
        new = True
    if scope == "global":
        note.stated_in = os.path.normpath(os.path.abspath(folder))
        note.reach = "context" if table else note.reach  # one context only: the nightly judge may widen it
    note.updated = day
    sync(note)
    store.write(note)
    notes[note.id] = note
    where = sorted(_placed(note)) if note.scope == "global" and note.reach == "context" else []
    return Remembered(note, rule, new, where)


def save(store: NoteStore, done: Remembered, log=print) -> None:
    """After remember(): rewrite the indexes so the next session starts with the rule, commit the
    notes folder, refresh the search index, and leave the nightly run its candidate."""
    from ..chats import store as chats

    if not done.changed:
        return
    notes = store.load()
    store.reconcile(notes)
    store.compile(notes)
    sha = store.commit(f"remember: {done.note.title}")
    try:
        con = chats.connect()
        from . import context, extract

        context.index_notes(con, notes)
        extract.ensure_tables(con)
        candidate(con, done)
        con.commit()
        con.close()
    except Exception as e:  # the note is written; the search index catches up on its next refresh
        log(f"note saved; search index not refreshed ({e!r})")
    if sha:
        log(f"notes folder: commit {sha}")


def candidate(con, done: Remembered) -> None:
    """A row in the nightly run's candidates for the remembered rule. Its chat id names the rule
    (remember:<note id>:<rule id>), which tells consolidation what it is: the rule is already
    written, so the run only retires what it replaces and folds duplicates into it."""
    n, r = done.note, done.rule
    stamp = datetime.fromisoformat(r.stated).timestamp()
    con.execute("delete from candidates where session_id=? and status='new'", (pseudo_chat(n.id, r.id),))
    con.execute(
        """insert into candidates(session_id, project_key, project_name, scope, type, title, body, why,
           evidence, speaker, confidence, chat_ts, created) values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pseudo_chat(n.id, r.id),
            n.project,
            n.project_name,
            n.scope,
            n.type,
            n.title,
            r.text,
            "",
            r.evidence,
            "user" if r.said_by == "developer" else "assistant",
            "high" if r.said_by == "developer" else "low",
            stamp,
            stamp,
        ),
    )


PSEUDO = "remember:"


def pseudo_chat(note_id: str, rid: str) -> str:
    return f"{PSEUDO}{note_id}:{rid}"


def anchor(session_id: str) -> tuple[str, str] | None:
    """The (note id, rule id) a candidate from `recall remember` names, else None."""
    if not session_id.startswith(PSEUDO):
        return None
    note_id, _, rid = session_id[len(PSEUDO) :].rpartition(":")
    return (note_id, rid) if note_id and rid else None


def _placed(note: Note) -> set[str]:
    from .notes import placed

    return placed(note, contexts.roots())


def _title(text: str) -> str:
    """The rule's first sentence, cut at a word to fit a title."""
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".")
    if len(first) <= TITLE_CHARS:
        return clean_title(first)
    return clean_title(first[: TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…")


def _same(a: str, b: str) -> bool:
    return said._norm(a) == said._norm(b)


def stamp_of(rule: Rule) -> float | None:
    """When a remembered rule was stated, as a timestamp."""
    try:
        return datetime.fromisoformat(rule.stated).timestamp() if rule.stated else None
    except ValueError:
        return None

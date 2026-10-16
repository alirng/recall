"""Memory at the moment it matters: a note or past chat reaches the agent when the person's
prompt, the file it reads, or the command that just failed matches one, and nothing arrives
when recall is unsure.

Four moments, each a handler for one kind of hook event (integrations/hooks.py registers them;
`recall hook` calls them with an Event and sends back the Reply):

  on_prompt        the prompt matches notes or an earlier chat: at most two items, one or two
                   lines each, with the chat's id and date
  on_read          the agent reads a file: rules scoped to that file that this agent does not
                   load itself, notes that name the file or its folder, and the latest chat
                   that talked about it; once per file per session
  on_tool_failure  a command fails: past chats with the same error (the index's tool errors,
                   once it keeps them; until then only pasted copies, which rarely match), and
                   what came next there
  on_compact       the session was compacted: notes re-matched to the task in hand (notes
                   seldom clear the gate, so this rarely fires)

Abstaining is the default. A prompt match fires only when a logistic model over cheap features
(the keyword ranking's fused score and margin over the runner-up, the title and every-word
matches, same project, age, closeness in meaning to the earlier chat's opening prompt) gives
P(relevant) at or above a threshold. Meaning needs the prompt's embedding, which hooks take
only from a local model and wait EMBED_WAIT for; items without a vector use a keyword-only gate.
`recall eval jit` fits both on labelled pairs from a developer's history and picks thresholds
by Learn-then-Test with a Clopper-Pearson bound, so that at most 15% of injections are
irrelevant with 95% confidence; when the data cannot promise that, it takes the highest
threshold that fires on 20 pairs and says so (see GATES). File and error matches are
structural (the path or the error's words must appear), so they follow fixed rules.

Budgets: each item at most once per session, at most MAX_CHARS per injection, at most
WINDOW_CHARS and WINDOW_FIRES per context window (compaction starts a new one). Notes and
rules are framed as the developer's standing guidance, past chats as records. Every fire is
logged to the usage log with ids only, and `recall usage` sums them up.

Contexts (contexts.py): memory pushed into a session never crosses one. A global preference is
handed over only within the contexts it was learned in (_note_scope), a past chat only within
the context it ran in (_chat_scope). Searching chats (search_chats) is not narrowed.

Hooks must be fast: nothing here imports numpy, the llm package or mcp. No model calls. They
open the index read-only (_connect) and stay silent when it is missing or of another chat
schema, which the indexer rebuilds.
"""

import contextlib
import fnmatch
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import inject, paths

if TYPE_CHECKING:
    from .integrations.hooks import Event, Reply

MAX_ITEMS = 2  # per injection
MAX_CHARS = 1200  # per injection
LINE_CHARS = 300  # per item
WINDOW_CHARS = 6000  # per context window, all events together
WINDOW_FIRES = 10
MIN_WORDS = 3  # content words a prompt needs before it is matched at all
DENSE = ("cos_first", "cos_best", "has_vec")  # features that need the prompt's embedding
# Seconds a hook waits for the prompt's embedding before going without. A loaded model answers in
# 10-20 ms; a model that is not loaded is not waited for at all (_hook_vector).
EMBED_WAIT = 0.1
WARMED = os.path.join(paths.HOME, "cache", "embed-warm")  # when a hook last started a background load
# A hook's last embedding that arrived in time; within WARM_FOR of it (hooks ask Ollama to keep the
# model 30 minutes) the model is taken to be loaded without asking. A background load starts at most
# once per WARM_FOR too.
EMBEDDED = os.path.join(paths.HOME, "cache", "embed-ok")
WARM_FOR = 25 * 60
# Matching failures against the index's tool errors (tool-errors branch) stays off for now: in
# a time-ordered replay on a copy of that index, a judge found 54 of 90 matches relevant (0.60);
# the same error line often recurs with a different cause. A stricter rule comes with it.
TOOL_ERRORS = False
LOCK_WAIT = 0.2  # seconds a hook waits for its session's state before staying out
LIVE_SECONDS = 15 * 60  # a chat this fresh may still be running: the edit hooks cover it
STATE = os.path.join(paths.HOME, "cache", "jit")
GATE_FILE = os.path.join(paths.HOME, "jit_gate.json")  # a gate fitted on this machine's history

# P(relevant) = logistic(bias + Σ weight × feature), one gate for items with a stored vector to
# compare with the prompt's embedding ("dense") and one for the rest ("keyword"). Fitted by
# `recall eval jit` on 545 prompt and item pairs replayed from one developer's history and labelled
# by a judge model. Held-out AUC: dense 0.82 [0.75, 0.88] (140 pairs), keyword 0.77 [0.70, 0.83]
# (193). Neither can promise at most 15% false injections with 95% confidence on this much data, so
# each threshold is the highest that fires on 20 training pairs out of fold, a choice that sees no
# labels (4 and 5 of those 20 were false). Held out, the dense and keyword gates together fired 9
# times, 8 relevant; the keyword gate alone (embeddings off) 4 times, all relevant. Notes rarely
# pass. `recall eval jit --write` fits gates to your own history instead.
GATES = {
    "dense": {
        "bias": -12.2108,
        "weights": {
            "score": 5.0501,
            "margin": -0.9907,
            "bm25": 0.0783,
            "kw": 0.7444,
            "title": 0.3975,
            "all": -0.3066,
            "project": 0.7306,
            "age": -0.1455,
            "words": -0.1645,
            "lead": 0.0922,
            "note": 0.0515,
            "matches": 0.2096,
            "file": -0.0630,
            "cos_first": 7.8066,
            "cos_best": 2.2821,
            "has_vec": 0.0000,
        },
        "threshold": 0.9342,
    },
    "keyword": {
        "bias": -6.9297,
        "weights": {
            "score": 5.0458,
            "margin": -2.1948,
            "bm25": 0.3652,
            "kw": 0.5926,
            "title": 1.1676,
            "all": -0.2639,
            "project": 0.7433,
            "age": -0.2390,
            "words": 0.2263,
            "lead": -0.0439,
            "note": -0.7068,
            "matches": 0.2304,
            "file": 0.1536,
        },
        "threshold": 0.9131,
    },
}


# Files whose name says nothing on its own: matched by their repo path only.
COMMON_NAMES = {
    "readme.md", "changelog.md", "agents.md", "claude.md", "license", "package.json", "package-lock.json",
    "pyproject.toml", "setup.py", "cargo.toml", "go.mod", "tsconfig.json", "makefile", "dockerfile",
    "__init__.py", "__main__.py", "main.py", "index.ts", "index.tsx", "index.js", "app.tsx", "app.ts",
    "app.js", "utils.py", "utils.ts", "types.ts", "config.py", "config.ts", ".env", ".gitignore", "uv.lock",
}  # fmt: skip
NOISE = re.compile(r"(^|/)(node_modules|\.git|\.venv|venv|dist|build|__pycache__|\.next|DerivedData)/")


# --- what one injection carries -------------------------------------------------------------


@dataclass
class Item:
    id: str  # chat id, note id, or rule:<path>
    kind: str  # chat | note | rule
    line: str
    p: float | None = None  # the gate's P(relevant), for prompt and compact matches
    features: dict = field(default_factory=dict)
    passed: bool = True  # cleared the gate (always, for file and error matches)


class State:
    """What one session (or one subagent in it) has been shown: item ids, files and error
    signatures already handled, and the context window's running totals. One small JSON file
    per session, held under an exclusive lock from load to save, since an agent's parallel
    tool calls fire hooks at once, and replaced atomically, so a reader never sees half a file.

        with State.of(event) as state:
            ...  state.d["items"] ...
    """

    def __init__(self, agent: str, session: str, sub: str | None = None):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "-".join(x for x in (agent, session, sub) if x))[:150]
        self.path = os.path.join(STATE, safe + ".json")
        self.d: dict = {"items": [], "files": [], "errors": [], "chars": 0, "fires": 0}
        self._lock = None
        self.busy = False

    @classmethod
    def of(cls, e: "Event") -> "State":
        """A subagent's tool events carry the main session's id plus its own agent_id."""
        return cls(e.agent, e.session or "", (e.raw or {}).get("agent_id"))

    def __enter__(self) -> "State":
        """Lock, then load. If another hook of this session holds the lock past LOCK_WAIT,
        `busy` is set and this hook stays out (it injects nothing and saves nothing)."""
        import fcntl

        os.makedirs(STATE, exist_ok=True)
        self._lock = open(self.path + ".lock", "a")
        os.utime(self.path + ".lock")  # in use: the week-old sweep leaves it alone
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    self.busy = True
                    return self
                time.sleep(0.01)
        try:
            with open(self.path) as f:
                self.d = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.d = {}
            _sweep()
        for key in ("items", "files", "errors"):
            self.d.setdefault(key, [])
        self.d.setdefault("chars", 0)
        self.d.setdefault("fires", 0)
        return self

    def __exit__(self, *exc) -> None:
        if self.busy:
            self._lock.close()
            return
        try:
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.d, f)
            os.replace(tmp, self.path)
        except OSError:
            pass
        finally:
            self._lock.close()  # releases the lock

    def new_window(self) -> None:
        """After compaction the earlier injections are summarised away: allow them again."""
        self.d.update(items=[], files=[], errors=[], chars=0, fires=0)


def _repo_path(path: str, cwd: str | None) -> str:
    """A file as the usage log names it: relative to its repo, never the whole path."""
    from . import rules

    full = os.path.abspath(os.path.join(cwd or "", path))
    root = rules.repo_root(os.path.dirname(full)) or rules.repo_root(cwd)
    return os.path.relpath(full, root) if root and full.startswith(root + os.sep) else os.path.basename(full)


def _sweep(days: int = 7) -> None:
    """Drop the state and lock files of sessions idle for a week."""
    try:
        cutoff = time.time() - days * 86400
        for name in os.listdir(STATE):
            path = os.path.join(STATE, name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


# --- handlers ---------------------------------------------------------------------------------


def on_prompt(e: "Event") -> "Reply | None":
    if not e.session or e.observe:
        return None
    with State.of(e) as state:
        if state.busy:
            return None
        return _reply(e, state, "prompt", _prompt_items(e, state, e.prompt or ""))


def on_read(e: "Event") -> "Reply | None":
    if not e.session or not e.files:
        return None
    with State.of(e) as state:
        if state.busy:
            return None
        items = _after_compaction(e, state)
        con = None
        for path in e.files[:3]:
            path = os.path.abspath(os.path.join(e.cwd or "", path))
            if path in state.d["files"]:
                continue
            con = con or _connect()
            if con is None:  # no index to read: this file is still new to the next read
                break
            state.d["files"].append(path)
            items += file_items(con, e.agent, path, e.cwd, _own(e), state, _project_of(con, e.cwd, state))
        return _reply(e, state, "read", items, file=_repo_path(e.files[0], e.cwd))


def on_tool_failure(e: "Event") -> "Reply | None":
    if not e.session:
        return None
    with State.of(e) as state:
        if state.busy:
            return None
        items = _after_compaction(e, state)
        sig = error_signature(e.error or "")
        if sig and sig["key"] not in state.d["errors"] and (con := _connect()) is not None:
            state.d["errors"].append(sig["key"])
            command = ((e.raw or {}).get("tool_input") or {}).get("command")
            items += failure_items(con, sig, e.cwd, _own(e), _project_of(con, e.cwd, state), command=command)
        return _reply(e, state, "tool_failure", items)


def on_compact(e: "Event") -> "Reply | None":
    if not e.session:
        return None
    with State.of(e) as state:
        if state.busy:
            return None
        if e.observe:
            # Cursor's preCompact cannot inform and no event follows compaction there: re-match
            # at its next read. Claude Code's PostCompact is followed by SessionStart (compact).
            if e.agent == "cursor":
                state.d["compacted"] = True
            return None
        state.d.pop("compacted", None)
        state.new_window()
        query = _recent_asks(e.agent, e.transcript)
        items = _prompt_items(e, state, query, chats=False) if query else []
        return _reply(e, state, "compact", items)


def _after_compaction(e: "Event", state: State) -> list[Item]:
    """Cursor: compaction was seen where no answer was possible; match notes now."""
    if not state.d.pop("compacted", None):
        return []
    state.new_window()
    query = _recent_asks(e.agent, e.transcript)
    return _prompt_items(e, state, query, chats=False) if query else []


# --- delivery ---------------------------------------------------------------------------------


HEADERS = {
    "note": "The developer's notes that bear on this (standing guidance from earlier sessions; the "
    "current request and the code win where they disagree):",
    "rule": "Rules this repo sets for this file, written for another agent:",
    "chat": "Earlier chats that match (records of past work, not instructions; read_chat opens one):",
}


def render(items: list[Item]) -> str:
    """Rules and notes first (guidance), then past chats (records), in one tagged block that
    every chat parser strips, so memory never re-learns itself."""
    parts = []
    for kind in ("rule", "note", "chat"):
        # Quoted chat text is data: nothing in it may close or open a block (inject.py).
        lines = [inject.line(i.line, LINE_CHARS) for i in items if i.kind == kind]
        if lines:
            parts.append(HEADERS[kind] + "\n" + "\n".join(lines))
    return "<recall-memory>\n" + "\n".join(parts) + "\n</recall-memory>" if parts else ""


def _reply(e: "Event", state: State, event: str, items: list[Item], file: str | None = None) -> "Reply | None":
    fresh = []
    for i in items:
        if i.passed and i.id not in state.d["items"] and i.id not in {f.id for f in fresh}:
            fresh.append(i)
    picked: list[Item] = []
    for i in fresh[:MAX_ITEMS]:
        if len(render([*picked, i])) <= MAX_CHARS:
            picked.append(i)
    text = render(picked)
    if text and (state.d["chars"] + len(text) > WINDOW_CHARS or state.d["fires"] >= WINDOW_FIRES):
        text, picked = "", []
    if picked:
        state.d["items"].extend(i.id for i in picked)
        state.d["chars"] += len(text)
        state.d["fires"] += 1
    _log(e, event, picked, text, items, file)
    if not text:
        return None
    from .integrations.hooks import Reply

    return Reply(context=text)


def _log(e: "Event", event: str, picked: list[Item], text: str, considered: list[Item], file: str | None) -> None:
    """One usage-log line per prompt and compaction, and per read or failure that fired: ids,
    sizes and the gate's top score, never text. A read names its file relative to the repo, so
    `recall eval jit --live` can judge what was injected beside it."""
    from . import usage

    top = max((i.p for i in considered if i.p is not None), default=None)
    if not picked and event not in ("prompt", "compact"):
        return  # file reads and failures that match nothing are too many to log
    usage.record(
        "hook",
        "jit",
        {"event": event, "file": file if picked else None},
        None,
        client=e.agent,
        session=e.session,
        agent_id=(e.raw or {}).get("agent_id"),  # a subagent's, apart from its session's
        ids=[i.id for i in picked] or None,
        kinds=[i.kind for i in picked] or None,
        chars=len(text) if text else 0,
        p=round(top, 3) if top is not None else None,
    )


# --- prompt matching --------------------------------------------------------------------------


def _prompt_items(e: "Event", state: State, prompt: str, chats: bool = True) -> list[Item]:
    if not askable(prompt):
        return []
    if (con := _connect()) is None:
        return []
    project = _project_of(con, e.cwd, state)
    return match_prompt(con, prompt, e.cwd, _own(e), chats=chats, project=project, keep_all=True)


def match_prompt(
    con, prompt: str, cwd: str | None, own: list[str] | None = None, gate: dict | None = None, chats: bool = True,
    keep_all: bool = False, now: float | None = None, project: dict | bool | None = False,
) -> list[Item]:  # fmt: skip
    """The items a prompt matches that pass the gate (all of them, scored, with keep_all),
    best first."""
    query = askable(prompt)
    if not query:
        return []
    gates = gate or load_gates()
    vector = None
    if local_embeddings():  # embed the prompt while the keyword search runs, waiting EMBED_WAIT at most
        import threading

        box: dict = {}
        worker = threading.Thread(target=lambda: box.update(v=_hook_vector(query)), daemon=True)
        started = time.perf_counter()
        worker.start()

        def vector():
            worker.join(max(0.0, EMBED_WAIT - (time.perf_counter() - started)))
            if worker.is_alive():  # too late: the next prompt asks Ollama first whether it is loaded
                with contextlib.suppress(OSError):
                    os.remove(EMBEDDED)
            return box.get("v")

    found = candidates(con, query, cwd, own=own, chats=chats, now=now, project=project, vector=vector)
    for c in found:
        # The meaning gate where this item has a stored vector to compare, the keyword gate else.
        g = gates["dense"] if c.features.get("has_vec") else gates["keyword"]
        c.p = probability(c.features, g)
        c.passed = c.p >= g["threshold"]
    found.sort(key=lambda c: -c.p)
    return found if keep_all else [c for c in found if c.passed]


def askable(prompt: str) -> str | None:
    """The prompt as a query, or None for what should never trigger a match: harness and
    other-session messages (they arrive as tagged text), slash commands, and short replies."""
    from .chats.transcript import clean_user_text

    if not prompt or prompt.lstrip().startswith(("<", "/")):
        return None
    text = clean_user_text(prompt).strip()
    return text[:2000] if len(content_words(text)) >= MIN_WORDS else None


def q_terms(query: str) -> int:
    """How many words the keyword search ORs together for this query."""
    from .chats.store import fts_query

    q = fts_query(query, "or")
    return q.count(" OR ") + 1 if q else 0


def content_words(text: str) -> list[str]:
    from .chats.store import STOPWORDS

    return [w for w in dict.fromkeys(re.findall(r"\w+", text.lower())) if w not in STOPWORDS and len(w) > 1]


def candidates(
    con,
    query: str,
    cwd: str | None,
    own: list[str] | None = None,
    until: float | None = None,
    now: float | None = None,
    vector=None,
    chats: bool = True,
    notes: bool = True,
    allowed_notes: set[str] | None = None,
    project: dict | bool | None = False,
) -> list[Item]:
    """The best two earlier chats and the best two notes for a query, each with the gate's
    features. until / now / allowed_notes let the labelled-pairs builder replay the moment a
    past prompt was typed: only chats started before it, and notes that existed then."""
    now = now or time.time()
    words = math.log(1 + len(content_words(query)))
    if project is False:  # not looked up yet
        project = _project(con, cwd)
    out = []
    if chats:
        out += _chat_candidates(con, query, project, own, until, now, words, cwd)
    if notes:
        out += _note_candidates(con, query, project, now, words, allowed_notes, cwd)
    named = files_named(query)
    if callable(vector):  # the prompt's embedding, waited for only when an item has vectors to compare it with
        vector = vector() if any(_has_vectors(con, c, until) for c in out) else None
    for c in out:
        c.features |= _meaning(con, c, vector, until) | {"file": float(_names_file(con, c, named, until))}
    return out


def files_named(text: str) -> list[str]:
    """File names a prompt mentions (`@CartView.swift`, src/billing/invoice.ts), when distinctive."""
    found = re.findall(r"[\w@./-]*?([A-Za-z_][\w-]{2,}\.[A-Za-z]{1,6})\b", text)
    return [
        f
        for f in dict.fromkeys(found)
        if f.lower() not in COMMON_NAMES and not re.match(r"^\w+\.(com|org|io|ai|co|uk)$", f)
    ][:5]


def _names_file(con, c: Item, names: list[str], until: float | None) -> bool:
    """Whether the chat (before `until`) or the note names one of the files the prompt does."""
    if not names:
        return False
    match = " or ".join(["instr(text, ?) > 0"] * len(names))
    if c.kind == "note":
        row = con.execute(
            f"select 1 from notes where id = ? and ({match.replace('text', 'body')}) limit 1", [c.id, *names]
        )
    else:
        when = "and (ts is null or ts < ?)" if until else ""
        row = con.execute(
            f"select 1 from messages where session_id = ? {when} and ({match}) limit 1",
            [c.id, *([until] if until else []), *names],
        )
    return row.fetchone() is not None


def _has_vectors(con, c: Item, until: float | None) -> bool:
    """Whether _meaning would find stored vectors for this item, without the prompt's own."""
    model = vector_model()
    try:
        if c.kind == "note":
            sql, args = "select 1 from note_vectors where note_id = ? and model = ? limit 1", [c.id, model]
        else:
            when = "and (m.ts is null or m.ts < ?)" if until else ""
            sql = f"""select 1 from vectors v join messages m on m.session_id = v.session_id and m.seq = v.seq
                      where v.session_id = ? and v.model = ? {when} limit 1"""
            args = [c.id, model, *([until] if until else [])]
        return con.execute(sql, args).fetchone() is not None
    except Exception:  # no vector tables yet: _meaning finds none either
        return False


def _meaning(con, c: Item, vector, until: float | None) -> dict:
    """Closeness in meaning of the prompt to the chat's opening message and to its closest
    message, or to the note, from vectors already stored. Zero without a query vector."""
    if not vector:
        return {"cos_first": 0.0, "cos_best": 0.0, "has_vec": 0.0}
    model = vector_model()
    if c.kind == "note":
        try:
            row = con.execute("select vec from note_vectors where note_id = ? and model = ?", (c.id, model)).fetchone()
        except Exception:
            row = None
        cos = cosine(vector, row[0]) if row else 0.0
        return {"cos_first": cos, "cos_best": cos, "has_vec": float(row is not None)}
    when = "and (m.ts is null or m.ts < ?)" if until else ""
    try:
        rows = con.execute(
            f"""select v.seq, v.vec from vectors v join messages m on m.session_id = v.session_id and m.seq = v.seq
                where v.session_id = ? and v.model = ? {when} order by v.seq limit 400""",
            [c.id, model, *([until] if until else [])],
        ).fetchall()
    except Exception:
        rows = []
    sims = [(seq, cosine(vector, blob)) for seq, blob in rows]
    return {
        "cos_first": next((x for seq, x in sims if seq == 0), 0.0),
        "cos_best": max((x for _, x in sims), default=0.0),
        "has_vec": float(bool(sims)),
    }


def _embedding_settings() -> tuple[dict, str, str, str]:
    """[embeddings] from config.toml: (settings, provider, model name, base URL)."""
    from . import config

    settings = config.load().get("embeddings") or {}
    provider, name = settings.get("provider", "ollama"), settings.get("name") or "nomic-embed-text"
    base = (settings.get("base_url") or ("http://localhost:11434" if provider == "ollama" else "")).rstrip("/")
    return settings, provider, name, base


def local_embeddings() -> bool:
    """Whether the prompt may be embedded in a hook: embeddings are on and the model runs on
    this machine. A hosted model would receive every prompt, so hooks never call one."""
    from urllib.parse import urlparse

    settings, _, _, base = _embedding_settings()
    if not (settings.get("provider") or settings.get("name")) or not base:
        return False
    return urlparse(base).hostname in ("localhost", "127.0.0.1", "::1")


def vector_model() -> str:
    """The model name stored vectors are keyed by, as embeddings.Embedder.model has it."""
    _, provider, name, _ = _embedding_settings()
    return f"{provider}:{name}"


def _hook_vector(text: str) -> list[float] | None:
    """The prompt's embedding for a hook. Ollama is asked first whether the model is loaded (under a
    millisecond): loading takes longer than a hook waits, and Ollama drops a load whose request was
    given up on, so asking a cold model would cost every prompt the whole wait and never warm it.
    A model that is not loaded is loaded in the background for the next prompts instead, once per
    WARM_FOR: after that, a model Ollama still does not list (its name there may be another form of
    the configured one) is asked for as before. While the hooks' embeddings keep arriving in time
    (EMBEDDED), Ollama is not asked."""
    _, provider, name, base = _embedding_settings()
    if provider == "ollama" and not _lately(EMBEDDED) and not _lately(WARMED) and not _ollama_loaded(base, name):
        _warm_up(base, name)
        return None
    vector = embed_query(text)
    if vector is not None:
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(EMBEDDED), exist_ok=True)
            with open(EMBEDDED, "w"):
                pass
    return vector


def _lately(marker: str) -> bool:
    try:
        return time.time() - os.path.getmtime(marker) < WARM_FOR
    except OSError:
        return False


def _ollama_loaded(base: str, name: str) -> bool:
    """Whether Ollama has the model loaded (/api/ps). False only when Ollama says it has not: when
    it cannot tell, or is not running, the hook asks for the embedding as before."""
    import http.client
    from urllib.parse import urlparse

    url = urlparse(base)
    try:
        kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = kind(url.hostname, url.port, timeout=EMBED_WAIT)
        conn.request("GET", url.path + "/api/ps")
        reply = conn.getresponse()
        models = json.loads(reply.read())["models"] if reply.status == 200 else None
    except Exception:
        return True
    if not isinstance(models, list):
        return True

    def plain(n):  # "nomic-embed-text" and "nomic-embed-text:latest" are one model
        return n.removesuffix(":latest") if isinstance(n, str) else n

    return any(isinstance(m, dict) and plain(name) in {plain(m.get("name")), plain(m.get("model"))} for m in models)


def _warm_up(base: str, name: str) -> None:
    """Load the embedding model for the prompts to come, in a process of its own that waits for it.
    The hook that starts it does not wait."""
    import subprocess
    import sys

    code = (
        "import http.client, json, sys; from urllib.parse import urlparse; u = urlparse(sys.argv[1]);"
        " c = (http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection)"
        "(u.hostname, u.port, timeout=120);"
        " c.request('POST', u.path + '/api/embed', json.dumps({'model': sys.argv[2], 'input': ['warm up'],"
        " 'keep_alive': '30m'}), {'Content-Type': 'application/json'}); c.getresponse().read()"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-I", "-c", code, base, name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.makedirs(os.path.dirname(WARMED), exist_ok=True)
        with open(WARMED, "w"):
            pass
    except OSError:
        pass


def embed_query(text: str, timeout: float = EMBED_WAIT) -> list[float] | None:
    """The prompt's unit vector from the embedding model in config.toml, or None. Hooks call it
    only when local_embeddings() says the model runs on this machine. This repeats
    embeddings.Embedder.embed for one query because importing that path pulls in numpy and the
    llm package, about 100 ms each in a cold hook process."""
    import http.client
    from urllib.parse import urlparse

    from . import keys
    from .embeddings import PREFIXES

    settings, provider, name, base = _embedding_settings()
    if not (settings.get("provider") or settings.get("name")) or not base:
        return None
    prefix = settings.get("query_prefix", PREFIXES.get(name.split(":")[0], ("", ""))[0])
    url = urlparse(base)
    headers = {"Content-Type": "application/json"}
    if provider == "ollama":
        path, body = url.path + "/api/embed", {"model": name, "input": [prefix + text[:2000]], "keep_alive": "30m"}
    else:
        key = keys.load(settings.get("api_key"), f"embeddings-{provider}")
        headers |= {"Authorization": f"Bearer {key}"} if key else {}
        path, body = url.path + "/embeddings", {"model": name, "input": [prefix + text[:2000]]}
    try:
        kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = kind(url.hostname, url.port, timeout=timeout)
        conn.request("POST", path, json.dumps(body), headers)
        reply = json.loads(conn.getresponse().read())
        v = reply["embeddings"][0] if provider == "ollama" else reply["data"][0]["embedding"]
    except Exception:
        return None
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _chat_candidates(con, query, project, own, until, now, words, cwd=None) -> list[Item]:
    from .chats import store

    explain: dict = {}
    until_iso = _iso(until) if until else None
    # Keyword ranking only: the full meaning ranking loads every vector (numpy, ~300 ms cold);
    # the query vector instead scores the candidates (_meaning). Chats of another context are
    # out before the ranking, so they cannot take a place or lower a margin.
    rows = store.search(
        con, query, limit=8, until=until_iso, exclude=own, vector=False, explain=explain, where=_chat_scope(con, cwd)
    )
    if not rows:
        return []
    meta = {
        r["id"]: r
        for r in con.execute(
            f"select id, origin, project_key, project, started_at, updated_at, n_messages from sessions "
            f"where id in ({','.join('?' * len(rows))})",
            [r["chat_id"] for r in rows],
        )
    }
    # Only chats a person drove: headless runs (evals, recall's own model calls) hold copies.
    keep = [r for r in rows if meta[r["chat_id"]]["origin"] == "interactive"]
    if until:
        keep = [r for r in keep if (meta[r["chat_id"]]["started_at"] or 0) < until]
    order = [r["chat_id"] for r in keep]
    terms = max(1, q_terms(query))
    out = []
    for i, r in enumerate(keep[:2]):
        m = meta[r["chat_id"]]
        last = m["updated_at"] or now
        if until:  # replaying a past prompt: the chat's last message before it
            row = con.execute("select max(ts) from messages where session_id = ? and ts < ?", (r["chat_id"], until))
            last = row.fetchone()[0] or m["started_at"] or now
        f = rank_features(r["chat_id"], order, explain)
        f |= {
            "bm25": -explain.get("bm25", {}).get(r["chat_id"], 0.0) / terms,
            "project": float(project is not None and _same_project(project, m["project_key"])),
            "age": math.log1p(max(0.0, now - last) / 86400),
            "words": words,
            "lead": float(i == 0),
            "note": 0.0,
            "matches": math.log1p(r.get("matches") or 0),
        }
        out.append(Item(r["chat_id"], "chat", _chat_line(con, r, m), features=f))
    return out


def rank_features(cid: str, order: list[str], explain: dict) -> dict:
    """How sure the fused ranking is about one item: its score as a share of the best possible,
    its lead over the next one, and where the keyword, title and every-word rankings put it."""
    from .chats.store import FUSION_K

    scores, rankings, weights = explain["scores"], explain["rankings"], explain["weights"]
    active = [n for n, r in rankings.items() if r]
    best = sum(weights[n] for n in active) / (FUSION_K + 1) or 1.0
    i = order.index(cid)
    nxt = scores.get(order[i + 1], 0.0) if i + 1 < len(order) else 0.0

    def rank(name: str) -> int | None:
        r = rankings.get(name) or []
        return r.index(cid) if cid in r[:50] else None

    kw = [x for x in (rank("best"), rank("sum")) if x is not None]
    kw_rank = min(kw) if kw else None
    title, every = rank("title"), rank("all")
    return {
        "score": scores[cid] / best,
        "margin": (scores[cid] - nxt) / best,
        "kw": 1 / (1 + kw_rank) if kw_rank is not None else 0.0,
        "title": float(title is not None and title < 3),
        "all": float(every is not None and every < 3),
    }


def _note_scope(con, project: dict | None, cwd: str | None) -> tuple[str, list]:
    """SQL (with its parameters, in order) for the notes a session in `cwd` may be handed: the
    current project's own, and global ones whose audience (memory/notes.py) includes it. A
    preference learned in one context is not handed to another's sessions: work and personal
    stay apart as at session start. Notes with no audience reach every folder.

    The audiences were worked out when the notes were indexed, against the contexts configured
    then (contexts.fingerprint). With contexts configured, an index that cannot say which, or
    says other than the config now, hands over no global note until the notes are indexed again
    (the next MCP server start or nightly run): that covers an index written before audiences
    existed, a config.toml edited since, and one that could not be read while indexing, whose
    notes all came out with no audience. A config.toml that cannot be read now counts the same."""
    from . import contexts

    keys = project["keys"] if project else []
    sql, args = "n.scope = 'global'", []
    try:
        table, now = contexts.read()
    except Exception:
        table, now = {}, contexts.UNKNOWN
    audiences = any(r[1] == "audience" for r in con.execute("pragma table_info(notes)"))
    if now != contexts.NONE and not (audiences and contexts.audiences_current(con, now)):
        sql = "0"
    elif audiences:
        try:
            here = contexts.of_folder(cwd, table)
        except Exception:
            here = None
        who = [f"project:{k}" for k in keys] + ([f"ctx:{here}"] if here else [])
        sql += " and (n.audience is null" + " or instr(n.audience, ?) > 0" * len(who) + ")"
        args = [json.dumps(w) for w in who]
    if keys:
        sql = f"({sql}) or n.project in ({','.join('?' * len(keys))})"
        args += keys
    return sql, args


def _chat_scope(con, cwd: str | None) -> tuple[str, list]:
    """SQL (starting with "and", over `sessions s`) and its parameters for the chats a session in
    `cwd` may be handed. Memory pushed into a session never crosses a context: with contexts
    configured, a chat is eligible only when its folder is in the same context as `cwd`, and a
    chat outside every context only for a session that is too. Searching chats (the search_chats
    tool) is not narrowed: it answers a person's question, and they asked. With no contexts
    nothing is narrowed; a config.toml that cannot be read now leaves no chat eligible."""
    import functools

    from . import contexts

    try:
        table, now = contexts.read()
        if now == contexts.NONE:
            return "", []
        if now == contexts.UNKNOWN:
            return " and 0", []
        here = contexts.of_folder(cwd, table) or ""
    except Exception:
        return " and 0", []

    @functools.cache
    def context_of(folder: str | None) -> str:
        try:
            return contexts.of_folder(folder, table) or ""
        except Exception:
            return "\0"  # in no context that a session can be in

    con.create_function("recall_context", 1, context_of, deterministic=True)
    return " and recall_context(s.project) = ?", [here]


def _note_candidates(con, query, project, now, words, allowed, cwd=None) -> list[Item]:
    """Notes for the current project and global ones, ranked by keywords and title words, with
    the same features as chats."""
    from .chats import store

    q = store.fts_query(query, "or")
    if not q:
        return []
    try:
        where, args = _note_scope(con, project, cwd)
        rows = con.execute(
            f"""select n.id, n.title, n.brief, n.project, n.scope, n.last_seen, n.type,
                       bm25(notes_fts, 0, 5.0, 1.0, 2.0) s
                from notes_fts f join notes n on n.id = f.id
                where notes_fts match ? and ({where}) and n.brief != ''
                order by s limit 30""",
            [q, *args],
        ).fetchall()
    except Exception:  # no notes index yet
        return []
    if allowed is not None:
        rows = [r for r in rows if r[0] in allowed]
    if not rows:
        return []
    by_kw = [r[0] for r in rows]
    by_title = [r[0] for r in rows if set(content_words(r[1])) & set(content_words(query))]
    rankings = {"best": by_kw, "title": by_title}
    weights = {"best": 1.0, "title": 1.0}
    scores: dict[str, float] = {}
    for name, ranking in rankings.items():
        for rank, nid in enumerate(ranking):
            scores[nid] = scores.get(nid, 0) + weights[name] / (store.FUSION_K + rank + 1)
    order = sorted(scores, key=scores.get, reverse=True)
    explain = {"scores": scores, "rankings": rankings, "weights": weights}
    info = {r[0]: r for r in rows}
    out = []
    for i, nid in enumerate(order[:2]):
        r = info[nid]
        f = rank_features(nid, order, explain)
        f |= {
            "bm25": -r[7] / max(1, q_terms(query)),
            "project": float(r[4] == "project"),
            "age": math.log1p(max(0.0, now - _ts(r[5], now)) / 86400),
            "words": words,
            "lead": float(i == 0),
            "note": 1.0,
            "matches": 0.0,
        }
        out.append(Item(nid, "note", _note_line(r[0], r[1], r[2], r[5]), features=f))
    return out


def probability(features: dict, gate: dict) -> float:
    z = gate["bias"] + sum(w * features.get(k, 0.0) for k, w in gate["weights"].items())
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def load_gates() -> dict:
    """The gates fitted on this machine's history (`recall eval jit --write`), else the shipped
    ones: {"dense": for items with a stored vector, "keyword": for the rest}. A file holding
    one gate uses it for both."""

    def one(g) -> dict | None:
        if isinstance(g, dict) and isinstance(g.get("weights"), dict) and "threshold" in g:
            return {"bias": float(g.get("bias", 0)), "weights": g["weights"], "threshold": float(g["threshold"])}
        return None

    try:
        with open(GATE_FILE) as f:
            stored = json.load(f)
    except (OSError, ValueError):
        stored = {}
    if single := one(stored):
        return {"dense": single, "keyword": single}
    return {k: one(stored.get(k)) or GATES[k] for k in GATES} if isinstance(stored, dict) else GATES


# --- files ------------------------------------------------------------------------------------


def file_items(
    con, agent: str, path: str, cwd: str | None, own: list[str], state: State | None = None, project=False
) -> list[Item]:
    """What is anchored to a file: rules scoped to it that this agent does not load itself,
    notes that name it or its folder, and the latest chat in this project that worked on it."""
    from . import rules

    root = rules.repo_root(os.path.dirname(path)) or rules.repo_root(cwd)
    rel = os.path.relpath(path, root) if root and path.startswith(root + os.sep) else None
    if NOISE.search("/" + (rel or path)):
        return []
    items = _file_rules(agent, root, rel, state) if rel else []
    names = _anchors(path, rel)
    if not names:
        return items
    if project is False:
        project = _project(con, cwd or root)
    items += _file_notes(con, names, project, cwd or root)
    if project and rel:
        items += _file_chats(con, names, project, own, cwd or root)
    return items


def _anchors(path: str, rel: str | None) -> list[str]:
    """The strings that name this file in a note or chat: its repo path, its name when the name
    is distinctive, and its folder when the folder is at least two levels deep."""
    base = os.path.basename(path)
    out = []
    if rel and "/" in rel:
        out.append(rel)
    if base.lower() not in COMMON_NAMES and len(os.path.splitext(base)[0]) >= 4:
        out.append(base)
    folder = os.path.dirname(rel) if rel else ""
    if folder.count("/") >= 1:
        out.append(folder + "/")
    return out


def _file_rules(agent: str, root: str, rel: str, state: State | None) -> list[Item]:
    from . import rules

    key = f"rules:{agent}:{root}"
    scoped = state.d.get(key) if state else None
    if scoped is None:
        try:
            repo = rules.scan(root)
        except Exception:
            return []
        scoped = [
            [s.path, s.globs, s.description or rules._first_line(s.body)]
            for s in repo.sources
            if s.apply == "files" and agent not in s.seen_by and s.globs
        ]
        if state:
            state.d[key] = scoped
    out = []
    for path, globs, what in scoped:
        patterns = [g.strip() for g in re.split(r"[,\s]+", globs) if g.strip()]
        if any(_glob(rel, g) for g in patterns):
            where = inject.line(path.replace("`", ""), 120)  # the rule file's own words: capped like them
            line = f"- {_clip(what.rstrip('.'), 200)} (`{where}`, for {inject.line(globs, 80)})"
            out.append(Item(f"rule:{path}", "rule", line))
    return out


def _glob(rel: str, pattern: str) -> bool:
    pattern = pattern.strip("'\"")
    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.replace("**/", "")):
        return True
    return "/" not in pattern and fnmatch.fnmatch(os.path.basename(rel), pattern)


def _file_notes(con, names: list[str], project: dict | None, cwd: str | None) -> list[Item]:
    match = " or ".join(["instr(n.body, ?) > 0 or instr(n.title, ?) > 0"] * len(names))
    try:
        scope, args = _note_scope(con, project, cwd)
        rows = con.execute(
            f"""select n.id, n.title, n.brief, n.last_seen from notes n
                where ({scope}) and ({match}) and n.brief != '' order by n.last_seen desc limit 2""",
            [*args, *[x for name in names for x in (name, name)]],
        ).fetchall()
    except Exception:
        return []
    return [Item(r[0], "note", _note_line(r[0], r[1], r[2], r[3])) for r in rows]


def _file_chats(con, names: list[str], project: dict, own: list[str], cwd: str | None = None) -> list[Item]:
    """The latest finished chat in this project whose agent named the file (its path, or its
    name when distinctive) in a reply, with what that reply said about it. Chats active in the
    last quarter hour are left to the edit hooks (status.py), which know what is live."""
    files = [n for n in names if not n.endswith("/")]
    phrase = " ".join(re.findall(r"\w+", os.path.splitext(os.path.basename(files[0]))[0])) if files else ""
    if not phrase:
        return []
    keys = project["keys"]
    scope, scope_args = _chat_scope(con, cwd)  # a project listed in projects.yaml can span contexts
    rows = con.execute(
        f"""select m.session_id sid, max(m.seq) seq, s.title, s.updated_at
            from messages_fts f join messages m on m.id = f.rowid join sessions s on s.id = m.session_id
            where messages_fts match ? and m.role = 'assistant' and s.origin = 'interactive'
              and s.project_key in ({",".join("?" * len(keys))}) and s.updated_at < ?
              and ({" or ".join(["instr(m.text, ?) > 0"] * len(files))})
              {f"and s.id not in ({','.join('?' * len(own))})" if own else ""}
              {scope}
            group by m.session_id order by s.updated_at desc limit 1""",
        [f'"{phrase}"', *keys, time.time() - LIVE_SECONDS, *files, *own, *scope_args],
    ).fetchall()
    out = []
    for r in rows:
        text = con.execute(
            "select text from messages where session_id = ? and seq = ?", (r["sid"], r["seq"])
        ).fetchone()
        said = _around(text[0] if text else "", files)
        title = _clip(r["title"] or "", 60)
        line = f'- {_day(r["updated_at"])}, {_short(r["sid"])} "{title}" said about it: {said}'
        out.append(Item(r["sid"], "chat", _clip(line, LINE_CHARS)))
    return out


def _around(text: str, names: list[str], n: int = 170) -> str:
    """The sentence of a message that names one of these files."""
    flat = " ".join(re.sub(r"```.*?```", " ", text, flags=re.S).split())
    at = min((i for i in (flat.find(x) for x in names) if i >= 0), default=-1)
    if at < 0:
        return _gist(text, n)
    start = max(flat.rfind(". ", 0, at) + 2, at - n // 2, 0)
    return _clip(re.sub(r"[*_`]+", "", flat[start:]), n)


# --- failed commands ----------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ERROR_LINE = [  # most telling first; prose that merely says "failed" or "not found" is not one
    re.compile(r"\b[A-Z]\w*(Error|Exception)\b\s*[:(]"),
    re.compile(r"(^|\s)(error|ERROR|fatal|FATAL|panic)(\[\w+\])?\s*:"),
    re.compile(r"\b(TS\d{4}|E\d{3,4}|ERR!)\b"),
]
GENERIC = set(
    "error errors exception failed failure fatal exit code line file files command returned status non zero nonzero "
    "warning warn info debug traceback most recent call last see above below for more details run with stack".split()
)


def error_signature(text: str) -> dict | None:
    """The most telling line of a failed command's output, as words to search for: paths cut
    to their file name, numbers, ids and quotes' punctuation dropped. None when what is left is
    too generic to match on, such as a bare "Exit code 1". Where the index keeps tool errors
    (chats/errors.py), its own choice of line is used, so both sides pick the same one."""
    try:
        from .chats.errors import signature
    except ImportError:
        signature = None
    lines = [_ANSI.sub("", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"Exit code \d+", ln)][-80:]
    line = signature(text) if signature else None
    for pattern in [] if line else _ERROR_LINE:
        found = [ln for ln in lines if pattern.search(ln)]
        if found:
            line = found[-1]
            break
    if not line:
        return None
    norm = re.sub(r"(?:[A-Za-z]:)?(?:[\w.@~-]*/)+([\w.@-]+)", r"\1", line)  # paths -> file names
    norm = re.sub(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b|\b\d+(\.\d+)*\b", " ", norm, flags=re.I)
    words = []
    for w in re.findall(r"[A-Za-z_][\w-]*", norm):
        low = w.lower()
        if len(low) > 2 and low not in GENERIC and low not in words:
            words.append(low)
    if len(words) < 3:
        return None
    return {"line": _clip(line, 160), "words": words[:8], "key": " ".join(words[:8])}


def failure_items(
    con, sig: dict, cwd: str | None, own: list[str], project=False, until: float | None = None, command=None
) -> list[Item]:
    """Past chats where the same error came up, newest in this project first, with the first
    line of the agent's reply after it. From the index's tool errors when it keeps them, else
    from pasted copies in messages, among the chats of this session's context (_chat_scope). A
    signature found in many chats is common knowledge, so it stays silent."""
    words = [w for w in sig["words"] if re.fullmatch(r"\w+", w)]
    if len(words) < 3:
        return []
    if project is False:
        project = _project(con, cwd)
    keys = project["keys"] if project else []
    scope, scope_args = _chat_scope(con, cwd)
    if TOOL_ERRORS and _has_table(con, "tool_errors"):
        return _tool_error_items(con, words, keys, own, until, command, scope, scope_args)
    # Every word within a short span, as in a pasted copy of the same error line.
    q = f"NEAR({' '.join(chr(34) + w + chr(34) for w in words)}, {len(words) + 6})"
    rows = con.execute(
        f"""select m.session_id sid, min(m.seq) seq, s.title, s.updated_at, s.project_key, s.project
            from messages_fts f join messages m on m.id = f.rowid join sessions s on s.id = m.session_id
            where messages_fts match ? and s.origin = 'interactive'
              {f"and s.id not in ({','.join('?' * len(own))})" if own else ""}
              {"and m.ts < ? and s.started_at < ?" if until else ""}
              {scope}
            group by m.session_id order by s.updated_at desc limit 40""",
        [q, *own, *([until, until] if until else []), *scope_args],
    ).fetchall()
    if not rows or len(rows) > 20:
        return []
    same = [r for r in rows if r["project_key"] in keys]
    if not same and len(sig["words"]) < 5:
        return []  # elsewhere only counts for a long, specific signature
    out = []
    for r in (same or rows)[:2]:
        after = _reply_after(con, r["sid"], r["seq"])
        if not after:
            continue
        line = (
            f'- {_day(r["updated_at"])}, {_short(r["sid"])} "{_clip(r["title"] or "", 60)}": '
            f"the same error, and the reply after it began: {after}"
        )
        out.append(Item(r["sid"], "chat", _clip(line, LINE_CHARS)))
    return out


def _tool_error_items(
    con, words: list[str], keys: list[str], own: list[str], until, command, scope: str = "", scope_args=()
) -> list[Item]:
    """Past failed tool calls (chats/errors.py) whose one-line signature holds every word of
    this one. A bare exit code never matches (too few words); a match counts in this project,
    or elsewhere when the command starts the same way."""
    head = " ".join(str(command or "").split()[:2]).lower()
    rows = con.execute(
        f"""select e.session_id sid, e.seq, e.command_head, e.signature, e.next_reply, s.title, s.updated_at,
                   s.project_key
            from tool_errors_fts join tool_errors e on e.id = tool_errors_fts.rowid
            join sessions s on s.id = e.session_id
            where tool_errors_fts match ? and s.origin = 'interactive'
              {f"and e.session_id not in ({','.join('?' * len(own))})" if own else ""}
              {"and e.ts < ?" if until else ""}
              {scope}
            order by e.ts desc limit 60""",
        [" AND ".join(f'"{w}"' for w in words[:6]), *own, *([until] if until else []), *scope_args],
    ).fetchall()
    chats = list(dict.fromkeys(r["sid"] for r in rows))
    if not rows or len(chats) > 20:
        return []
    near = [
        r
        for r in rows
        if r["project_key"] in keys or (head and " ".join(str(r["command_head"]).split()[:2]).lower() == head)
    ]
    near.sort(key=lambda r: r["project_key"] not in keys)  # this project first, newest first within
    out, seen = [], set()
    for r in near:
        if r["sid"] in seen or len(out) == 2:
            continue
        seen.add(r["sid"])
        # seq is NULL where the failure could not be placed in the chat exactly: the reply stored with it stands in
        after = _reply_after(con, r["sid"], r["seq"]) if r["seq"] is not None else _gist(r["next_reply"] or "", 170)
        line = (
            f'- {_day(r["updated_at"])}, {_short(r["sid"])} "{_clip(r["title"] or "", 50)}" hit '
            f'"{_clip(r["signature"], 90)}"' + (f"; the reply after it began: {after}" if after else "")
        )
        out.append(Item(r["sid"], "chat", _clip(line, LINE_CHARS)))
    return out


def _has_table(con, name: str) -> bool:
    return bool(con.execute("select 1 from sqlite_master where name = ?", (name,)).fetchone())


def _reply_after(con, sid: str, seq: int) -> str:
    row = con.execute(
        "select text from messages where session_id = ? and seq > ? and role = 'assistant' order by seq limit 1",
        (sid, seq),
    ).fetchone()
    return _gist(row[0], 170) if row else ""


# --- after compaction --------------------------------------------------------------------------

# How agents open the summary they write into the chat after compacting it; not a request.
COMPACTION_SUMMARY = ("This session is being continued from a previous conversation",)


def _recent_asks(agent: str, transcript: str | None, n: int = 3) -> str:
    """The person's last few requests in this session, read from its transcript."""
    if not transcript or not os.path.exists(transcript):
        return ""
    try:
        from .chats.sources import claude, codex, cursor

        if agent == "codex":
            s = codex.load(transcript, {})
        elif agent == "cursor":
            s = cursor.load_transcript(transcript, errors=False)
        else:
            s = claude.load(transcript)
    except Exception:
        return ""
    asks = [
        m.text
        for m in (s.messages if s else [])
        if m.role == "user" and not m.text.lstrip().startswith(("<", *COMPACTION_SUMMARY))
    ]
    return " ".join(a[:400] for a in asks[-n:])


# --- helpers ------------------------------------------------------------------------------------


def _connect():
    """The chat index, opened read-only, or None when a hook has nothing to read: no index yet,
    or one built under another chat schema (store.CHAT_SCHEMA_VERSION). A hook never builds or
    rebuilds it: the first one after a release that bumped the schema would spend seconds on a
    rebuild the indexer does at the next MCP server start or nightly run, and a hook of an older
    release would rebuild a newer index backwards. Until then the hooks stay silent."""
    import sqlite3

    from .chats import store

    try:
        con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return None
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("select v from meta where k = 'chat_schema'").fetchone()
        if row and row[0] == store.CHAT_SCHEMA_VERSION:
            return con
    except sqlite3.Error:
        pass
    con.close()
    return None


def _own(e: "Event") -> list[str]:
    """This session's own chat in the index, so it never matches itself."""
    return [f"{e.agent}:{e.session}"] if e.session else []


def _project_of(con, cwd: str | None, state: State) -> dict | None:
    """_project, remembered for the session: it costs a scan of every folder key."""
    cache = state.d.setdefault("projects", {})
    if str(cwd) not in cache:
        cache[str(cwd)] = _project(con, cwd)
    return cache[str(cwd)]


def _project(con, cwd: str | None) -> dict | None:
    """The project a folder belongs to, and every folder key it spans in the index and the
    notes: one key for a folder projects.yaml does not list, all of a listed project's."""
    if not cwd:
        return None
    from . import projects
    from .chats.projects import resolve

    own = resolve(cwd)
    keys = {own.key} if own else set()
    try:
        listed = projects.load()
        pid = projects.match(own.key if own else None, cwd, listed) if listed else None
    except Exception:
        listed, pid = {}, None
    if pid:
        one = {pid: listed[pid]}
        rows = list(con.execute("select distinct project_key, project from sessions where project_key is not null"))
        with contextlib.suppress(Exception):  # no notes index yet
            rows += [(k, None) for (k,) in con.execute("select distinct project from notes where project is not null")]
        keys |= {k for k, folder in rows if projects.match(k, folder, one)}
    return {"id": pid or (own.key if own else None), "keys": sorted(keys)} if keys else None


def _same_project(project: dict, key: str | None) -> bool:
    return bool(key) and key in project["keys"]


def _chat_line(con, row: dict, meta) -> str:
    title = _clip(row.get("title") or "", 70)
    line = f'- worked on this on {_day(meta["updated_at"])} in {_short(row["chat_id"])} "{title}": '
    return _clip(line + _outcome(con, row["chat_id"]), LINE_CHARS)


def _note_line(nid: str, title: str, body: str, last_seen: str | None) -> str:
    """The note's title and the gist of its body, cut so the id at the end always fits."""
    from .memory.said import dangerous

    tail = f" (note {nid}{', ' + last_seen[:10] if last_seen else ''})"
    head = f"- {_clip(title, 90)}: "
    gist = _gist(body, max(40, LINE_CHARS - len(head) - len(tail)))
    # The rules passed notes.pushed as written; _gist rewrites them (code marks and links out), so
    # what is delivered is checked again, and only the title goes when it no longer passes.
    return head.rstrip(": ") + tail if dangerous(gist) else head + gist + tail


def _outcome(con, sid: str) -> str:
    """How a chat ended: the gist of the agent's last reply."""
    row = con.execute(
        "select text from messages where session_id = ? and role = 'assistant' order by seq desc limit 1", (sid,)
    ).fetchone()
    return ("it ended: " + _gist(row[0], 160)) if row else "no reply recorded"


def _gist(text: str, n: int) -> str:
    """The first lines of a message that say something, without markdown, code or links."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    keep = []
    for ln in text.splitlines():
        ln = re.sub(r"^[#>*\-\s\d.)]+", "", ln).strip()
        ln = re.sub(r"[*_`]+", "", ln)
        ln = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", ln)
        if len(ln) > 3:
            keep.append(ln)
        if sum(len(k) for k in keep) > n:
            break
    return _clip(" ".join(keep), n)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _short(chat_id: str) -> str:
    agent, _, native = chat_id.partition(":")
    return f"{agent}:{native[:8]}" if native else chat_id


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "an unknown date"


def _iso(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _ts(value: str | None, default: float) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return default


def cosine(vector, blob: bytes) -> float:
    """Dot product of a unit vector with a stored float16 unit vector, without numpy."""
    import struct

    other = struct.unpack(f"{len(blob) // 2}e", blob)
    return sum(a * b for a, b in zip(vector, other, strict=False))

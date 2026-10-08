"""Map step: read chat digests and propose candidate memories.

Candidates are not memories yet: consolidation decides which become notes, which
merge into existing notes, and which are dropped. Several chats share one model call
(a batch) because most providers charge a fixed overhead per call.
"""

import re
import time
from dataclasses import dataclass

from ..chats.redact import redact
from ..llm import Client, Profile
from . import digest, prompts

SYSTEM, PROMPT_VERSION = prompts.load("extract")
MAX_PER_CHUNK = 6

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memories"],
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["chat", "type", "scope", "title", "body", "why", "evidence", "speaker", "confidence"],
                "properties": {
                    "chat": {"type": "integer"},
                    "type": {"enum": ["preference", "fact", "decision", "lesson", "open"]},
                    "scope": {"enum": ["global", "project"]},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "why": {"type": "string"},
                    "evidence": {"type": "string"},
                    "speaker": {"enum": ["user", "assistant"]},
                    "confidence": {"enum": ["high", "medium", "low"]},
                },
            },
        }
    },
}

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)")


def ensure_tables(con) -> None:
    con.executescript(
        """
        create table if not exists extractions (
          session_id text primary key, digest_hash text, prompt_version text, backend text,
          candidates int, status text, error text, at real
        );
        create table if not exists candidates (
          id integer primary key, session_id text, project_key text, project_name text,
          scope text, type text, title text, body text, why text, evidence text, speaker text,
          confidence text, chat_ts real, status text default 'new', note_id text, reason text,
          attempts int default 0, created real
        );
        create index if not exists candidates_status on candidates(status, project_key);
        """
    )


@dataclass
class Unit:
    """One chunk of one chat: the item that goes into a batch."""

    session: object  # sqlite3.Row from the sessions table
    chunk: str
    parts: int
    digest_hash: str


def plan(items, profile: Profile) -> tuple[list[list[Unit]], list]:
    """items: (session row, message rows) pairs. Returns (batches, sessions too short to read)."""
    units, skipped = [], []
    for session, messages in items:
        d = digest.build(session, messages)
        if d is None:
            skipped.append(session)
            continue
        units += [Unit(session, chunk, len(d.chunks), d.hash) for chunk in d.chunks]
    batches, current, size = [], [], 0
    for u in units:
        if current and (size + len(u.chunk) > profile.batch_chars or len(current) >= profile.batch_chats):
            batches.append(current)
            current, size = [], 0
        current.append(u)
        size += len(u.chunk)
    if current:
        batches.append(current)
    return batches, skipped


def run_batch(client: Client, batch: list[Unit]) -> list[tuple[Unit, list[dict]]]:
    """One model call over a batch; returns each chunk with its candidate memories."""
    text = "\n\n".join(f"=== CHAT {i}\n{_scrub(u.chunk)}" for i, u in enumerate(batch, 1))
    data = client.json("extract", SYSTEM, text, SCHEMA)
    per_chat: dict[int, list[dict]] = {i: [] for i in range(1, len(batch) + 1)}
    for m in data["memories"]:
        if m["chat"] in per_chat:
            per_chat[m["chat"]].append(m)
    return [(u, _clean(per_chat[i], u)) for i, u in enumerate(batch, 1)]


def grounded(evidence: str, source: str) -> bool:
    """Does the quoted evidence occur in the chat text the model read? Checked on
    overlapping 6-word windows, so light paraphrase at the edges still passes."""
    words, haystack = _norm(evidence).split(), f" {_norm(source)} "
    if len(words) < 4:
        return False
    windows = (" ".join(words[i : i + 6]) for i in range(0, max(1, len(words) - 5), 3))
    return any(f" {w} " in haystack for w in windows)


class Collector:
    """Holds chunk results until every chunk of a chat is in."""

    def __init__(self, batches: list[list[Unit]]):
        self.pending = {
            u.session["id"]: {"session": u.session, "left": u.parts, "candidates": [], "hash": u.digest_hash}
            for batch in batches
            for u in batch
        }

    def add(self, unit: Unit, candidates: list[dict]):
        """Returns (session, candidates, digest hash) once the chat is complete, else None."""
        entry = self.pending.get(unit.session["id"])
        if entry is None:
            return None
        entry["candidates"] += candidates
        entry["left"] -= 1
        if entry["left"]:
            return None
        del self.pending[unit.session["id"]]
        return entry["session"], entry["candidates"], entry["hash"]

    def fail(self, unit: Unit):
        """Drops a chat whose chunk failed; returns its session the first time only."""
        entry = self.pending.pop(unit.session["id"], None)
        return entry["session"] if entry else None


def store(
    con, session, status: str, candidates: list[dict], digest_hash: str | None, backend: str, error: str | None = None
) -> None:
    con.execute("delete from candidates where session_id=? and status='new'", (session["id"],))
    now = time.time()
    con.executemany(
        """insert into candidates(session_id, project_key, project_name, scope, type, title, body, why,
           evidence, speaker, confidence, chat_ts, created) values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                session["id"],
                session["project_key"],
                session["project_name"],
                c["scope"],
                c["type"],
                c["title"],
                c["body"],
                c["why"],
                c["evidence"],
                c["speaker"],
                c["confidence"],
                session["updated_at"],
                now,
            )
            for c in candidates
        ],
    )
    con.execute(
        "insert or replace into extractions values (?,?,?,?,?,?,?,?)",
        (session["id"], digest_hash, PROMPT_VERSION, backend, len(candidates), status, error, now),
    )


def _scrub(text: str) -> str:
    """Secrets are already redacted in the index; also mask contact details before a model sees them."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _clean(memories: list[dict], unit: Unit) -> list[dict]:
    out = []
    for m in memories[:MAX_PER_CHUNK]:
        m = {k: (v.strip() if isinstance(v, str) else v) for k, v in m.items() if k != "chat"}
        text = " ".join((m["title"], m["body"], m["why"]))
        if redact(text) != text or "[REDACTED]" in text:
            continue  # a credential must never become a memory
        m["title"], m["body"] = m["title"][:120], m["body"][:700]
        if m["speaker"] == "assistant" and m["confidence"] == "high":
            m["confidence"] = "medium"  # the developer never confirmed it
        if not grounded(m["evidence"], unit.chunk):
            m["confidence"] = "low"  # the quote is not in the chat: unverified
        if m["scope"] == "project" and not unit.session["project_key"]:
            m["scope"] = "unfiled"  # about some project, but the chat ran in a scratch folder
        out.append(m)
    return out

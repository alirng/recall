"""Latest first: what an agent needs when recall shows it a past chat, so that it does not answer
"what did we decide?" with a version that was changed later. All of it is read from the index;
nothing here calls a model.

- newer: a later chat in the same project that covers the same ground, with its date and id.
  Agents asked about a decision often open an older chat that looks almost the same as the one
  that changed it, and answer from that.
- ended: where the chat ended up: the person's last instruction in it and the agent's closing
  words. changed_course_at: the person's turns that changed course ("instead", "rather than",
  "not quite", "switch to", ...). Which earlier message each one overturned is not marked: by
  shared words, the guess was wrong about as often as right.
- who said each line, and when: the person, the agent, or, in a subagent's chat, the parent
  agent that briefed it.

"Covers the same ground" is measured on words. A chat's fingerprint is its distinctive words:
the ones it uses most, weighted by how rare they are across every indexed message. A later chat
covers the same ground when, on average, each chat uses at least half of the other's
fingerprint weight, and neither uses less than a quarter. Both directions count, so a long chat
that mentions everything does not cover every short chat before it. On the train split of the
evolve and qa_v2 cases, the later chat of a changed decision mostly clears that bar, against
about one random pair of chats in twenty from the same project.
"""

import math
import re
import time
from collections import Counter

from . import usage
from .chats import store

# Words of at least three characters that start with a letter. FTS5's tokenizer splits on
# underscores too, so person__c counts as "person".
_WORD = re.compile(r"[^\W\d_][^\W_]{2,}")
FP_MESSAGES = 200  # messages read per chat, from its start
FP_CHARS = 4000  # of each message: a long tool-heavy reply repeats itself
FP_POOL = 80  # most frequent words looked up for their rarity
FP_TERMS = 24  # distinctive words kept
COMMON = 0.04  # a word in more than this share of all messages says nothing about a topic
# A later chat is "newer on this" when the two chats' fingerprints overlap by at least MEAN on
# average and FLOOR each way. A chat outside any project is compared with chats in every
# project, so it must overlap more.
MEAN, FLOOR = 0.45, 0.25
MEAN_ANY_PROJECT, FLOOR_ANY_PROJECT = 0.55, 0.35
CANDIDATES = 6  # later chats compared in full, the best by keyword score
POOL = 600  # matching messages considered when picking them
MIN_GAP = 1800  # seconds the later chat must run past the earlier one's last message
# A later chat in another agent with the same words both ways and about as many messages is a
# copy (Codex keeps an imported Cursor chat as a chat of its own), not a newer one: reading it
# would add nothing. The same agent's look-alike rerun is newer.
COPY = 0.95
RANKED = 30  # places of the search's ranking that count as being about the query
RANKED_MEAN, RANKED_FLOOR = 0.3, 0.15
PICK = "latest"  # which passing chat to name: the latest one, or the closest ("overlap")
# The chat asking always covers the ground it searches for, so it is never offered as the newer
# chat: by its session id where the agent gives one (usage.agent_session: Claude Code, Codex),
# and for Cursor, which gives none, as any chat written to in the last few minutes.
ACTIVE = 900
ANNOTATED = 8  # search results checked for a newer chat, from the top

# Where a chat ended and where it changed course.
ENDED_PERSON = 200  # characters of the person's last instruction
ENDED_AGENT = 300  # characters of the end of the agent's last reply
CHANGED_SHOWN = 4  # most recent course changes listed
MIN_INSTRUCTION = 12  # characters: shorter last turns ("yep", "go") point back at the one before
# A turn where the person changes course: phrasings that overturn something said before.
_CHANGE = re.compile(
    r"\b(actually|instead|rather than|not quite|change (?:it|this|that|the \w+ )?(?:from|to)|"
    r"switch(?:ed)? (?:back |over )?to|go back|revert|roll ?back|undo|scrap|start (?:again|over)|"
    r"let'?s not|no longer|changed? my mind|on second thought|get rid of|replace|don'?t (?:use|do|want)|"
    r"stop using|stick with|go with|we'?ll use|use \w+ not)\b",
    re.I,
)

_cache: dict[tuple, object] = {}


def _cached(key: tuple, make):
    if key not in _cache:
        if len(_cache) > 50_000:
            _cache.clear()
        _cache[key] = make()
    return _cache[key]


def _db_key(con) -> str:
    return next((r[2] for r in con.execute("pragma database_list") if r[1] == "main"), "") or ""


def _norm(word: str) -> str:
    """A rough stem, so "decided" and "decide" count as one word."""
    for end in ("ing", "ed", "es", "s"):
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[: -len(end)]
    return word


def _df(con, db: str, word: str) -> int:
    """How many messages hold the word, as search matches it (porter-stemmed)."""
    return _cached(
        (db, "df", word),
        lambda: con.execute("select count(*) from messages_fts where messages_fts match ?", (f'"{word}"',)).fetchone()[
            0
        ],
    )


def _total(con, db: str) -> int:
    return _cached((db, "total"), lambda: con.execute("select count(*) from messages").fetchone()[0] or 1)


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "")[:FP_CHARS].lower()) if w not in store.STOPWORDS]


def _idf(con, db: str, word: str) -> float:
    d = _df(con, db, word)
    total = _total(con, db)
    return math.log(total / d) if 0 < d <= total * COMMON else 0.0


def _fingerprint(con, db: str, sid: str, n: int) -> tuple[dict[str, float], frozenset[str]]:
    """The chat's distinctive words (stemmed roughly), each weighted by its rarity, and every
    word it uses. n, its message count, keys the cache, so a chat that grows is read again."""

    def make():
        tf: Counter = Counter()
        raw: dict[str, str] = {}
        for (text,) in con.execute(
            "select text from messages where session_id = ? order by seq limit ?", (sid, FP_MESSAGES)
        ):
            for w in _words(text):
                k = _norm(w)
                tf[k] += 1
                raw.setdefault(k, w)
        idf = {k: _idf(con, db, raw[k]) for k, _ in tf.most_common(FP_POOL)}
        top = sorted((k for k in idf if idf[k]), key=lambda k: (1 + math.log(tf[k])) * idf[k], reverse=True)
        return {k: idf[k] for k in top[:FP_TERMS]}, frozenset(tf)

    return _cached((db, "fp", sid, n), make)


def _cover(fp: dict[str, float], have: frozenset[str]) -> float:
    total = sum(fp.values())
    return sum(v for w, v in fp.items() if w in have) / total if total else 0.0


def newer(
    con,
    sid: str,
    query: str | None = None,
    ranked: list[str] | None = None,
    db: str | None = None,
    now: float | None = None,
) -> dict | None:
    """The latest chat after this one, in the same project, that covers the same ground; None
    when there is none.

    query: what the agent searched for; a later chat must then also use at least half of its
    words, so the flag points at a chat about what the agent asked.
    ranked: the search's own ranking for that query, deeper than the results shown. A later chat
    ranked there is already about what the agent asked, so it needs less overlap with this one
    (RANKED_MEAN, RANKED_FLOOR): a decision changed in a later chat often uses other words.
    now: the time of the call (default the clock), for replaying calls made earlier."""
    db = db if db is not None else _db_key(con)
    s = con.execute(
        "select id, agent, project_key, started_at, updated_at, n_messages from sessions where id = ?", (sid,)
    ).fetchone()
    if not s or not s["updated_at"]:
        return None
    fp, mine = _fingerprint(con, db, sid, s["n_messages"])
    if len(fp) < 4:  # too little to say what it is about
        return None
    later = """s.id != ? and (? is null or s.native_id is not ?) and s.updated_at > ? and s.updated_at < ?
           and coalesce(s.started_at, s.updated_at) > ?
           and coalesce(s.origin, '') not in ('subagent', 'headless')
           and (? is null or s.project_key is null or s.project_key = ?)"""
    args = [
        sid,
        own := usage.agent_session(),
        own,
        s["updated_at"] + MIN_GAP,
        (now or time.time()) - ACTIVE,
        s["started_at"] or s["updated_at"],
        s["project_key"],
        s["project_key"],
    ]
    rows = con.execute(
        f"""select m.session_id sid, bm25(messages_fts) score
            from messages_fts join messages m on m.id = messages_fts.rowid
            join sessions s on s.id = m.session_id
            where messages_fts match ? and {later}
            order by score limit ?""",
        [" OR ".join(f'"{w}"' for w in fp), *args, POOL],
    ).fetchall()
    hits: dict[str, list[float]] = {}
    for r in rows:
        hits.setdefault(r["sid"], []).append(r["score"])
    cands = sorted(hits, key=lambda c: sum(hits[c][: store.TOP_HITS]))[:CANDIDATES]
    on_query = set((ranked or [])[:RANKED]) - {sid}
    cands += [c for c in on_query if c not in cands]
    if not cands:
        return None
    qwords = {_norm(w) for w in _words(query or "") if _idf(con, db, w)}
    found = []
    marks = ",".join("?" * len(cands))
    for r in con.execute(
        f"""select s.id, s.agent, s.title, s.project_key, s.updated_at, s.n_messages from sessions s
            where s.id in ({marks}) and {later}""",
        [*cands, *args],
    ):
        theirs_fp, theirs = _fingerprint(con, db, r["id"], r["n_messages"])
        forward, back = _cover(fp, theirs), _cover(theirs_fp, mine)
        same = s["project_key"] and r["project_key"] == s["project_key"]
        if r["id"] in on_query:
            mean, floor = RANKED_MEAN, RANKED_FLOOR
        else:
            mean, floor = (MEAN, FLOOR) if same else (MEAN_ANY_PROJECT, FLOOR_ANY_PROJECT)
        if (forward + back) / 2 < mean or min(forward, back) < floor:
            continue
        copy = r["agent"] != s["agent"] and abs(r["n_messages"] - s["n_messages"]) <= max(2, s["n_messages"] // 10)
        if copy and min(forward, back) >= COPY:
            continue
        if qwords and len(qwords & theirs) * 2 < len(qwords):
            continue
        found.append((r, forward + back))
    if not found:
        return None
    # The end of the line rather than the nearest step: a decision changed twice is answered by
    # the last chat.
    b = max(found, key=lambda x: x[0]["updated_at"] if PICK == "latest" else x[1])[0]
    return {"chat_id": b["id"], "title": b["title"], "updated": store.fmt_time(b["updated_at"])}


def who(role: str, origin: str | None) -> str:
    """Who wrote a message: the person ("user"), the agent ("assistant"), or, for the "user"
    turns of a subagent's or a headless chat, the parent agent or script that wrote them."""
    if role == "user":
        return {"subagent": "parent agent", "headless": "script"}.get(origin or "", "user")
    return role


def annotate_search(con, rows: list[dict], queries: list[str], found: list[dict], explain: dict) -> list[dict]:
    """Search results with each hit labelled with who wrote it (and its date when not the
    chat's last day), and the top results with the newer chat on the same ground, if any.
    found: every chat the search returned, of which rows are the first; explain: what
    store.search filled in for a single phrasing, whose scores rank chats beyond those."""
    db = _db_key(con)
    scores = explain.get("scores") if len(queries) == 1 else None
    ranked = sorted(scores, key=scores.get, reverse=True) if scores else [r["chat_id"] for r in found]
    for i, row in enumerate(rows):
        s = con.execute("select origin from sessions where id = ?", (row["chat_id"],)).fetchone()
        seqs = [h["seq"] for h in row.get("hits") or []]
        meta = {
            r["seq"]: r
            for r in con.execute(
                f"select seq, role, ts from messages where session_id = ? and seq in ({','.join('?' * len(seqs))})",
                [row["chat_id"], *seqs],
            )
        }
        day = (row.get("updated") or "")[:10]
        for h in row.get("hits") or []:
            m = meta.get(h["seq"])
            if not m:
                continue
            h["who"] = who(m["role"], s["origin"] if s else None)
            when = (store.fmt_time(m["ts"]) or "")[:10]
            if when and when != day:
                h["date"] = when
        if i < ANNOTATED and (n := newer(con, row["chat_id"], queries[0] if queries else None, ranked, db)):
            row["newer_chat"] = n
    return rows


def search_lines(row: dict) -> tuple[list[str], str | None]:
    """The text form of annotate_search's additions: each hit's label, and the newer line."""
    labels = []
    for h in row.get("hits") or []:
        label = " ".join(x for x in (h.get("who"), h.get("date")) if x)
        labels.append(f" {label}" if label else "")
    n = row.get("newer_chat")
    line = f"newer chat on this: {n['chat_id']} · {n['updated']} · {n['title']}" if n else None
    return labels, line


def annotate_read(con, result: dict, query: str | None, first: bool) -> dict:
    """read_chat's result with the roles of a subagent's or headless chat's prompts named for who
    wrote them; and, on a first read (no offset), the newer chat on the same ground, where this
    chat ended up (when the messages shown stop short of its end), and the person's turns that
    changed course."""
    sid = result["chat_id"]
    s = con.execute("select origin from sessions where id = ?", (sid,)).fetchone()
    origin = s["origin"] if s else None
    for m in result["transcript"]:
        m["role"] = who(m["role"], origin)
    if not first:
        return result
    db = _db_key(con)
    msgs = con.execute("select seq, role, text from messages where session_id = ? order by seq", (sid,)).fetchall()
    extra = {}
    if n := newer(con, sid, query, db=db):
        extra["newer_chat"] = n
    shown = {m["seq"] for m in result["transcript"]}
    if msgs and msgs[-1]["seq"] not in shown and (ended := _ended(msgs, origin)):
        extra["ended"] = ended
    if origin not in ("subagent", "headless") and (changes := _changes(msgs)):
        extra["changed_course_at"] = changes[-CHANGED_SHOWN:]
    # The additions go right after the chat's own fields, before the transcript.
    transcript = result.pop("transcript")
    result.update(extra)
    result["transcript"] = transcript
    return result


def _ended(msgs, origin) -> dict | None:
    """The person's last instruction (or the one before a bare "yes") and the end of the agent's
    last reply."""
    users = [(m["seq"], own) for m in msgs if m["role"] == "user" and (own := _own_words(m["text"]) or "").strip()]
    agents = [m for m in msgs if m["role"] != "user" and (m["text"] or "").strip()]
    if not users or not agents:
        return None
    seq, said = next(((q, t) for q, t in reversed(users) if len(t.strip()) >= MIN_INSTRUCTION), users[-1])
    said = " ".join(said.split())
    closing = " ".join(agents[-1]["text"].split())
    return {
        "seq": seq,
        who("user", origin): said[:ENDED_PERSON] + ("…" if len(said) > ENDED_PERSON else ""),
        "assistant": ("…" if len(closing) > ENDED_AGENT else "") + closing[-ENDED_AGENT:],
    }


def _own_words(text: str) -> str | None:
    """The person's own words in a user turn, or None for a turn an app or agent wrote (a
    tag-wrapped notice, a subagent's completion note). Codex puts attached files' paths before
    "## My request:"."""
    text = (text or "").split("## My request:")[-1].lstrip()
    if text.startswith(("<", "#")) or text.startswith(AUTOMATED):
        return None
    return text


AUTOMATED = ("Perform any necessary follow-up", "If the available MCP tools")


def _changes(msgs) -> list[int]:
    """The person's turns, after the opening one, that change course."""
    return [
        m["seq"]
        for i, m in enumerate(msgs)
        if m["role"] == "user" and i > 0 and (own := _own_words(m["text"])) and _CHANGE.search(own[:2000])
    ]

"""Evals for the two things recall must get right: finding chats, and extracting memories.

Search, two kinds of case:
* Hand-written: the way you would actually ask, e.g.
  {"query": "pen test the admin app", "agent": "cursor", "expect": "Admin app penetration testing"}
  where expect is a chat id or part of its title.
* Known-item, sampled automatically: three distinctive words from a chat's opening
  prompt must find that same chat. Needs no labelling and covers the whole corpus.

Memory extraction: hand-written expectations per chat, e.g.
  {"chat": "<chat id>", "expect": [{"name": "...", "type": "preference", "scope": "global",
   "all": [["commit"], ["push"]]}], "forbid": [["password"]], "max": 8}
A memory matches when, for every group in "all", its text contains one of the group's words.

Personal eval sets name real chats, so they live in recall's data folder, never in
the repository; evals/*.example.jsonl shows the format.
"""

import json
import random
import re

from . import config
from .chats import store

STOPWORDS = frozenset(
    "the a an and or but to of in on for with this that it is are be can we you i me my our your so do does "
    "how what why when where which there here from at as by if not no yes please just like want need make use "
    "using have has had will would could should about into then than them they their also all any some more "
    "most very get got let lets one".split()
)


def run_search(con, cases_path: str | None, sample: int) -> dict:
    out = {}
    if cases_path:
        out["hand_written"] = _score_search(con, _read_jsonl(cases_path))
    rng = random.Random(7)
    rows = con.execute("select id, agent, first_prompt from sessions where n_messages >= 4 order by id").fetchall()
    cases = []
    for r in rng.sample(rows, min(len(rows), sample * 2)):
        query = _vague_query(r["first_prompt"], rng)
        if query:
            cases.append({"query": query, "agent": r["agent"], "expect": r["id"]})
        if len(cases) == sample:
            break
    out["known_item"] = _score_search(con, cases)
    print(json.dumps(out, indent=2))
    return out


def run_memory(con, gold_path: str, model: str | None = None, fresh: bool = False, verbose: bool = True) -> dict:
    """Extract from each gold chat exactly as a memory run would, and score the result.

    recall: share of expected memories found; type and scope accuracy are measured
    on the matches. Negative chats check that little or nothing is extracted, and
    forbidden text (such as passwords) never appears.
    """
    from .llm import Client, from_spec
    from .memory import extract

    client = Client(from_spec(model, config.load()["model"]), cache=not fresh)
    gold = _read_jsonl(gold_path)
    items = []
    for g in gold:
        session = con.execute("select * from sessions where id=?", (g["chat"],)).fetchone()
        if session:
            messages = con.execute(
                "select seq, role, text from messages where session_id=? order by seq", (g["chat"],)
            ).fetchall()
            items.append((session, messages))
    batches, _ = extract.plan(items, client.provider.profile)
    found_by_chat: dict[str, list[dict]] = {}
    for batch in batches:
        for unit, memories in extract.run_batch(client, batch):
            found_by_chat.setdefault(unit.session["id"], []).extend(memories)

    expected = found = type_ok = scope_ok = over_max = leaks = total = 0
    for g in gold:
        memories = found_by_chat.get(g["chat"], [])
        total += len(memories)
        over_max += len(memories) > g.get("max", 8)
        misses = []
        for e in g.get("expect", []):
            expected += 1
            match = next((m for m in memories if _mentions(m, e["all"])), None)
            if not match:
                misses.append(e["name"])
                continue
            found += 1
            type_ok += e["type"] in ("any", match["type"])
            scope_ok += e["scope"] in ("any", match["scope"]) or (e["scope"], match["scope"]) == ("project", "unfiled")
        leaks += sum(_mentions(m, [group]) for group in g.get("forbid", []) for m in memories)
        if verbose:
            print(f"\n## {g['chat']}  ({g.get('note', '')})  -> {len(memories)} memories, missed {misses or 'none'}")
            for m in memories:
                print(f"  - [{m['type']}/{m['scope']}/{m['confidence']}] {m['title']}: {m['body'][:140]}")
    result = {
        "model": client.provider.label,
        "recall": round(found / expected, 3) if expected else None,
        "type_accuracy": round(type_ok / found, 3) if found else None,
        "scope_accuracy": round(scope_ok / found, 3) if found else None,
        "chats_over_max": over_max,
        "forbidden_leaks": leaks,
        "memories_per_chat": round(total / max(len(gold), 1), 2),
        "model_calls": client.calls,
        "tokens_in": client.tokens_in,
        "tokens_out": client.tokens_out,
    }
    print("\n" + json.dumps(result, indent=2))
    return result


def _score_search(con, cases) -> dict:
    ranks, misses = [], []
    for c in cases:
        rows = store.search(con, c["query"], c.get("agent"), c.get("project"), None, limit=10)
        expect = c["expect"].lower()
        rank = next(
            (
                i
                for i, r in enumerate(rows, 1)
                if r["chat_id"].endswith(c["expect"]) or expect in (r["title"] or "").lower()
            ),
            None,
        )
        ranks.append(rank)
        if rank is None or rank > 5:
            misses.append(c)
    n = len(ranks) or 1
    return {
        "cases": len(ranks),
        "hit@1": round(sum(r == 1 for r in ranks) / n, 3),
        "hit@5": round(sum(r is not None and r <= 5 for r in ranks) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
        "misses": misses[:5],
    }


def _vague_query(prompt: str, rng: random.Random) -> str | None:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", prompt) if w.lower() not in STOPWORDS]
    words = list(dict.fromkeys(words))
    return " ".join(rng.sample(words, 3)) if len(words) >= 4 else None


def _mentions(memory: dict, groups: list[list[str]]) -> bool:
    text = f"{memory['title']} {memory['body']} {memory.get('why', '')}".lower()
    return all(any(word.lower() in text for word in group) for group in groups)


def _read_jsonl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]

"""Evals for the two things recall must get right: finding chats, and extracting memories.

Search, three kinds of case:
* Hand-written: the way you would actually ask, e.g.
  {"query": "pen test the admin app", "agent": "cursor", "expect": "Admin app penetration testing"}
  where expect is a chat id or part of its title.
* Known-item, sampled automatically: three distinctive words from a chat's opening
  prompt must find that same chat. Needs no labelling and covers the whole corpus.
* Paraphrased (paraphrase_cases.jsonl, optional): the chat described in other words, as you
  might remember it months later. Same format as the hand-written cases.

Memory extraction: hand-written expectations per chat, e.g.
  {"chat": "<chat id>", "expect": [{"name": "...", "type": "preference", "scope": "global",
   "all": [["commit"], ["push"]]}], "forbid": [["password"]], "max": 8}
A memory matches when, for every group in "all", its text contains one of the group's words.

Personal eval sets name real chats, so they live in recall's data folder, never in
the repository; evals/*.example.jsonl shows the format.

Session start: what the hook injects when an agent opens in a folder. Each case is a folder
with every note labelled for it, e.g.
  {"id": "p-recall", "cwd": "/path/to/recall", "agent": "claude", "split": "train",
   "must": ["<note id>", ...], "ok": [...], "no": [...]}
`must` notes should be loaded there, `no` notes must not be (another context or project, or
noise). The block is built by the current code from a throwaway copy of the notes, so the
grade is exact and the same on every run.

LongMemEval (Wu et al., ICLR 2025) is the public check on search: 500 questions, each over
its own history of about 50 chat sessions, with the sessions that hold the answer marked.
Finding those sessions is what search_chats does, so `recall eval longmemeval` indexes each
history with recall's own code and scores the ranking with the benchmark's retrieval
metrics. Data: longmemeval_s_cleaned.json from huggingface.co/datasets/xiaowu0162/longmemeval-cleaned.
"""

import json
import math
import random
import re
import time
from datetime import datetime

from . import config
from .chats import store
from .chats.transcript import Message, Session

# Words never sampled into a known-item query. Kept apart from search's own list, so the
# sampled cases stay the same when search changes.
STOPWORDS = frozenset(
    "the a an and or but to of in on for with this that it is are be can we you i me my our your so do does "
    "how what why when where which there here from at as by if not no yes please just like want need make use "
    "using have has had will would could should about into then than them they their also all any some more "
    "most very get got let lets one".split()
)
# Characters a session-start block may take: a 4,500-character body plus its header and footer.
# Fixed here, not read from the hook's own limit, so changing the hook cannot move the bar.
SESSION_BUDGET = 5100


def run_search(con, cases_path: str | None, sample: int, paraphrased: str | None = None) -> dict:
    """paraphrased: cases whose queries describe a chat in other words than it used, which is
    where semantic search earns its keep; same format as the hand-written cases."""
    out = {}
    if cases_path:
        out["hand_written"] = _score_search(con, _read_jsonl(cases_path))
    if paraphrased:
        out["paraphrased"] = _score_search(con, _read_jsonl(paraphrased))
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


def run_longmemeval(
    path: str,
    limit: int | None = None,
    user_only: bool = False,
    search=None,
    embedder=None,
    cache=None,
    official: bool = False,
) -> dict:
    """Session retrieval on LongMemEval: for each question, index its history in memory and
    rank the sessions with recall's search.

    Metrics follow the benchmark's retrieval code: recall_any@k (an answer session in the top
    k), recall_all@k (every answer session in the top k; the paper's Recall@k) and ndcg_any@k.
    Abstention questions are skipped, as in the paper. user_only indexes only the user's turns,
    which is how the paper's own retrieval runs; recall indexes both sides of a chat.
    embedder: also rank by meaning (hybrid search); cache: a connection that keeps vectors
    between runs, so the 19,000 sessions are embedded once. official: only the questions the
    benchmark's own retrieval script scores (an answer turn on the user's side; 419 of 470).
    """
    search = search or store.search
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ks = (1, 3, 5, 10, 30, 50)
    totals: dict[str, float] = {}
    by_type: dict[str, list[float]] = {}
    n = 0
    started = time.time()
    for q in data:
        if q["question_id"].endswith("_abs") or (official and not _user_evidence(q)):
            continue
        if limit and n >= limit:
            break
        con = _lme_index(q, user_only)
        vector = False
        if embedder is not None:
            from . import embeddings

            embeddings.index_messages(con, embedder, cache=cache)
            vector = embeddings.query_vector(q["question"], embedder)
        ranked = [r["chat_id"].split(":", 1)[1] for r in search(con, q["question"], limit=max(ks), vector=vector)]
        evidence = set(q["answer_session_ids"])
        n += 1
        for k in ks:
            top = ranked[:k]
            found = evidence & set(top)
            totals[f"recall_any@{k}"] = totals.get(f"recall_any@{k}", 0) + bool(found)
            totals[f"recall_all@{k}"] = totals.get(f"recall_all@{k}", 0) + (found == evidence)
            dcg = sum(1 / math.log2(i + 2) for i, sid in enumerate(top) if sid in evidence)
            ideal = sum(1 / math.log2(i + 2) for i in range(min(len(evidence), k)))
            totals[f"ndcg_any@{k}"] = totals.get(f"ndcg_any@{k}", 0) + (dcg / ideal if ideal else 0)
        by_type.setdefault(q["question_type"], []).append(float(evidence <= set(ranked[:5])))
        con.close()
    result = {
        "questions": n,
        "user_turns_only": user_only,
        "official_subset": official,
        "seconds": round(time.time() - started, 1),
        **{k: round(v / max(n, 1), 3) for k, v in totals.items()},
        "recall_all@5_by_type": {t: round(sum(v) / len(v), 3) for t, v in sorted(by_type.items())},
    }
    print(json.dumps(result, indent=2))
    return result


# The benchmark's own reading prompt (step-by-step variant) and judge prompts, from
# github.com/xiaowu0162/LongMemEval src/generation and src/evaluation.
QA_TEMPLATE = (
    "I will give you several history chats between you and a user. Please answer the question based on the "
    "relevant chat history. Answer the question step by step: first extract all the relevant information, and "
    "then reason over the information to get the answer.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\n"
    "Question: {}\nAnswer (step by step):"
)
_JUDGE_BASE = (
    "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response "
    "contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or "
    "contains all the intermediate steps to get the correct answer, you should also answer yes. If the response "
    "only contains a subset of the information required by the answer, answer no. "
)
JUDGE = {
    "default": _JUDGE_BASE,
    "temporal-reasoning": _JUDGE_BASE + "In addition, do not penalize off-by-one errors for the number of days. If "
    "the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
    "predicting 19 days when the answer is 18), the model's response is still correct. ",
    "knowledge-update": "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous "
    "information along with an updated answer, the response should be considered as correct as long as the "
    "updated answer is the required answer.",
}
JUDGE_TAIL = (
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)
JUDGE_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, and a response from a model. Please "
    "answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to "
    "reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's "
    "personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model "
    "response correct? Answer yes or no only."
)
JUDGE_ABSTAIN = (
    "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if "
    "the model correctly identifies the question as unanswerable. The model could say that the information is "
    "incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\n"
    "Explanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? "
    "Answer yes or no only."
)
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "string"}, "answer": {"type": "string"}},
    "required": ["steps", "answer"],
    "additionalProperties": False,
}
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def run_longmemeval_qa(
    path: str,
    model: str,
    sample: int = 200,
    judge: str | None = None,
    context: str = "sessions",
    top_k: int = 10,
    max_tokens: int = 90_000,
    embedder=None,
    cache=None,
    db_path: str | None = None,
    workers: int = 4,
    out_path: str | None = None,
) -> dict:
    """End-to-end LongMemEval: retrieve with recall, answer with `model`, judge with `judge`
    (by default the same model) using the benchmark's own prompts. This is the number memory
    vendors headline. Token counts are kept apart for answering and judging, so the cost of
    answering can be read off on its own.

    context: "sessions" (the top_k chats in full, oldest first, as the benchmark formats
    them), "windows" (two turns either side of each chat's best hits, for the top 5 chats:
    roughly what an agent reads through search_chats and read_chat) or "oracle" (only the
    sessions holding the answer, an upper bound). max_tokens caps the history, estimated at
    four characters a token. sample: a fixed, type-stratified sample of the 500 questions.
    """
    import random
    from concurrent.futures import ThreadPoolExecutor

    from .llm import Client, from_spec

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rng = random.Random(7)
    by_type: dict[str, list] = {}
    for q in data:
        key = "abstention" if q["question_id"].endswith("_abs") else q["question_type"]
        by_type.setdefault(key, []).append(q)
    chosen = []
    for items in by_type.values():
        chosen += rng.sample(items, min(len(items), max(1, round(sample * len(items) / len(data)))))
    jobs = []
    for q in chosen:
        history, sessions = _qa_history(q, context, top_k, max_tokens, embedder, cache)
        jobs.append((q, history, sessions))
    settings = config.load()["model"]
    client = Client(from_spec(model, settings), db_path=db_path or paths_db())
    grader = Client(from_spec(judge or model, settings), db_path=db_path or paths_db())
    calls: list[dict] = []

    def one(job):
        q, history, sessions = job
        prompt = QA_TEMPLATE.format(history, q["question_date"], q["question"])
        started = time.time()
        before = (client.tokens_in, client.tokens_out)
        reply = client.json("lme-answer", "You answer questions from chat history.", prompt, _ANSWER_SCHEMA)
        seconds = time.time() - started
        response = f"{reply['steps']}\n\nAnswer: {reply['answer']}"
        verdict = grader.json("lme-judge", "You grade answers.", _judge_prompt(q, response), _VERDICT_SCHEMA)
        return {
            "question_id": q["question_id"],
            "type": "abstention" if q["question_id"].endswith("_abs") else q["question_type"],
            "correct": verdict["verdict"] == "yes",
            "answer": reply["answer"],
            "gold": q["answer"],
            "history_tokens": len(history) // 4,
            "sessions": sessions,
            "evidence_in_context": bool(set(q["answer_session_ids"]) & set(sessions)),
            "seconds": round(seconds, 1),
            "cached": (client.tokens_in, client.tokens_out) == before,
        }

    with ThreadPoolExecutor(workers) as pool:
        calls = list(pool.map(one, jobs))
    if out_path:
        with open(out_path, "w") as f:
            f.writelines(json.dumps(c) + "\n" for c in calls)
    n = len(calls)
    by: dict[str, list[bool]] = {}
    for c in calls:
        by.setdefault(c["type"], []).append(c["correct"])
    answered = [c for c in calls if not c["cached"]]
    result = {
        "model": client.provider.label,
        "judge": grader.provider.label,
        "context": context,
        "questions": n,
        "accuracy": round(sum(c["correct"] for c in calls) / n, 3),
        "accuracy_by_type": {t: round(sum(v) / len(v), 3) for t, v in sorted(by.items())},
        "evidence_in_context": round(sum(c["evidence_in_context"] for c in calls) / n, 3),
        "history_tokens_mean": round(sum(c["history_tokens"] for c in calls) / n),
        "history_tokens_max": max(c["history_tokens"] for c in calls),
        "answer_seconds_median": sorted(c["seconds"] for c in answered)[len(answered) // 2] if answered else None,
        "answer_calls": client.calls,
        "answer_tokens_in": client.tokens_in,
        "answer_tokens_out": client.tokens_out,
        "judge_calls": grader.calls,
        "judge_tokens_in": grader.tokens_in,
        "judge_tokens_out": grader.tokens_out,
    }
    print(json.dumps(result, indent=2))
    return result


def _qa_history(q, context, top_k, max_tokens, embedder, cache) -> tuple[str, list[str]]:
    """The history the answer model reads, formatted as the benchmark formats it."""
    dates = dict(zip(q["haystack_session_ids"], q["haystack_dates"], strict=True))
    turns = dict(zip(q["haystack_session_ids"], q["haystack_sessions"], strict=True))
    if context == "oracle":
        picked = [(sid, None) for sid in q["answer_session_ids"] if sid in turns]
    else:
        con = _lme_index(q)
        vector = False
        if embedder is not None:
            from . import embeddings

            embeddings.index_messages(con, embedder, cache=cache)
            vector = embeddings.query_vector(q["question"], embedder)
        rows = store.search(con, q["question"], limit=top_k if context == "sessions" else 5, vector=vector)
        con.close()
        picked = [(r["chat_id"].split(":", 1)[1], [h["seq"] for h in r["hits"]]) for r in rows]
    blocks, used = [], 0
    for sid, seqs in picked:
        kept = [t for t in turns[sid] if t["content"]]
        if context == "windows" and seqs:
            keep = sorted({i for s in seqs for i in range(max(0, s - 2), min(len(kept), s + 3))})
            parts, last = [], None
            for i in keep:
                if last is not None and i != last + 1:
                    parts.append("...")
                parts.append(f"{kept[i]['role']}: {kept[i]['content'].strip()}")
                last = i
            body = "\n\n".join(parts)
        else:
            body = "\n\n".join(f"{t['role']}: {t['content'].strip()}" for t in kept)
        if used + len(body) // 4 > max_tokens:
            break
        used += len(body) // 4
        blocks.append((dates[sid], sid, body))
    blocks.sort(key=lambda b: _lme_time(b[0]) or 0)
    history = "".join(
        f"\n### Session {i}:\nSession Date: {date}\nSession Content:\n\n{body}\n"
        for i, (date, _, body) in enumerate(blocks, 1)
    )
    return history, [sid for _, sid, _ in blocks]


def _judge_prompt(q: dict, response: str) -> str:
    if q["question_id"].endswith("_abs"):
        return JUDGE_ABSTAIN.format(q["question"], q["answer"], response)
    if q["question_type"] == "single-session-preference":
        return JUDGE_PREFERENCE.format(q["question"], q["answer"], response)
    return JUDGE.get(q["question_type"], JUDGE["default"]) + JUDGE_TAIL.format(q["question"], q["answer"], response)


def paths_db() -> str:
    from . import paths

    return paths.DB


def _lme_index(q: dict, user_only: bool = False):
    """An in-memory index of one LongMemEval question's history."""
    con = store.connect(":memory:")
    history = zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"], strict=True)
    for sid, date, turns in history:
        ts = _lme_time(date)
        kept = [t for t in turns if t["content"] and (t["role"] == "user" or not user_only)]
        messages = [Message(t["role"], t["content"], ts) for t in kept]
        if messages:
            store.upsert(con, Session("lme", sid, "longmemeval", started_at=ts, updated_at=ts, messages=messages))
    return con


def _user_evidence(q: dict) -> bool:
    return any(t.get("has_answer") and t["role"] == "user" for s in q["haystack_sessions"] for t in s)


# --- LoCoMo ------------------------------------------------------------------------


def run_locomo(path: str, search=None, embedder=None, cache=None) -> dict:
    """Session retrieval on LoCoMo (Maharana et al., 2024): ten long conversations, each
    question marked with the turns that answer it. Categories 1-4 are scored; category 5
    (adversarial, no answer) is left out, as every published result does."""
    search = search or store.search
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ks = (1, 3, 5, 10)
    totals: dict[str, float] = {}
    by_cat: dict[int, list[float]] = {}
    n = 0
    for sample in data:
        conv = sample["conversation"]
        con = store.connect(":memory:")
        names = [k for k in conv if re.fullmatch(r"session_\d+", k)]
        for key in names:
            ts = _locomo_time(conv.get(f"{key}_date_time"))
            messages = []
            for t in conv[key]:
                text = t.get("text", "")
                if t.get("blip_caption"):
                    text += f" [shares a photo: {t['blip_caption']}]"
                role = "user" if t.get("speaker") == conv.get("speaker_a") else "assistant"
                messages.append(Message(role, f"{t.get('speaker')}: {text}", ts))
            store.upsert(con, Session("locomo", key, "locomo", started_at=ts, updated_at=ts, messages=messages))
        vectors = embedder is not None
        if vectors:
            from . import embeddings

            embeddings.index_messages(con, embedder, cache=cache)
        for qa in sample["qa"]:
            if qa.get("category") == 5:
                continue
            evidence = {f"session_{m}" for m in re.findall(r"D(\d+):", " ".join(map(str, qa.get("evidence", []))))}
            if not evidence:
                continue
            vector = False
            if vectors:
                vector = embeddings.query_vector(qa["question"], embedder)
            ranked = [r["chat_id"].split(":", 1)[1] for r in search(con, qa["question"], limit=max(ks), vector=vector)]
            n += 1
            for k in ks:
                found = evidence & set(ranked[:k])
                totals[f"recall_any@{k}"] = totals.get(f"recall_any@{k}", 0) + bool(found)
                totals[f"recall_all@{k}"] = totals.get(f"recall_all@{k}", 0) + (found == evidence)
            by_cat.setdefault(qa["category"], []).append(float(bool(evidence & set(ranked[:5]))))
        con.close()
    result = {
        "questions": n,
        **{k: round(v / max(n, 1), 3) for k, v in totals.items()},
        "recall_any@5_by_category": {c: round(sum(v) / len(v), 3) for c, v in sorted(by_cat.items())},
    }
    print(json.dumps(result, indent=2))
    return result


def _locomo_time(text: str | None) -> float | None:
    try:  # "1:56 pm on 8 May, 2023"
        return datetime.strptime(text, "%I:%M %p on %d %B, %Y").timestamp()
    except (TypeError, ValueError):
        return None


def _lme_time(date: str) -> float | None:
    try:  # "2023/05/20 (Sat) 02:21"
        return datetime.strptime(date, "%Y/%m/%d (%a) %H:%M").timestamp()
    except (TypeError, ValueError):
        return None


def run_session(cases_path: str, split: str | None = None, verbose: bool = True) -> dict:
    """Grade the session-start block in each case's folder against its labelled notes."""
    import os
    import shutil
    import tempfile

    from . import paths
    from .memory.context import session_context
    from .memory.notes import NoteStore

    with open(cases_path) as f:
        cases = json.load(f)
    if split:
        cases = [c for c in cases if c.get("split") == split]
    with tempfile.TemporaryDirectory() as tmp:
        # Compile indexes with the code under test, never into the real memory folder.
        root = os.path.join(tmp, "memory")
        shutil.copytree(paths.MEMORY, root, ignore=shutil.ignore_patterns(".git"))
        os.makedirs(os.path.join(root, ".git"))
        notes = NoteStore(root)
        active = notes.load()
        notes.compile(active)
        titles = {n.id: n.title for n in active.values() if n.status == "active"}
        rows = []
        for c in cases:
            text = session_context(c["cwd"], notes=notes, agent=c.get("agent", "claude"))
            loaded = {i for i, t in titles.items() if _shown(t, text)}
            must, ok, no = set(c.get("must", [])), set(c.get("ok", [])), set(c.get("no", []))
            rows.append(
                {
                    "id": c["id"],
                    "split": c.get("split"),
                    "chars": len(text),
                    "loaded": len(loaded),
                    "must": len(must),
                    "must_hit": len(loaded & must),
                    "ok_hit": len(loaded & ok),
                    "leaks": sorted(loaded & no),
                    "unlabelled": sorted(loaded - must - ok - no),
                    "missed": sorted(must - loaded),
                }
            )
    with_must = [r for r in rows if r["must"]]
    loaded = sum(r["loaded"] for r in rows) or 1
    summary = {
        "cases": len(rows),
        "must_recall": round(sum(r["must_hit"] / r["must"] for r in with_must) / (len(with_must) or 1), 3),
        "leak_rate": round(sum(len(r["leaks"]) for r in rows) / loaded, 3),
        "leaks": sum(len(r["leaks"]) for r in rows),
        "precision": round(sum(r["must_hit"] + r["ok_hit"] for r in rows) / loaded, 3),
        "unlabelled": sum(len(r["unlabelled"]) for r in rows),
        "max_chars": max((r["chars"] for r in rows), default=0),
        "over_budget": sum(r["chars"] > SESSION_BUDGET for r in rows),
    }
    if verbose:
        print(f"{'case':22} {'split':5} {'chars':>5} {'loaded':>6} {'must':>7} {'leaks':>5}")
        for r in rows:
            print(
                f"{r['id']:22} {r['split'] or '':5} {r['chars']:>5} {r['loaded']:>6} "
                f"{r['must_hit']:>3}/{r['must']:<3} {len(r['leaks']):>5}"
            )
        print(json.dumps(summary))
    return {"summary": summary, "cases": rows}


def _shown(title: str, block: str) -> bool:
    """A note counts as loaded when its title is one of the block's lines."""
    return re.search(rf"^- (\*\*)?{re.escape(title)}(\*\*)?(:|$)", block, re.M) is not None


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

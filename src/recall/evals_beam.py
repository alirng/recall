"""BEAM (Tavakoli et al., ICLR 2026, arXiv 2510.27246): long chats between one user and an
assistant, probed with 20 questions per conversation across ten memory abilities.

recall indexes each conversation the way it indexes an agent's history: the conversation is cut
at user turns into chats of a few thousand tokens, each dated by its batch's time anchor. For each
probing question recall searches that index, the reader answers from the chats it returns, and a
judge scores the answer against the question's rubric with BEAM's own judge prompt (0, 0.5 or 1
per rubric item, averaged). Event ordering is scored as BEAM's report_results.py scores it:
normalised Kendall tau-b between the reference order and the answer's items, after a judge maps
each answer line to a reference item. BEAM's code judges with gpt-4.1-mini; the judge here is named
in the result and must not be the reader.
"""

# ruff: noqa: E501  (JUDGE_TEMPLATE keeps BEAM's judge prompt verbatim, long lines included)
import ast
import json
import math
import time
from datetime import datetime

from . import config
from .chats import store
from .chats.transcript import Message, Session

ABILITIES = [
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
]

ANSWER_TEMPLATE = (
    "Below are excerpts from your earlier chats with a user, each with its date. Reply to the user's new "
    "message as their assistant. Use what they told you earlier, including any preferences and standing "
    "instructions about how you should reply. When the earlier chats disagree with each other, say so. When "
    "they don't cover what is asked, say that rather than guessing.\n\n\nHistory Chats:\n\n{}\n\n"
    "Current Date: {}\nUser's new message: {}"
)
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {"type": "string", "description": "what the history says that bears on the message"},
        "reply": {"type": "string", "description": "the reply to send the user"},
    },
    "required": ["notes", "reply"],
    "additionalProperties": False,
}

# BEAM's judge prompt below is copied from github.com/mohammadtavakoli78/BEAM (src/prompts.py),
# which is distributed under this licence:
#
# MIT License
#
# Copyright (c) 2025 Mohammad Tavakoli
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# BEAM's unified_llm_judge_base_prompt (src/prompts.py), verbatim, asked once per rubric item as BEAM does.
JUDGE_TEMPLATE = """You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- RUBRIC CRITERION (what to check): {rubric_item}
- RESPONSE TO EVALUATE: {response}

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate.

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT
A compliant response must be **on-topic** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms (e.g., “$68,000”, “68k”, “68,000 USD”, or “sixty-eight thousand dollars”). Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., “itemized list”, “no citations”, “one sentence”).
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).
  - Negative: prohibited element **absent** AND response is **responsive**.

- **0.5 (Partial Compliance)**: Partially complies.
  - Positive: element present but minor inaccuracies/incomplete execution.
  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.

- **0.0 (No Compliance)**: Fails to comply.
  - Positive: required element missing or incorrect.
  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.

## EVALUATION INSTRUCTIONS:
1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).

2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:
   - **All elements** must be present for full compliance (1.0)
   - **Some elements** present indicates partial compliance (0.5)
   - **No elements** present indicates no compliance (0.0)

3. **Check Compliance**:
   - For positive requirements: Look for the presence and quality of the required element
   - For negative constraints: Look for the absence of the prohibited element

4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.

5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.

## OUTPUT FORMAT:
Return your evaluation with two fields:

{{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[detailed explanation of whether the rubric criterion was satisfied and why this justified the assigned score]"
}}

NOTE: ONLY output the json object, without any explanation before or after that"""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number", "enum": [0, 0.5, 1]}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
    "additionalProperties": False,
}

# BEAM matches each answer line to a reference event with a YES/NO equivalence call per pair
# (compute_metrics.align_with_llm); one call maps every line here.
ALIGN_TEMPLATE = (
    "Reference events, numbered:\n{reference}\n\nLines from an answer, numbered:\n{lines}\n\n"
    "For each answer line, give the number of the reference event it describes (the same event or fact), "
    "or 0 if it describes none of them or is not an event (a heading, a preamble). One entry per answer "
    "line, in order."
)
_ALIGN_SCHEMA = {
    "type": "object",
    "properties": {"matches": {"type": "array", "items": {"type": "integer"}}},
    "required": ["matches"],
    "additionalProperties": False,
}

CHUNK_CHARS = 12_000  # start a new chat at the first user turn after this much text (~3k tokens)


def _anchor_time(anchor: str | None) -> float | None:
    for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime((anchor or "").strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def load(path: str, limit: int | None = None) -> list[dict]:
    """BEAM's parquet split as a list of conversations, each with its chats cut at user turns and
    its probing questions."""
    import pyarrow.parquet as pq

    rows = pq.read_table(path).to_pylist()[:limit]
    convs = []
    for r in rows:
        chats, current, size, last_ts = [], [], 0, None
        for b, batch in enumerate(r["chat"], 1):
            if current:
                chats.append(current)
                current, size = [], 0
            for m in batch:
                text = (m.get("content") or "").strip()
                if not text:
                    continue
                if m.get("role") == "user" and current and size >= CHUNK_CHARS:
                    chats.append(current)
                    current, size = [], 0
                ts = _anchor_time(m.get("time_anchor")) or last_ts
                last_ts = ts
                current.append({"role": m.get("role") or "user", "text": text, "ts": ts, "batch": b})
                size += len(text)
        if current:
            chats.append(current)
        questions = []
        probing = ast.literal_eval(r["probing_questions"])
        for ability in ABILITIES:
            for i, q in enumerate(probing.get(ability, [])):
                questions.append({"id": f"{r['conversation_id']}-{ability}-{i}", "ability": ability, **q})
        convs.append({"id": str(r["conversation_id"]), "chats": chats, "questions": questions, "last_ts": last_ts})
    return convs


def _index(conv: dict):
    con = store.connect(":memory:")
    for k, chat in enumerate(conv["chats"]):
        # Turns within a batch share a date; a second apart keeps their order.
        t0 = chat[0]["ts"] or 0
        messages = [Message(m["role"], m["text"], (m["ts"] or t0) + i) for i, m in enumerate(chat)]
        sid = f"{conv['id']}-{k}"
        store.upsert(
            con, Session("beam", sid, "beam", started_at=messages[0].ts, updated_at=messages[-1].ts, messages=messages)
        )
    return con


def _history(conv: dict, con, question: str, top_k: int, max_tokens: int, embedder) -> tuple[str, list[int]]:
    vector = False
    if embedder is not None:
        from . import embeddings

        vector = embeddings.query_vector(question, embedder)
    rows = store.search(con, question, limit=top_k, vector=vector)
    picked, used = [], 0
    for r in rows:
        k = int(r["chat_id"].rsplit("-", 1)[1])
        body = "\n\n".join(f"{m['role']}: {m['text']}" for m in conv["chats"][k])
        if used + len(body) // 4 > max_tokens:
            break  # as LongMemEval's reader does: never skip ahead to lower-ranked chats
        used += len(body) // 4
        picked.append(k)
    picked.sort()  # chats are cut in order, so their numbers sort them by time
    blocks = []
    for n, k in enumerate(picked, 1):
        chat = conv["chats"][k]
        date = datetime.fromtimestamp(chat[0]["ts"]).strftime("%Y/%m/%d") if chat[0]["ts"] else "unknown"
        body = "\n\n".join(f"{m['role']}: {m['text']}" for m in chat)
        blocks.append(f"\n### Session {n}:\nSession Date: {date}\nSession Content:\n\n{body}\n")
    return "".join(blocks), picked


def _tau_norm(reference: list[str], system: list[str]) -> float:
    """BEAM's event_ordering_score tau_norm: Kendall tau-b over the union of items, items missing
    from a list ranked last together, mapped to 0..1."""
    union = list(dict.fromkeys(reference + system))
    tie = len(union) + 1

    def ranks(seq):
        pos = {item: i + 1 for i, item in enumerate(seq)}
        return [pos.get(u, tie) for u in union]

    x, y = ranks(reference), ranks(system)
    p = q = tx = ty = 0
    for i in range(len(union)):
        for j in range(i + 1, len(union)):
            dx, dy = x[i] - x[j], y[i] - y[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                tx += 1
            elif dy == 0:
                ty += 1
            elif (dx > 0) == (dy > 0):
                p += 1
            else:
                q += 1
    denom = math.sqrt((p + q + tx) * (p + q + ty))
    return (((p - q) / denom) + 1) / 2 if denom else 0.0


def run_beam_qa(
    path: str,
    model: str,
    judge: str | None = None,
    top_k: int = 12,
    max_tokens: int = 40_000,
    embedder=None,
    cache=None,
    db_path: str | None = None,
    workers: int = 4,
    out_path: str | None = None,
    limit: int | None = None,
) -> dict:
    """End-to-end BEAM on one split (a parquet file from huggingface.co/datasets/Mohammadta/BEAM):
    retrieve with recall, answer with `model`, score with `judge` (never the reader)."""
    from concurrent.futures import ThreadPoolExecutor

    from . import embeddings
    from .evals import ledger_qa, paths_db
    from .evals_harness import qa_judge
    from .llm import Client, from_spec

    judge = qa_judge(model, judge)
    convs = load(path, limit)
    jobs = []
    for conv in convs:
        con = _index(conv)
        if embedder is not None:
            embeddings.index_messages(con, embedder, cache=cache)
        current = datetime.fromtimestamp(conv["last_ts"]).strftime("%Y/%m/%d") if conv["last_ts"] else "unknown"
        for q in conv["questions"]:
            history, picked = _history(conv, con, q["question"], top_k, max_tokens, embedder)
            jobs.append((conv, q, history, picked, current))
        con.close()
    settings = config.load()["model"]
    client = Client(from_spec(model, settings), db_path=db_path or paths_db())
    grader = Client(from_spec(judge, settings), db_path=db_path or paths_db())

    def one(job):
        conv, q, history, picked, current = job
        started = time.time()
        before = (client.tokens_in, client.tokens_out)
        reply = client.json(
            "beam-answer",
            "You are a helpful assistant.",
            ANSWER_TEMPLATE.format(history, current, q["question"]),
            _ANSWER_SCHEMA,
        )
        seconds = time.time() - started
        response = reply["reply"]
        rubric = [str(x) for x in q.get("rubric") or []]
        verdicts = [
            grader.json(
                "beam-judge",
                "You grade answers.",
                JUDGE_TEMPLATE.format(rubric_item=item, response=response),
                _JUDGE_SCHEMA,
            )
            for item in rubric
        ]
        item_scores = [float(v.get("score", 0)) for v in verdicts]
        item_reasons = [v.get("reason", "") for v in verdicts]
        judge_score = sum(item_scores) / len(item_scores) if item_scores else 0.0
        result = {
            "id": q["id"],
            "conversation": conv["id"],
            "ability": q["ability"],
            "question": q["question"],
            "reply": response,
            "rubric": rubric,
            "item_scores": item_scores,
            "item_reasons": item_reasons,
            "judge_score": judge_score,
            "score": judge_score,
            "chats_read": picked,
            "chats_total": len(conv["chats"]),
            "history_tokens": len(history) // 4,
            "seconds": round(seconds, 1),
            "cached": (client.tokens_in, client.tokens_out) == before,
        }
        if q["ability"] == "event_ordering":
            lines = [x.strip() for x in response.split("\n") if x.strip()][:40]
            aligned = grader.json(
                "beam-align",
                "You match events.",
                ALIGN_TEMPLATE.format(
                    reference="\n".join(f"{i}. {x}" for i, x in enumerate(rubric, 1)),
                    lines="\n".join(f"{i}. {x}" for i, x in enumerate(lines, 1)),
                ),
                _ALIGN_SCHEMA,
            )
            used, system = set(), []
            for line, m in zip(lines, aligned.get("matches", []) + [0] * len(lines), strict=False):
                if 1 <= m <= len(rubric) and m not in used:
                    used.add(m)
                    system.append(rubric[m - 1])
                else:
                    system.append(line)
            result["tau_norm"] = _tau_norm(rubric, system)
            result["score"] = result["tau_norm"]
        return result

    import threading

    lock = threading.Lock()
    out = open(out_path, "w") if out_path else None  # noqa: SIM115 (closed below, after every worker)

    def safe(job):
        try:
            row = one(job)
        except Exception as e:  # one reader or judge failure must not lose the run
            _, q, _, picked, _ = job
            row = {
                "id": q["id"],
                "ability": q["ability"],
                "error": f"{type(e).__name__}: {e}"[:500],
                "score": None,
                "judge_score": None,
                "chats_read": picked,
                "history_tokens": 0,
                "seconds": 0,
                "cached": False,
            }
        if out:
            with lock:
                out.write(json.dumps(row) + "\n")
                out.flush()
        return row

    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(safe, jobs))
    if out:
        out.close()
    failed = [r for r in rows if r.get("error")]
    calls = [r for r in rows if not r.get("error")]
    by: dict[str, list[float]] = {}
    judged: dict[str, list[float]] = {}
    for c in calls:
        by.setdefault(c["ability"], []).append(c["score"])
        judged.setdefault(c["ability"], []).append(c["judge_score"])
    ability = {k: round(sum(v) / len(v), 4) for k, v in sorted(by.items())}
    tokens = sorted(c["history_tokens"] for c in calls)
    result = {
        "benchmark": "beam",
        "split": path.rsplit("/", 1)[-1],
        "model": client.provider.label,
        "judge": grader.provider.label,
        "conversations": len(convs),
        "questions": len(rows),
        "failed": len(failed),
        "failed_by_ability": {
            a: sum(1 for r in failed if r["ability"] == a) for a in ABILITIES if any(r["ability"] == a for r in failed)
        },
        "score": round(sum(ability.values()) / len(ability), 4) if ability else None,
        "score_by_ability": ability,
        "judge_score_all_abilities": round(sum(sum(v) / len(v) for v in judged.values()) / len(judged), 4)
        if judged
        else None,
        "history_tokens_mean": round(sum(tokens) / len(tokens)) if tokens else 0,
        "history_tokens_max": tokens[-1] if tokens else 0,
        "answer_seconds_median": sorted(c["seconds"] for c in calls)[len(calls) // 2] if calls else None,
        "fresh_answers": sum(not c["cached"] for c in calls),
        "answer_calls": client.calls,
        "answer_tokens_in": client.tokens_in,
        "answer_tokens_out": client.tokens_out,
        "judge_calls": grader.calls,
        "judge_tokens_in": grader.tokens_in,
        "judge_tokens_out": grader.tokens_out,
        "top_k": top_k,
        "max_tokens": max_tokens,
        "chunk_chars": CHUNK_CHARS,
    }
    print(json.dumps(result, indent=2))
    ledger_qa("beam", f"{result['split']} n={len(rows)}", f"score={result['score']}", result)
    return result

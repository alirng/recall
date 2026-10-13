"""The jit gate: does a prompt-time match (jit.py) fire only when the note or chat is relevant?

  recall eval jit --build N      replay N prompts from your history and write the pairs to label
  recall eval jit --label        a judge model labels each pair: was the item relevant?
  recall eval jit [--write]      fit the gate, check it, and with --write install it

Building replays the moment each sampled prompt was typed: only chats started before it can
match, a chat never matches itself, and only notes learned from earlier chats count. Each
pair is (prompt, candidate item) with the gate's features. Prompts are mostly the first of a
chat, plus some later ones, and the pairs are split into train and test by chat (seeded), so
a chat's prompts never sit on both sides.

Fitting: a logistic model over the features on train. Discrimination comes first: the AUC on
test, against the fused score alone. The threshold is chosen on train's out-of-fold
predictions by Learn-then-Test: thresholds from the highest down (only those with at least
MIN_FIRES fires, a choice that looks at no labels), each tested with a one-sided Clopper-
Pearson bound, stopping at the first that fails, so the false-injection rate stays at or
below ALPHA with confidence 1 - DELTA. Test then reports fire precision, fires per prompt, and
the characters a session would get when every prompt in a held-out chat is replayed.

The pairs file names real chats and holds their text: it lives in ~/.agents/recall/evals/
(jit_pairs.jsonl) and never in the repo.
"""

import hashlib
import json
import math
import os
import random
import sys
import time

from . import jit, paths

PAIRS = os.path.join(paths.EVALS, "jit_pairs.jsonl")
ALPHA = 0.15  # most false injections allowed
DELTA = 0.05  # 1 - confidence
MIN_FIRES = 20  # thresholds firing on fewer calibration pairs are not tried
TRAIN_SHARE = 0.6
SEED = 7
FEATURES = (
    "score",
    "margin",
    "bm25",
    "kw",
    "title",
    "all",
    "project",
    "age",
    "words",
    "lead",
    "note",
    "matches",
    "file",
)
DENSE = jit.DENSE
LATER_SHARE = 0.25  # of sampled prompts, ones typed later in a chat rather than first
JUDGE_BATCH = 8

JUDGE_SYSTEM = (
    "You label data for a memory tool for coding agents. When a developer types a prompt, the tool may "
    "show the agent one earlier chat or memory note from the developer's own history. You decide, for "
    "each (prompt, item) pair, whether showing that item at that moment would be relevant."
)
JUDGE_RULES = """Relevant (true) means the item is about the same task, feature, bug, error, file area or
decision as the prompt, so what it records (what was done, decided, learned, or how it ended) bears
directly on how the agent should handle this prompt. An earlier attempt at the same task, the chat
where the thing being asked about was built, or a note stating a convention or decision the prompt
touches all count.

Not relevant (false): the same project but a different topic; overlap in generic words only;
loosely related background; a note too general to change what the agent does here.

Judge each pair on its own. Reply with JSON: {"labels": [{"id": "<pair id>", "relevant": true|false,
"why": "<at most 15 words>"}]} with one entry per pair, in order."""


# --- building pairs ---------------------------------------------------------------------------


def build(con, n: int, out: str = PAIRS, vectors: bool = True) -> int:
    """Replay n sampled prompts and write their candidate pairs, unlabelled."""
    from .memory.notes import NoteStore

    rng = random.Random(SEED)
    started = {r[0]: r[1] or 0 for r in con.execute("select id, started_at from sessions")}
    notes = NoteStore().load()
    sources = {i: [started.get(s) for s in nt.sources] for i, nt in notes.items()}
    note_from = {i: [s for s in nt.sources] for i, nt in notes.items()}
    embedder = _embedder() if vectors else None
    queries = _sample(con, rng, n)
    written = 0
    with open(out, "w", encoding="utf-8") as f:
        for q in queries:
            allowed = {
                i for i, ts in sources.items() if q["chat"] not in note_from[i] and any(t and t < q["at"] for t in ts)
            }
            vec = _vector(embedder, q["prompt"]) if embedder else None
            found = jit.candidates(
                con, q["prompt"], q["cwd"], own=[q["chat"]], until=q["at"], now=q["at"], allowed_notes=allowed,
                vector=vec,
            )  # fmt: skip
            for c in found:
                pair = {
                    "id": hashlib.sha1(f"{q['chat']}:{q['seq']}:{c.id}".encode()).hexdigest()[:12],
                    "chat": q["chat"],
                    "seq": q["seq"],
                    "split": split_of(q["chat"]),
                    "first": q["seq"] == 0,
                    "at": q["at"],
                    "cwd": q["cwd"],
                    "prompt": q["prompt"][:1500],
                    "kind": c.kind,
                    "item": c.id,
                    "line": c.line,
                    "features": {k: round(v, 5) for k, v in c.features.items()},
                    "material": _material(con, c, notes),
                }
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1
    return written


def refresh(con, path: str) -> dict:
    """Recompute the features of the pairs in a file, keeping their labels, after the gate's
    features change. A pair whose item the replay no longer finds is kept, marked stale."""
    from .memory.notes import NoteStore

    pairs = _read(path)
    started = {r[0]: r[1] or 0 for r in con.execute("select id, started_at from sessions")}
    notes = NoteStore().load()
    found_by: dict[tuple, dict] = {}
    for key in dict.fromkeys((p["chat"], p["seq"]) for p in pairs):
        p = next(x for x in pairs if (x["chat"], x["seq"]) == key)
        allowed = {
            i
            for i, n in notes.items()
            if p["chat"] not in n.sources and any(started.get(s) and started[s] < p["at"] for s in n.sources)
        }
        items = jit.candidates(
            con, p["prompt"], p["cwd"], own=[p["chat"]], until=p["at"], now=p["at"], allowed_notes=allowed,
            vector=jit.embed_query(p["prompt"]),
        )  # fmt: skip
        found_by[key] = {c.id: c for c in items}
    stale = 0
    for p in pairs:
        c = found_by[(p["chat"], p["seq"])].get(p["item"])
        if c:
            p["features"] = {k: round(v, 5) for k, v in c.features.items()}
            p.pop("stale", None)
        else:
            p["stale"] = True
            stale += 1
    _write(path, pairs)
    return {"pairs": len(pairs), "stale": stale}


def _sample(con, rng, n: int) -> list[dict]:
    """Prompts people typed: mostly the first of a chat, some later ones, from chats with
    some history before them."""
    rows = con.execute(
        """select s.id, s.project, s.started_at, m.seq, m.ts, m.text from sessions s
           join messages m on m.session_id = s.id and m.role = 'user'
           where s.origin = 'interactive' and s.started_at > (select min(started_at) + 30*86400 from sessions)"""
    ).fetchall()
    first, later = [], []
    for r in rows:
        prompt = jit.askable(r[5] or "")
        if not prompt:
            continue
        q = {"chat": r[0], "cwd": r[1], "seq": r[3], "at": (r[4] or r[2] or 0) - 1, "prompt": prompt}
        if r[3] == 0:
            first.append(q)
        elif len(jit.content_words(prompt)) >= 6:
            later.append(q)
    rng.shuffle(first)
    rng.shuffle(later)
    n_later = int(n * LATER_SHARE)
    return first[: n - n_later] + later[:n_later]


def split_of(chat: str) -> str:
    h = int(hashlib.sha1(f"{SEED}:{chat}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "train" if h < TRAIN_SHARE else "test"


def _material(con, c, notes) -> dict:
    """What the judge reads about an item: a chat's title, date, opening and ending, or a note."""
    if c.kind == "note":
        n = notes.get(c.id)
        return {"title": n.title, "type": n.type, "body": n.body[:900]} if n else {"line": c.line}
    s = con.execute("select title, project, updated_at, first_prompt from sessions where id = ?", (c.id,)).fetchone()
    last = con.execute(
        "select text from messages where session_id = ? and role = 'assistant' order by seq desc limit 1", (c.id,)
    ).fetchone()
    return {
        "title": s[0],
        "project": os.path.basename(s[1] or "") or None,
        "date": jit._day(s[2]),
        "opening": (s[3] or "")[:600],
        "ending": jit._gist(last[0], 500) if last else "",
    }


def _embedder():
    from . import embeddings

    return embeddings.configured()


def _vector(embedder, text: str):
    return jit.embed_query(text)


# --- labelling --------------------------------------------------------------------------------


def label(path: str, judge: str, workers: int = 2) -> dict:
    """Label every unlabelled pair with the judge, in batches; writes the labels back."""
    from concurrent.futures import ThreadPoolExecutor

    from . import evals_harness as h

    pairs = _read(path)
    todo = [p for p in pairs if "relevant" not in p]
    client = h.judge_client(judge)
    batches = [todo[i : i + JUDGE_BATCH] for i in range(0, len(todo), JUDGE_BATCH)]
    schema = {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "relevant": {"type": "boolean"},
                        "why": {"type": "string"},
                    },
                    "required": ["id", "relevant"],
                },
            }
        },
        "required": ["labels"],
    }

    def one(batch):
        prompt = JUDGE_RULES + "\n\n" + "\n\n".join(_judge_block(p) for p in batch)
        try:
            out = client.json("jit_label", JUDGE_SYSTEM, prompt, schema)
        except Exception as e:
            print(f"judge failed on a batch: {e}", file=sys.stderr)
            return {}
        return {x["id"]: x for x in out.get("labels", []) if isinstance(x, dict) and "id" in x}

    got: dict = {}
    with ThreadPoolExecutor(workers) as pool:
        for i, found in enumerate(pool.map(one, batches)):
            got |= found
            print(f"labelled batch {i + 1}/{len(batches)}", file=sys.stderr)
    for p in pairs:
        if p["id"] in got and "relevant" not in p:
            p["relevant"] = bool(got[p["id"]]["relevant"])
            p["why"] = got[p["id"]].get("why", "")
            p["judge"] = judge
    _write(path, pairs)
    return {"pairs": len(pairs), "labelled": sum("relevant" in p for p in pairs), "new": len(got)}


def _judge_block(p: dict) -> str:
    m = p["material"]
    if p["kind"] == "note":
        item = f"Memory note ({m.get('type')}): {m.get('title')}\n{m.get('body', m.get('line', ''))}"
    else:
        item = (
            f'Earlier chat "{m.get("title")}" ({m.get("date")}, folder {m.get("project")})\n'
            f"It opened with: {m.get('opening')}\nIt ended with: {m.get('ending')}"
        )
    where = os.path.basename(p.get("cwd") or "") or "no folder"
    return f"### Pair {p['id']}\nPrompt (typed in {where}):\n{p['prompt']}\n\nItem:\n{item}"


# --- fitting and checking -----------------------------------------------------------------


def fit(pairs: list[dict], features: tuple[str, ...], l2: float = 1.0) -> dict:
    """Logistic regression by Newton's method on standardised features, returned in the raw
    features' scale so jit.probability can apply it directly."""
    import numpy as np

    X = np.array([[p["features"].get(k, 0.0) for k in features] for p in pairs], dtype=float)
    y = np.array([1.0 if p["relevant"] else 0.0 for p in pairs])
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = np.zeros(Z.shape[1])
    reg = np.eye(Z.shape[1]) * l2
    reg[0, 0] = 0.0
    for _ in range(50):
        p = 1 / (1 + np.exp(-Z @ w))
        g = Z.T @ (p - y) + reg @ w
        H = Z.T @ (Z * (p * (1 - p))[:, None]) + reg
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    weights = {k: float(w[i + 1] / sd[i]) for i, k in enumerate(features)}
    bias = float(w[0] - sum(w[i + 1] * mu[i] / sd[i] for i in range(len(features))))
    return {"bias": bias, "weights": weights}


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Area under the ROC curve (Mann-Whitney, ties count half)."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def auc_ci(scores: list[float], labels: list[bool], groups: list[str], reps: int = 1000) -> list[float]:
    """95% bootstrap interval for the AUC, resampling chats (a chat's pairs move together)."""
    rng = random.Random(SEED)
    by: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    keys = list(by)
    out = []
    for _ in range(reps):
        idx = [i for g in (rng.choice(keys) for _ in keys) for i in by[g]]
        a = auc([scores[i] for i in idx], [labels[i] for i in idx])
        if a is not None:
            out.append(a)
    out.sort()
    return [round(out[int(0.025 * len(out))], 3), round(out[int(0.975 * len(out)) - 1], 3)] if out else []


def upper(k: int, n: int, delta: float = DELTA) -> float:
    """One-sided Clopper-Pearson upper bound on a rate seen k times in n."""
    if n == 0:
        return 1.0
    if k >= n:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > delta:
            lo = mid
        else:
            hi = mid
    return hi


def lower(k: int, n: int, delta: float = DELTA) -> float:
    return 1 - upper(n - k, n, delta) if n else 0.0


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0 if k < n else 1.0
    lp, lq = math.log(p), math.log1p(-p)
    return sum(
        math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
        for i in range(k + 1)
    )


def threshold(probs: list[float], labels: list[bool]) -> dict:
    """Learn-then-Test over thresholds from the highest down, fixed sequence: the lowest
    threshold whose false-injection rate has a Clopper-Pearson upper bound at or below ALPHA,
    trying only thresholds that fire on at least MIN_FIRES pairs (that choice sees no labels).

    When no threshold passes, the bound cannot be promised from this data, and the gate falls
    back to a conservative one: of the thresholds firing at least MIN_FIRES times, the one with
    the fewest false injections per fire (the highest on ties). Its bound is reported as is."""
    order = sorted(set(probs), reverse=True)
    chosen, path, tried = None, [], []
    failed = False
    for t in order:
        fired = [y for p, y in zip(probs, labels, strict=True) if p >= t]
        if len(fired) < MIN_FIRES:
            continue
        bad = sum(1 for y in fired if not y)
        u = upper(bad, len(fired))
        step = {"t": round(t, 4), "fires": len(fired), "bad": bad, "upper": round(u, 3)}
        tried.append(step)
        if not failed:
            path.append(step)
            if u > ALPHA:
                failed = True
            else:
                chosen = t
    if chosen is not None:
        return {"threshold": chosen, "certified": True, "path": path}
    if not tried:
        return {"threshold": None, "certified": False, "path": path}
    best = min(tried, key=lambda x: (x["bad"] / x["fires"], -x["t"]))
    return {"threshold": best["t"], "certified": False, "path": path, "fallback": best}


def oof(pairs: list[dict], features: tuple[str, ...], folds: int = 5) -> list[float]:
    """Out-of-fold probabilities on the training pairs, folds split by chat."""
    out = [0.0] * len(pairs)
    for k in range(folds):
        held = [i for i, p in enumerate(pairs) if _fold(p["chat"], folds) == k]
        rest = [p for i, p in enumerate(pairs) if _fold(p["chat"], folds) != k]
        g = fit(rest, features)
        for i in held:
            out[i] = jit.probability(pairs[i]["features"], g)
    return out


def _fold(chat: str, folds: int) -> int:
    return int(hashlib.sha1(f"fold:{chat}".encode()).hexdigest()[:8], 16) % folds


def report(path: str, features: tuple[str, ...] = FEATURES) -> dict:
    pairs = [p for p in _read(path) if "relevant" in p and not p.get("stale")]
    train = [p for p in pairs if p["split"] == "train"]
    test = [p for p in pairs if p["split"] == "test"]
    gate = fit(train, features)
    probs = oof(train, features)
    calib = threshold(probs, [p["relevant"] for p in train])
    t = calib["threshold"]
    test_p = [jit.probability(p["features"], gate) for p in test]
    labels = [p["relevant"] for p in test]
    out = {
        "pairs": {
            "train": len(train),
            "test": len(test),
            "relevant_share": round(sum(labels) / max(1, len(labels)), 3),
        },
        "features": list(features),
        "auc": {
            "gate": _auc_row(test_p, labels, test),
            "score_only": _auc_row([p["features"]["score"] for p in test], labels, test),
            "chats": _auc_row(*_kind(test_p, test, "chat")),
            "notes": _auc_row(*_kind(test_p, test, "note")),
        },
        "threshold": t,
        "certified": calib["certified"],
        "calibration_path": calib["path"][-6:],
        "fallback": calib.get("fallback"),
        "gate": gate | {"threshold": t if t is not None else 1.01},
    }
    if t is not None:
        fired = [(p, y) for p, y in zip(test_p, labels, strict=True) if p >= t]
        good = sum(1 for _, y in fired if y)
        prompts = {(q["chat"], q["seq"]) for q in test}
        hits = [(y, q["kind"]) for p, y, q in zip(test_p, labels, test, strict=True) if p >= t]
        out["test"] = {
            "fires": len(fired),
            "precision": round(good / len(fired), 3) if fired else None,
            "precision_ci95": _ci(good, len(fired)),
            "false_injection_rate": round(1 - good / len(fired), 3) if fired else None,
            "fires_per_prompt": round(len(fired) / max(1, len(prompts)), 3),
            "recall_of_relevant": round(good / max(1, sum(labels)), 3),
            "by_kind": {
                k: {
                    "fires": sum(1 for _, kind in hits if kind == k),
                    "good": sum(1 for y, kind in hits if y and kind == k),
                }
                for k in ("chat", "note")
            },
        }
    return out


def _ci(good: int, n: int) -> list[float] | None:
    """Two-sided 95% Clopper-Pearson interval for a share good / n."""
    return [round(lower(good, n, 0.025), 3), round(upper(good, n, 0.025), 3)] if n else None


def _auc_row(scores, labels, rows) -> dict:
    a = auc(scores, labels)
    return {
        "auc": round(a, 3) if a is not None else None,
        "ci95": auc_ci(scores, labels, [r["chat"] for r in rows]),
        "n": len(rows),
    }


def _kind(probs, rows, kind):
    keep = [i for i, r in enumerate(rows) if r["kind"] == kind]
    return [probs[i] for i in keep], [rows[i]["relevant"] for i in keep], [rows[i] for i in keep]


def session_chars(con, path: str, gate: dict, limit: int = 40) -> dict:
    """Replay every prompt of held-out chats through the gate, with the per-session budgets
    and once-per-session rule, and count the characters each session would have received."""
    chats = sorted({p["chat"] for p in _read(path) if p["split"] == "test"})[:limit]
    per_session, fires = [], []
    for chat in chats:
        s = con.execute("select project from sessions where id = ?", (chat,)).fetchone()
        seen: set[str] = set()
        chars = n = 0
        for ts, text in con.execute(
            "select ts, text from messages where session_id = ? and role = 'user' order by seq", (chat,)
        ):
            query = jit.askable(text or "")
            if not query:
                continue
            at = (ts or time.time()) - 1
            vec = jit.embed_query(query) if any(k in gate["weights"] for k in jit.DENSE) else None
            found = jit.candidates(con, query, s[0] if s else None, own=[chat], until=at, now=at, vector=vec)
            picked = [c for c in found if jit.probability(c.features, gate) >= gate["threshold"] and c.id not in seen]
            picked = picked[: jit.MAX_ITEMS]
            if not picked:
                continue
            block = jit.render(picked)
            if chars + len(block) > jit.WINDOW_CHARS or n >= jit.WINDOW_FIRES:
                continue
            seen |= {c.id for c in picked}
            chars += len(block)
            n += 1
        per_session.append(chars)
        fires.append(n)
    import statistics

    return {
        "sessions": len(per_session),
        "chars_mean": round(statistics.fmean(per_session), 1) if per_session else None,
        "chars_median": statistics.median(per_session) if per_session else None,
        "chars_max": max(per_session, default=None),
        "fires_mean": round(statistics.fmean(fires), 2) if fires else None,
        "sessions_with_a_fire": sum(1 for f in fires if f),
    }


# --- the command ------------------------------------------------------------------------------


def from_args(a) -> None:
    from .chats import store

    path = a.cases or PAIRS
    if a.build:
        con = store.connect()
        n = build(con, a.build, path, vectors=not a.keyword)
        print(f"wrote {n} pairs to {path}; label them with `recall eval jit --label`")
        return
    if a.refresh:
        print(json.dumps(refresh(store.connect(), path), indent=2))
        return
    if a.label:
        print(json.dumps(label(path, a.judge or "claude:claude-sonnet-5-5", a.workers), indent=2))
        return
    feats = FEATURES + (() if a.keyword else DENSE)
    out = report(path, feats)
    if out.get("threshold") is not None:
        out["session_chars"] = session_chars(store.connect(), path, out["gate"])
    print(json.dumps(out, indent=2))
    if a.write:
        if out.get("threshold") is None:
            sys.exit("too few labelled pairs to choose a threshold; the gate stays closed")
        os.makedirs(paths.HOME, exist_ok=True)
        with open(jit.GATE_FILE, "w") as f:
            json.dump(out["gate"] | {"fitted": time.strftime("%Y-%m-%d"), "pairs": out["pairs"]}, f, indent=2)
        print(f"gate written to {jit.GATE_FILE}")


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)

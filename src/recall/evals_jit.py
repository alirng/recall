"""The jit gate: does a prompt-time match (jit.py) fire only when the note or chat is relevant?

  recall eval jit --build N      replay N prompts from your history and write the pairs to label
  recall eval jit --label        a judge model labels each pair: was the item relevant?
  recall eval jit [--write]      fit the gate, check it, and with --write install it
  recall eval jit --live         label the prompt fires in the usage log: live precision

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
OFFLINE_WAIT = 5.0  # seconds to wait for an embedding when replaying offline (hooks wait 0.25)

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
    queries = _sample(con, rng, n)
    written = 0
    with open(out, "w", encoding="utf-8") as f:
        for q in queries:
            allowed = {
                i for i, ts in sources.items() if q["chat"] not in note_from[i] and any(t and t < q["at"] for t in ts)
            }
            vec = jit.embed_query(q["prompt"], timeout=OFFLINE_WAIT) if vectors else None
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
            vector=jit.embed_query(p["prompt"], timeout=OFFLINE_WAIT),
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
    if c.kind == "rule":
        return {"line": c.line}
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


# --- live fires ----------------------------------------------------------------------------------

LIVE = os.path.join(paths.EVALS, "jit_live.jsonl")


def harvest(con, path: str = LIVE, since: float | None = None) -> int:
    """Turn the fires in the usage log into pairs to label, once their chat is indexed: the
    request each answered (the session's user message just before it) and each injected item.
    Prompt fires get their gate features replayed at that moment, so they can refit the gate;
    read fires name the file the agent had just read. Pairs already in the file are kept."""
    from datetime import datetime

    from . import rules, usage
    from .memory.notes import NoteStore

    have = {p["id"]: p for p in (_read(path) if os.path.exists(path) else [])}
    notes = NoteStore().load()
    for e in usage.entries(since):
        args = e.get("args") or {}
        event = args.get("event")
        if e.get("tool") != "jit" or event not in ("prompt", "read") or not e.get("ids"):
            continue
        chat, at = f"{e.get('client')}:{e.get('session')}", datetime.fromisoformat(e["at"]).timestamp()
        row = con.execute(
            "select m.text, m.ts, m.seq, s.project from messages m join sessions s on s.id = m.session_id "
            "where m.session_id = ? and m.role = 'user' and m.ts <= ? order by m.ts desc limit 1",
            (chat, at + 5),
        ).fetchone()
        prompt = jit.askable(row[0]) if row else None
        if not prompt:
            continue  # not indexed yet, or the message is not one a person typed
        if event == "prompt":
            vec = jit.embed_query(prompt, timeout=OFFLINE_WAIT)
            found = jit.candidates(con, prompt, row[3], own=[chat], until=row[1] - 1, now=row[1], vector=vec)
        elif args.get("file"):  # logged relative to the repo of the chat's folder
            root = rules.repo_root(row[3]) or row[3] or ""
            full = os.path.join(root, args["file"])
            found = jit.file_items(con, e.get("client") or "claude", full, row[3], [chat])
        else:
            continue
        for c in found:
            pid = hashlib.sha1(f"live:{event}:{chat}:{row[2]}:{c.id}".encode()).hexdigest()[:12]
            if c.id in e["ids"] and pid not in have:
                have[pid] = {
                    "id": pid, "chat": chat, "seq": row[2], "split": "live", "event": event,
                    "file": args.get("file"), "first": row[2] == 0, "at": row[1], "cwd": row[3],
                    "prompt": prompt[:1500], "kind": c.kind, "item": c.id, "line": c.line,
                    "features": {k: round(v, 5) for k, v in c.features.items()}, "fired_at": e["at"],
                    "p": e.get("p"), "material": _material(con, c, notes),
                }  # fmt: skip
    _write(path, list(have.values()))
    return len(have)


def live_precision(path: str = LIVE) -> dict:
    """How many labelled live fires were relevant, with a 95% Clopper-Pearson interval."""
    rows = [p for p in _read(path) if "relevant" in p] if os.path.exists(path) else []
    good = sum(1 for p in rows if p["relevant"])
    return {
        "fires_labelled": len(rows),
        "precision": round(good / len(rows), 3) if rows else None,
        "precision_ci95": _ci(good, len(rows)),
        "by_event": {
            ev: {"fires": len(mine), "relevant": sum(1 for p in mine if p["relevant"])}
            for ev in ("prompt", "read")
            if (mine := [p for p in rows if p.get("event", "prompt") == ev])
        },
        "by_kind": {k: sum(1 for p in rows if p["kind"] == k) for k in ("chat", "note", "rule")},
    }


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
    if p["kind"] == "rule":
        item = f"Repository rule: {m.get('line')}"
    elif p["kind"] == "note":
        item = f"Memory note ({m.get('type')}): {m.get('title')}\n{m.get('body', m.get('line', ''))}"
    else:
        item = (
            f'Earlier chat "{m.get("title")}" ({m.get("date")}, folder {m.get("project")})\n'
            f"It opened with: {m.get('opening')}\nIt ended with: {m.get('ending')}"
        )
    where = os.path.basename(p.get("cwd") or "") or "no folder"
    read = (
        f"\n\nWorking on that, the agent had just read the file {p['file']}; the item was shown beside it, "
        "so judge whether it bears on that work on that file."
        if p.get("event") == "read"
        else ""
    )
    return f"### Pair {p['id']}\nPrompt (typed in {where}):\n{p['prompt']}{read}\n\nItem:\n{item}"


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

    When none passes, the bound cannot be promised from this data, and the threshold is the
    first in that sequence: the highest that fires on MIN_FIRES calibration pairs. That choice
    sees no labels either, and its bound is reported as it stands."""
    path, chosen, first = [], None, None
    for t in sorted(set(probs), reverse=True):
        fired = [y for p, y in zip(probs, labels, strict=True) if p >= t]
        if len(fired) < MIN_FIRES:
            continue
        bad = sum(1 for y in fired if not y)
        u = upper(bad, len(fired))
        path.append({"t": round(t, 4), "fires": len(fired), "bad": bad, "upper": round(u, 3)})
        first = t if first is None else first
        if u > ALPHA:
            break
        chosen = t
    if chosen is not None:
        return {"threshold": chosen, "certified": True, "path": path}
    return {"threshold": first, "certified": False, "path": path}


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


GATE_FEATURES = {"dense": FEATURES + DENSE, "keyword": FEATURES}


def _uses(kind: str, p: dict) -> bool:
    """The pairs a gate decides: the meaning gate those whose item has a stored vector."""
    return kind == "keyword" or bool(p["features"].get("has_vec"))


def report(path: str) -> dict:
    """Fit both gates on train, check them on test, and check the decision jit makes with
    them: the meaning gate for items with a stored vector, the keyword gate for the rest
    (embeddings on), or the keyword gate for every item (embeddings off)."""
    pairs = [p for p in _read(path) if "relevant" in p and not p.get("stale")]
    train = [p for p in pairs if p["split"] == "train"]
    test = [p for p in pairs if p["split"] == "test"]
    out: dict = {
        "pairs": {"train": len(train), "test": len(test), "relevant_share": round(_share(test), 3)},
        "auc_score_only": _auc_row([p["features"]["score"] for p in test], [p["relevant"] for p in test], test),
        "gates": {},
    }
    gates = {}
    for kind, feats in GATE_FEATURES.items():
        tr = [p for p in train if _uses(kind, p)]
        te = [p for p in test if _uses(kind, p)]
        g = fit(tr, feats)
        calib = threshold(oof(tr, feats), [p["relevant"] for p in tr])
        gates[kind] = g | {"threshold": calib["threshold"] if calib["threshold"] is not None else 1.01}
        probs = [jit.probability(p["features"], g) for p in te]
        out["gates"][kind] = {
            "features": list(feats),
            "pairs": {"train": len(tr), "test": len(te)},
            "auc": _auc_row(probs, [p["relevant"] for p in te], te),
            "auc_chats": _auc_row(*_kind(probs, te, "chat")),
            "auc_notes": _auc_row(*_kind(probs, te, "note")),
            "threshold": calib["threshold"],
            "certified": calib["certified"],
            "calibration_path": calib["path"][:3] + (["…"] if len(calib["path"]) > 6 else []) + calib["path"][-3:]
            if len(calib["path"]) > 6
            else calib["path"],
            "gate": gates[kind],
        }
    for mode, route in (("embeddings_on", True), ("embeddings_off", False)):
        fired = [p for p in test if _passes(p, gates, route)]
        out[mode] = _fires(fired, test)
    out["gate_file"] = gates
    return out


def _passes(p: dict, gates: dict, embeddings: bool) -> bool:
    g = gates["dense"] if embeddings and p["features"].get("has_vec") else gates["keyword"]
    return jit.probability(p["features"], g) >= g["threshold"]


def _fires(fired: list[dict], test: list[dict]) -> dict:
    good = sum(1 for p in fired if p["relevant"])
    return {
        "fires": len(fired),
        "precision": round(good / len(fired), 3) if fired else None,
        "precision_ci95": _ci(good, len(fired)),
        "fires_per_prompt": round(len(fired) / max(1, len({(q["chat"], q["seq"]) for q in test})), 3),
        "recall_of_relevant": round(good / max(1, sum(p["relevant"] for p in test)), 3),
        "by_kind": {k: f"{sum(1 for p in fired if p['kind'] == k and p['relevant'])}/"
                    f"{sum(1 for p in fired if p['kind'] == k)}" for k in ("chat", "note")},
    }  # fmt: skip


def _share(rows: list[dict]) -> float:
    return sum(p["relevant"] for p in rows) / max(1, len(rows))


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


def session_chars(con, path: str, gates: dict, limit: int = 40, embeddings: bool = True) -> dict:
    """Replay every prompt of held-out chats through the gates, with the per-session budgets
    and once-per-session rule, and count the characters each session would have received."""
    import statistics

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
            vec = jit.embed_query(query, timeout=OFFLINE_WAIT) if embeddings else None
            found = jit.candidates(con, query, s[0] if s else None, own=[chat], until=at, now=at, vector=vec)
            picked = [c for c in found if _passes({"features": c.features}, gates, embeddings) and c.id not in seen]
            block = jit.render(picked[: jit.MAX_ITEMS])
            if not block or chars + len(block) > jit.WINDOW_CHARS or n >= jit.WINDOW_FIRES:
                continue
            seen |= {c.id for c in picked[: jit.MAX_ITEMS]}
            chars += len(block)
            n += 1
        per_session.append(chars)
        fires.append(n)
    return {
        "sessions": len(per_session),
        "chars_mean": round(statistics.fmean(per_session), 1) if per_session else None,
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
    if a.live:
        live = a.cases or LIVE
        n = harvest(store.connect(), live, store.parse_since(a.since) if getattr(a, "since", None) else None)
        print(f"{n} live fires in {live}", file=sys.stderr)
        label(live, a.judge or "claude:claude-sonnet-5-5", a.workers)
        print(json.dumps(live_precision(live), indent=2))
        return
    if a.refresh:
        print(json.dumps(refresh(store.connect(), path), indent=2))
        return
    if a.label:
        print(json.dumps(label(path, a.judge or "claude:claude-sonnet-5-5", a.workers), indent=2))
        return
    out = report(path)
    con = store.connect()
    out["session_chars"] = {
        "embeddings_on": session_chars(con, path, out["gate_file"], embeddings=True),
        "embeddings_off": session_chars(con, path, out["gate_file"], embeddings=False),
    }
    print(json.dumps(out, indent=2))
    if a.write:
        if any(g["threshold"] is None for g in out["gates"].values()):
            sys.exit("too few labelled pairs to choose a threshold; the gates stay as they are")
        uncertified = [k for k, g in out["gates"].items() if not g["certified"]]
        if uncertified:
            print(
                f"warning: the {' and '.join(uncertified)} gate cannot promise at most {ALPHA:.0%} false injections "
                f"with {1 - DELTA:.0%} confidence on these pairs; it uses the highest threshold that fires on "
                f"{MIN_FIRES} of them instead. Label more pairs (--build, --label, or --live) to certify it.",
                file=sys.stderr,
            )
        os.makedirs(paths.HOME, exist_ok=True)
        with open(jit.GATE_FILE, "w") as f:
            stamp = {"fitted": time.strftime("%Y-%m-%d"), "pairs": out["pairs"], "uncertified": uncertified}
            json.dump(out["gate_file"] | stamp, f, indent=2)
        print(f"gates written to {jit.GATE_FILE}")


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)

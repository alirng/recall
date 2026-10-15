"""Paired comparison of eval runs: is a difference real, noise, or too small to see?

  recall eval compare RUN [RUN...] [vs RUN [RUN...]] [--metric M] [--split S] [--tag T]
      [--names A,B] [--flips] [--target D] [--json]

Each arm is one or more run folders of the same eval: a harness run folder (cases.jsonl, and
test/cases.jsonl for test cases) or a folder from the shared run wrapper, whose output.txt names
the harness folder. RUN#2 (or RUN#1,3) keeps only those repeats of a run, so a run's repeats can
be compared with each other: what no change looks like. Every (run folder, repeat) is one
replicate of the arm. Cases are paired by id across arms; a case's value in an arm is its mean
over the arm's replicates, so extra repeats of a few cases (see --flips) sharpen those cases
without reweighting the rest.

Per metric it reports:
  base, cand, Δ     the arm scores over the cases both arms scored, and cand minus base
  95% CI            paired bootstrap over cases (2,000 resamples, fixed seed): does the gain
                    hold for cases like these, run noise included
  W/L/T             cases where cand is better than, worse than or equal to base (for a
                    metric with no better direction, above or below)
  detectable        the smallest difference these runs would detect 80% of the time: 2.8 x the
                    bootstrap standard error, or the replicate spread's floor where that is
                    larger (noise one run shares across cases, which the bootstrap misses).
                    The same rule gives the one-arm "vs an identical one" floors. The spread's
                    floor uses a Student t quantile in place of 1.96, as the spread comes from
                    few replicates
  verdict           better or worse when the CI excludes 0 and Δ is beyond the run-to-run
                    spread of the replicates; else within noise. None under 10 paired cases
                    ("too few cases"), and "run noise unmeasured" in place of better or worse
                    when an arm has fewer than 3 full replicates
  run-to-run        each replicate's score on the paired cases, the spread per arm, and the
                    differences between replicates of the same arm (what no change looks like)
  repeats needed    how many replicates per arm would make the observed Δ (or --target)
                    detectable, from a split of the variance into case sampling, which repeats
                    cannot shrink, and run noise, which they can
--flips lists the cases whose outcome changes between replicates or between arms, with the
--case flags that rerun only those. With one arm (no "vs") it reports that arm's score, spread
and flips. Pure standard library: this file also runs on its own, outside the package.
"""

import argparse
import itertools
import json
import math
import os
import random
import re
import statistics
import sys

SEED = 7
RESAMPLES = 2000
Z80 = 2.8  # 1.96 (two-sided 5%) + 0.84 (80% power)
Z_POWER = 0.84  # the 80% power part of Z80
EPS = 1e-9
MIN_CASES = 10  # fewer paired cases than this give no better/worse verdict
MIN_REPLICATES = 3  # full replicates an arm needs for its run noise to count as measured
# Two-sided 95% Student t quantile by degrees of freedom: what 1.96 becomes when a spread is
# estimated from few replicates.
# fmt: off
T95 = dict(
    enumerate(
        (
            12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
            2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
        ),
        1,
    )
)
# fmt: on

# Per eval: how a row maps to its case (the unit the bootstrap resamples), and each metric as
# (numerator key, denominator key or None, how cases combine, direction, only rows where key).
# "ratio": sum over cases of the numerator over the sum of the denominator (a pooled rate, as the
# eval's own summary pools it); "mean": the mean over cases of each case's own value.
# direction: 1 higher is better, -1 lower is better, 0 neither.
SPECS = [
    (
        "memorycode-extract",
        "id",
        {
            "recall": ("found", "conventions", "ratio", 1, None),
            "stale": ("stale", "replaced", "ratio", -1, None),
            "updates": ("updates_ok", "updated", "ratio", 1, None),
            "lost": ("lost", "extracted_latest", "ratio", -1, None),
            "filler_notes": ("notes_filler", "notes", "ratio", -1, None),
            "notes": ("notes", None, "mean", 0, None),
        },
    ),
    (
        "memorycode-agent",
        "history",
        {
            "accuracy": ("score", None, "mean", 1, None),
            "checks_updated": ("updated_ok", "updated_n", "ratio", 1, None),
            "tokens_in": ("tokens_in", None, "mean", -1, None),
            "tool_calls": ("tool_calls", None, "mean", 0, None),
        },
    ),
    (
        "qa_v2",
        "id",
        {"accuracy": ("correct", None, "mean", 1, None), "tokens_in": ("tokens_in", None, "mean", -1, None)},
    ),
    (
        "memory_v2",
        "id",
        {
            "recall": ("found", "expected", "ratio", 1, None),
            "precision": ("good", "memories", "ratio", 1, None),
            "note_recall": ("note_found", "expected", "ratio", 1, "folded"),
            "updates": ("updates_ok", "updates", "ratio", 1, None),
            "leaks": ("leaks", None, "mean", -1, None),
            "note_leaks": ("note_leaks", None, "mean", -1, None),
        },
    ),
    (
        "swecb-lite-agent",
        "id",
        {"hit": ("hit", None, "mean", 1, None), "tokens_in": ("tokens_in", None, "mean", -1, None)},
    ),
    (
        "swecb-full-agent",
        "id",
        {"hit": ("hit", None, "mean", 1, None), "tokens_in": ("tokens_in", None, "mean", -1, None)},
    ),
    (
        "search_v2",
        "id",
        {
            "hit@5": ("hit@5", None, "mean", 1, None),
            "hit@1": ("hit@1", None, "mean", 1, None),
            "mrr": ("rr", None, "mean", 1, None),
        },
    ),
    (
        "swecb",
        "id",
        {
            "hit@5": ("hit@5", None, "mean", 1, None),
            "hit@10": ("hit@10", None, "mean", 1, None),
            "mrr": ("rr", None, "mean", 1, None),
        },
    ),
    ("amb", "id", {"hit@5": ("hit@5", None, "mean", 1, None), "mrr": ("rr", None, "mean", 1, None)}),
    (
        "search",
        "id",
        {
            "hit@5": ("hit@5", None, "mean", 1, None),
            "hit@1": ("hit@1", None, "mean", 1, None),
            "mrr": ("rr", None, "mean", 1, None),
        },
    ),
    (
        "memory_v1",
        "id",
        {"recall": ("found", "expected", "ratio", 1, None), "leaks": ("leaks", None, "mean", -1, None)},
    ),
    ("longmemeval-qa", "id", {"accuracy": ("correct", None, "mean", 1, None)}),
    (
        "longmemeval",
        "id",
        {
            "recall_all@5": ("recall_all@5", None, "mean", 1, None),
            "recall_any@5": ("recall_any@5", None, "mean", 1, None),
        },
    ),
    (
        "locomo",
        "id",
        {
            "recall_any@5": ("recall_any@5", None, "mean", 1, None),
            "recall_all@5": ("recall_all@5", None, "mean", 1, None),
        },
    ),
]
FALLBACK = ("score", "correct", "hit", "hit@5", "recall", "rr")


# --- reading runs -------------------------------------------------------------------


def resolve(path: str) -> str:
    """A harness run folder from a folder, a cases.jsonl, or a run wrapper folder."""
    path = os.path.abspath(os.path.expanduser(path)).rstrip("/")
    if path.endswith("cases.jsonl"):
        path = os.path.dirname(path)
        return os.path.dirname(path) if os.path.basename(path) == "test" else path
    if any(os.path.exists(os.path.join(path, *p)) for p in (("cases.jsonl",), ("test", "cases.jsonl"))):
        return path
    out = os.path.join(path, "output.txt")
    if os.path.exists(out):
        with open(out, encoding="utf-8", errors="replace") as f:
            found = re.findall(r"^results: (.+?)(?:/index\.html)?\s*$", f.read(), re.M)
        if found and os.path.isdir(found[-1]):
            return resolve(found[-1])
    sys.exit(f"{path}: no cases.jsonl, and no output.txt naming a run folder that has one")


def read_run(path: str) -> dict:
    """A run's rows; RUN#2 or RUN#1,3 keeps only those repeats (a run against its own repeats
    shows what no change looks like)."""
    path, _, picked = path.partition("#")
    keep = {int(k) for k in picked.split(",") if k.strip()} if picked else None
    folder = resolve(path)
    meta = {}
    if os.path.exists(os.path.join(folder, "meta.json")):
        with open(os.path.join(folder, "meta.json")) as f:
            meta = json.load(f)
    rows = []
    for part in (("cases.jsonl",), ("test", "cases.jsonl")):
        p = os.path.join(folder, *part)
        if os.path.exists(p):
            with open(p) as f:
                rows += [json.loads(line) for line in f if line.strip()]
    if keep:
        rows = [r for r in rows if (r.get("repeat") or 1) in keep]
    if spec_for(meta.get("eval") or "")[0] == "memorycode-extract":
        _add_lost(folder, rows)
    if not rows:
        sys.exit(f"{folder}: no case rows" + (f" for repeat {picked}" if keep else ""))
    return {"dir": folder, "meta": meta, "eval": meta.get("eval") or "", "rows": rows}


QUOTED = re.compile(r"'([^']+)'")


def _states(text: str, tokens: list[str]) -> bool:
    """evals_memorycode._states: every token appears as a token, not inside a longer word."""
    low = text.lower()
    for t in tokens:
        t = t.lower().lstrip("@")
        before = r"(?<![a-z0-9])" if t[:1].isalnum() else ""
        after = r"(?![a-z0-9])" if t[-1:].isalnum() else ""
        if not re.search(before + re.escape(t) + after, low):
            return False
    return True


def _add_lost(folder: str, rows: list[dict]) -> None:
    """MemoryCode runs from before evals_memorycode.score counted extracted_latest and lost:
    the same counts from each case's transcript (conventions, candidates, active notes)."""
    for r in rows:
        if "lost" in r or r.get("status") != "ok" or not r.get("transcript"):
            continue
        part = "test" if r.get("split") == "test" else ""
        path = os.path.join(folder, part, r["transcript"].removesuffix(".html") + ".json")
        try:
            with open(path) as f:
                t = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        notes = [f"{n['title']}\n{n['body']}" for n in (t.get("notes") or {}).get("active", [])]
        said = {int(k): [f"{m['title']} {m['body']}" for m in v] for k, v in (t.get("candidates") or {}).items()}
        extracted = lost = 0
        for c in t.get("conventions") or []:
            tokens = QUOTED.findall(c["forms"][-1])
            if tokens and any(_states(x, tokens) for x in said.get(c["sessions"][-1], [])):
                extracted += 1
                lost += not any(_states(n, tokens) for n in notes)
        r["extracted_latest"], r["lost"] = extracted, lost


def spec_for(name: str):
    name = name.replace("memorycode-consolidate", "memorycode-extract")  # a replay scores the same way
    for prefix, unit, metrics in SPECS:
        if name.startswith(prefix):
            return prefix, unit, metrics
    return "", "id", {}


def custom_metric(text: str, rows: list[dict]) -> tuple:
    """--metric KEY or NUM/DEN for a key the eval's spec does not name."""
    num, _, den = text.partition("/")
    if not any(num in r for r in rows):
        sys.exit(f"--metric {text}: no row has a {num!r} field")
    return (num, den or None, "ratio" if den else "mean", 0, None)


def _num(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float) and not (isinstance(v, float) and math.isnan(v)):
        return float(v)
    return None


def collect(runs: list[dict], unit_key: str, metric: tuple, split: str, tag: str | None) -> dict:
    """{unit: {replicate: (num, den)}} for one arm and metric. A unit with several rows in one
    replicate (a history's requests) sums them for a ratio and averages them for a mean."""
    num_key, den_key, how, _, where = metric
    out: dict[str, dict[tuple, list[float]]] = {}
    for run in runs:
        for r in run["rows"]:
            if r.get("status", "ok") != "ok":
                continue
            if split != "all" and r.get("split", "train") != split:
                continue
            if tag and tag not in (r.get("tags") or []):
                continue
            if where and not r.get(where):
                continue
            n = _num(r.get(num_key))
            d = _num(r.get(den_key)) if den_key else 1.0
            if n is None or d is None:
                continue
            unit = str(r.get(unit_key, r.get("id")))
            rep = (run["dir"], r.get("repeat") or 1)
            acc = out.setdefault(unit, {}).setdefault(rep, [0.0, 0.0])
            acc[0] += n
            acc[1] += d
    result: dict[str, dict] = {}
    for unit, reps in out.items():
        for rep, (n, d) in reps.items():
            if how == "mean":
                n, d = (n / d if d else None), 1.0  # a mean: the unit's own value, weight 1
            if n is not None:  # a ratio with nothing to rate here (den 0) still marks the replicate as present
                result.setdefault(unit, {})[rep] = (n, d)
    return result


def pooled(reps: dict) -> tuple[float, float]:
    """A unit's (num, den) averaged over its replicates."""
    k = len(reps)
    return sum(v[0] for v in reps.values()) / k, sum(v[1] for v in reps.values()) / k


def ratio(pairs) -> float | None:
    pairs = list(pairs)
    d = sum(p[1] for p in pairs)
    return sum(p[0] for p in pairs) / d if d else None


# --- statistics ---------------------------------------------------------------------


def bootstrap(a: list[tuple], b: list[tuple]) -> list[float]:
    """Paired resamples of cand minus base over the same cases."""
    rng = random.Random(SEED)
    n, out = len(a), []
    for _ in range(RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        ra, rb = ratio(a[i] for i in idx), ratio(b[i] for i in idx)
        if ra is not None and rb is not None:
            out.append(rb - ra)
    return sorted(out)


def replicates(data: dict, units: list[str]) -> list[dict]:
    """Each replicate's score on the units it covers; full=True when it covers all of them."""
    reps: dict[tuple, list] = {}
    for u in units:
        for rep, v in data[u].items():
            reps.setdefault(rep, []).append(v)
    out = []
    for rep, vals in sorted(reps.items()):
        out.append(
            {"run": rep[0], "repeat": rep[1], "cases": len(vals), "full": len(vals) == len(units), "score": ratio(vals)}
        )
    return out


def _sd(xs: list[float]) -> float | None:
    return statistics.stdev(xs) if len(xs) > 1 else None


def t95(dof: int) -> float:
    return T95.get(dof) or 1.96 + 2.4 / dof  # past 30 degrees of freedom this is within 0.002


def conservative(*floors: float | None) -> float | None:
    """The one rule for a smallest detectable difference: the largest of the floors that exist.
    The case bootstrap (or its one-arm form) assumes run noise is independent across cases;
    where one run's noise is shared (memory_v2 reads several cases' chats in one extraction
    call) the replicates' spread is larger, so the larger of the two sets the floor."""
    have = [f for f in floors if f is not None]
    return max(have) if have else None


def decompose(a: dict, b: dict | None, units: list[str]) -> dict:
    """Split the variance of the paired difference into case sampling and run noise, through
    each case's linearised contribution to the pooled score. Returns per-case variances."""

    def infl(data):
        pairs = [pooled(data[u]) for u in units]
        r = ratio(pairs)
        dbar = sum(p[1] for p in pairs) / len(pairs) or 1.0
        case = [(p[0] - r * p[1]) / dbar for p in pairs]
        within = []
        for u in units:
            reps = list(data[u].values())
            vals = [(n - r * d) / dbar for n, d in reps]
            within.append((statistics.variance(vals) if len(vals) > 1 else None, len(vals)))
        return case, within

    ca, wa = infl(a)
    cb, wb = infl(b) if b is not None else ([0.0] * len(units), [(0.0, 1)] * len(units))
    z = [y - x for x, y in zip(ca, cb, strict=True)]
    known = [w for w, _ in wa + (wb if b is not None else []) if w is not None]
    if not known or len(units) < 3:
        return {}
    fill = statistics.fmean(known)  # replicate variance where a case has one replicate
    va = [(w if w is not None else fill, k) for w, k in wa]
    vb = [(w if w is not None else fill, k) for w, k in wb] if b is not None else [(0.0, 1)] * len(units)
    observed_run = statistics.fmean(w / k for w, k in va) + statistics.fmean(w / k for w, k in vb)
    total = statistics.variance(z)
    per_rep = statistics.fmean(w for w, _ in va) + statistics.fmean(w for w, _ in vb)
    return {"n": len(units), "total": total, "het": max(0.0, total - observed_run), "run_per_rep": per_rep}


def repeats_needed(dec: dict, target: float, cap: int = 50) -> dict:
    if not dec or not target:
        return {}
    n = dec["n"]
    floor = Z80 * math.sqrt(dec["het"] / n)
    out = {"target": round(target, 4), "cases_only_floor": round(floor, 4)}
    if floor >= abs(target):
        out["repeats"] = None
        out["cases_needed"] = math.ceil(n * (floor / abs(target)) ** 2) if dec["het"] else None
        return out
    for k in range(1, cap + 1):
        if Z80 * math.sqrt((dec["het"] + dec["run_per_rep"] / k) / n) <= abs(target):
            out["repeats"] = k
            return out
    out["repeats"] = f">{cap}"
    return out


def compare_metric(name: str, metric: tuple, arms: list[list[dict]], unit_key, split, tag, target) -> dict:
    datas = [collect(runs, unit_key, metric, split, tag) for runs in arms]
    common = set.intersection(*(set(d) for d in datas))
    units = sorted((u for u in common if all(pooled(d[u])[1] for d in datas)), key=_natural)
    res: dict = {
        "metric": name,
        "direction": metric[3],
        "cases": len(units),
        "per_arm": [len(d) for d in datas],
        "in_every_arm": len(common),  # before the cases with nothing to rate here are dropped
    }
    if not units:
        return res
    a = datas[0]
    pa = [pooled(a[u]) for u in units]
    res["base"] = ratio(pa)
    res["base_replicates"] = replicates(a, units)
    res["flips"] = flips(datas, units)
    if len(datas) == 1:
        boot = _boot_one(pa)
        res["ci"] = [boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]] if boot else None
        dec = decompose(a, None, units)
        full = [r["score"] for r in res["base_replicates"] if r["full"]]
        sd = _sd(full)
        res["run_sd"] = sd
        mult = t95(len(full) - 1) + Z_POWER if sd is not None else None  # the spread has len(full) - 1 df

        def spread_floor(k: int):  # this arm against an identical one, k replicates a side
            return mult * sd * math.sqrt(2 / k) if sd is not None else None

        if sd is not None:  # a same-size rerun of this arm against itself
            res["run_floor"] = spread_floor(len(full))
        res["same_arm"] = [y - x for x, y in itertools.combinations(full, 2)]
        res["decompose"] = dec
        # Only run noise differs between this arm and an identical one. The case-level rule of the
        # paired path and the replicate spread, whichever is larger (see conservative()).
        res["null_detectable"] = {
            k: conservative(Z80 * math.sqrt(2 * dec["run_per_rep"] / (k * dec["n"])) if dec else None, spread_floor(k))
            for k in (1, 3, 5)
        }
        if not any(res["null_detectable"].values()):
            del res["null_detectable"]
        return res
    b = datas[1]
    pb = [pooled(b[u]) for u in units]
    res["cand"] = ratio(pb)
    res["delta"] = res["cand"] - res["base"]
    boot = bootstrap(pa, pb)
    if boot:
        res["ci"] = [boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]]
        res["se"] = statistics.stdev(boot)
        res["detectable"] = Z80 * res["se"]
    va = [p[0] / p[1] for p in pa]
    vb = [p[0] / p[1] for p in pb]
    sign = -1 if metric[3] < 0 else 1  # a win is a case where cand is better
    res["wins"] = sum(sign * (y - x) > EPS for x, y in zip(va, vb, strict=True))
    res["losses"] = sum(sign * (y - x) < -EPS for x, y in zip(va, vb, strict=True))
    res["ties"] = len(units) - res["wins"] - res["losses"]
    res["cand_replicates"] = replicates(b, units)
    sds = []
    for side in ("base", "cand"):
        full = [r["score"] for r in res[f"{side}_replicates"] if r["full"]]
        res[f"{side}_sd"] = _sd(full)
        res[f"{side}_full"] = len(full)
        res[f"{side}_same_arm"] = [y - x for x, y in itertools.combinations(full, 2)]
        sds.append((res[f"{side}_sd"], len(full)))
    dof = sum(k - 1 for s, k in sds if s is not None)
    run_se = None
    if dof:  # the arms' replicate spreads pooled (two replicates alone give a shaky spread)
        sd = math.sqrt(sum((k - 1) * s**2 for s, k in sds if s is not None) / dof)
        run_se = sd * math.sqrt(sum(1 / max(k, 1) for _, k in sds))
        res["run_sd_pooled"] = sd
        res["run_floor"] = (t95(dof) + Z_POWER) * run_se
    dec = decompose(a, b, units)
    res["decompose"] = dec
    res["repeats"] = repeats_needed(dec, target if target is not None else res["delta"])
    det = conservative(res.get("detectable"), res.get("run_floor"))
    if det is not None:
        if det != res.get("detectable"):
            res["detectable_from"] = "replicate spread"
        res["detectable"] = det
    lo, hi = res.get("ci") or (0, 0)
    res["run_noise_measured"] = min(res["base_full"], res["cand_full"]) >= MIN_REPLICATES
    if len(units) < MIN_CASES:
        res["verdict"] = f"too few cases (<{MIN_CASES})"
    elif not (lo > 0 or hi < 0):
        res["verdict"] = "within noise"
    elif not res["run_noise_measured"]:
        res["verdict"] = "run noise unmeasured"
    elif run_se is not None and abs(res["delta"]) <= t95(dof) * run_se:
        res["verdict"] = "within run noise"
    else:
        good = (lo > 0) == (metric[3] >= 0)
        res["verdict"] = ("better" if good else "worse") if metric[3] else "different"
    return res


def _boot_one(pairs: list[tuple]) -> list[float]:
    rng = random.Random(SEED)
    n, out = len(pairs), []
    for _ in range(RESAMPLES):
        r = ratio(pairs[rng.randrange(n)] for _ in range(n))
        if r is not None:
            out.append(r)
    return sorted(out)


def flips(datas: list[dict], units: list[str]) -> dict:
    """Cases whose value changes between replicates of an arm, or differs between arms."""
    unstable = [[], []]
    multi = [0, 0]
    differ = []
    for u in units:
        means = []
        for i, data in enumerate(datas):
            vals = [n / d for n, d in data[u].values() if d]
            if len(vals) > 1:
                multi[i] += 1
                if max(vals) - min(vals) > EPS:
                    unstable[i].append(u)
            means.append(sum(vals) / len(vals))
        if len(datas) > 1 and u not in unstable[0] and u not in unstable[1] and abs(means[1] - means[0]) > EPS:
            differ.append(u)
    out = {"unstable_base": unstable[0], "base_cases_with_repeats": multi[0]}
    if len(datas) > 1:
        out.update(unstable_cand=unstable[1], cand_cases_with_repeats=multi[1], differ=differ)
    return out


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# --- the command --------------------------------------------------------------------


def run(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(
        prog="recall eval compare",
        description="Paired comparison of eval runs: RUN [RUN...] [vs RUN [RUN...]]",
    )
    p.add_argument("runs", nargs="+", help="base run folders, then 'vs', then candidate run folders")
    p.add_argument("--metric", action="append", help="a metric of the eval, or KEY / NUM/DEN of the case rows")
    p.add_argument("--split", choices=["train", "test", "all"], default="all")
    p.add_argument("--tag", help="only cases with this tag (e.g. long, short)")
    p.add_argument("--names", default="base,cand", help="labels for the arms (default base,cand)")
    p.add_argument("--flips", action="store_true", help="list the cases that change, and how to rerun only those")
    p.add_argument("--target", type=float, help="the difference to size repeats for (default: the observed one)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    groups = [list(g) for k, g in itertools.groupby(a.runs, lambda s: s == "vs") if not k]
    if len(groups) > 2 or (len(groups) == 2 and a.runs.count("vs") != 1) or not groups:
        p.error("give base runs, then 'vs', then candidate runs")
    arms = [[read_run(r) for r in g] for g in groups]
    names = [*a.names.split(","), "base", "cand"][:2]
    evals = sorted({r["eval"] for arm in arms for r in arm})
    _, unit_key, metrics = spec_for(evals[0])
    warnings = []
    if len({spec_for(e)[0] for e in evals}) > 1:
        warnings.append(f"the runs are of different evals: {', '.join(evals)}")
    for key in ("judge", "judge_version", "model", "keyword_only"):
        seen = {json.dumps(r["meta"].get(key)) for arm in arms for r in arm if key in r["meta"]}
        if len(seen) > 1:
            warnings.append(f"the runs differ in {key}: {', '.join(sorted(seen))}")
    if any("agent_harness" in r["meta"] for arm in arms for r in arm):  # an agent eval's tool setup
        seen = {json.dumps(r["meta"].get("agent_harness", "unrecorded")) for arm in arms for r in arm}
        if len(seen) > 1:
            warnings.append(
                f"the runs differ in agent_harness: {', '.join(sorted(seen))} (runs made before the marker are "
                "unrecorded): their tool use and token counts may not compare"
            )
    rows = [row for arm in arms for r in arm for row in r["rows"]]
    if a.metric:
        chosen = {m: metrics.get(m) or custom_metric(m, rows) for m in a.metric}
    elif metrics:
        chosen = metrics
    else:
        chosen = {k: (k, None, "mean", 1, None) for k in FALLBACK if any(k in r for r in rows)}
        if not chosen:
            sys.exit(f"no known metric for eval {evals[0]!r}: pass --metric KEY or --metric NUM/DEN")
    results = [compare_metric(m, spec, arms, unit_key, a.split, a.tag, a.target) for m, spec in chosen.items()]
    if len(arms) > 1:  # cases only one arm scored are left out of every number below
        unpaired: dict[tuple, list[str]] = {}
        for m in results:
            if any(n > m["in_every_arm"] for n in m["per_arm"]):
                unpaired.setdefault((m["in_every_arm"], *m["per_arm"]), []).append(m["metric"])
        for (both, *each), ms in unpaired.items():
            sizes = ", ".join(f"{n} has {k}" for n, k in zip(names, each, strict=False))
            of = f" ({', '.join(ms)})" if len(unpaired) > 1 else ""
            warnings.append(f"only {both} cases are in both arms, but {sizes}: the others are left out{of}")
    report = {
        "eval": evals[0] if len(evals) == 1 else evals,
        "unit": unit_key,
        "split": a.split,
        "tag": a.tag,
        "arms": {n: [r["dir"] for r in arm] for n, arm in zip(names, arms, strict=False)},
        "warnings": warnings,
        "has_test": any(row.get("split") == "test" for row in rows),
        "metrics": results,
    }
    if a.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        show(report, names, a.flips, unit_key)
    return report


def _f(x, signed=False) -> str:
    if x is None:
        return "n/a"
    digits = 0 if abs(x) >= 100 else 3  # token counts read better whole
    return f"{x:+.{digits}f}" if signed else f"{x:.{digits}f}"


def show(report: dict, names: list[str], list_flips: bool, unit_key: str) -> None:
    arms = report["arms"]
    paired = len(arms) > 1
    test_rows = report.get("has_test")
    name = report["eval"] if isinstance(report["eval"], str) else " vs ".join(report["eval"])
    head = f"{name} · " + " · ".join(f"{n}: {len(dirs)} run{'s' if len(dirs) > 1 else ''}" for n, dirs in arms.items())
    print(
        head
        + (f" · split {report['split']}" if report["split"] != "all" else "")
        + (f" · tag {report['tag']}" if report["tag"] else "")
    )
    for w in report["warnings"]:
        print(f"warning: {w}")
    b, c = names
    if paired:
        print(f"\n{'metric':16} {b:>8} {c:>8} {'Δ':>8}  {'95% CI':18} {'W/L/T':10} {'detectable':>10}  verdict")
    else:
        print(f"\n{'metric':16} {b:>8}  {'95% CI':18} {'cases':>6}")
    for m in report["metrics"]:
        label = m["metric"] + {1: "", -1: " (↓)", 0: ""}[m["direction"]]
        if not m["cases"]:
            sizes = ", ".join(f"{n} {k}" for n, k in zip(names, m["per_arm"], strict=False))
            print(f"{label:16} no case scored in every arm ({sizes} cases; different splits?)")
            continue
        ci = m.get("ci")
        ci_text = f"[{_f(ci[0], paired)}, {_f(ci[1], paired)}]" if ci else ""
        if paired:
            wlt = f"{m['wins']}/{m['losses']}/{m['ties']}"
            det = f"±{_f(m['detectable'])}" if m.get("detectable") is not None else ""
            print(
                f"{label:16} {_f(m['base']):>8} {_f(m['cand']):>8} {_f(m['delta'], True):>8}  {ci_text:18} "
                f"{wlt:10} {det:>10}  {m['verdict']}"
            )
        else:
            print(f"{label:16} {_f(m['base']):>8}  {ci_text:18} {m['cases']:>6}")
    print(f"\ncases{' paired' if paired else ''}: {report['metrics'][0]['cases']} (unit: {unit_key})")
    print("\nrun-to-run: each replicate's score on those cases, its spread, and same-arm differences")
    for m in report["metrics"]:
        if not m["cases"]:
            continue
        parts = []
        sides = [("base", b), ("cand", c)] if paired else [("base", b)]
        for key, name in sides:
            reps = m.get(f"{key}_replicates") or []
            vals = " ".join(_f(r["score"]) + ("" if r["full"] else f"*{r['cases']}") for r in reps)
            sd = m.get(f"{key}_sd") if paired else m.get("run_sd")
            same = m.get(f"{key}_same_arm") if paired else m.get("same_arm")
            text = f"{name} {vals}"
            if sd is not None:
                text += f" (sd {_f(sd)}"
                if same:
                    text += f", max same-arm |Δ| {_f(max(abs(x) for x in same))}"
                text += ")"
            parts.append(text)
        print(f"  {m['metric']:14} " + "   ".join(parts))
    if any(not r["full"] for m in report["metrics"] for k in ("base", "cand") for r in m.get(f"{k}_replicates") or []):
        print("  (* a replicate covering only some cases, e.g. extra repeats of flips: left out of the spread)")
    print("\nnoise:")
    for m in report["metrics"]:
        if not m["cases"]:
            continue
        bits = []
        if paired and m.get("detectable") is not None:
            bits.append(f"detectable ±{_f(m['detectable'])}")
        if m.get("run_floor") is not None:
            bits.append(
                f"run noise alone ±{_f(m['run_floor'])}" + ("" if paired else " (a rerun of this arm vs itself)")
            )
        if paired and m.get("run_noise_measured") is False:
            bits.append(
                f"run noise unmeasured: {b} has {m['base_full']} and {c} {m['cand_full']} full replicates, "
                f"{MIN_REPLICATES} each needed"
            )
        rep = m.get("repeats") or {}
        if rep:
            floor = rep["cases_only_floor"]
            bits.append(f"case sampling alone ±{_f(floor)}" if floor >= 0.0005 else "case sampling alone ≈0")
            if rep.get("repeats") is None:
                more = f" (about {rep['cases_needed']} cases)" if rep.get("cases_needed") else ""
                bits.append(f"to detect {_f(rep['target'], True)}: more repeats cannot, it needs more cases{more}")
            else:
                k = rep["repeats"]
                bits.append(f"to detect {_f(rep['target'], True)}: {k} replicate{'' if k == 1 else 's'} per arm")
        if m.get("null_detectable"):
            k = m["null_detectable"]
            bits.append(
                "this arm vs an identical one detects " + ", ".join(f"±{_f(v)} at {n}v{n}" for n, v in k.items())
            )
        if bits:
            print(f"  {m['metric']:14} " + "; ".join(bits))
    fl = report["metrics"][0].get("flips") or {}
    if fl:
        print(f"\nflips ({report['metrics'][0]['metric']}):")
        for key, name in (("base", b), ("cand", c)):
            if f"unstable_{key}" not in fl:
                continue
            n = fl[f"{key}_cases_with_repeats"]
            k = len(fl[f"unstable_{key}"])
            rate = f"{k}/{n} cases change between {name}'s replicates" if n else f"{name} has one replicate per case"
            print(f"  {rate}")
        if paired:
            print(f"  {len(fl['differ'])} more cases are steady in each arm but differ between them")
        if list_flips:
            ids = sorted(
                set(fl.get("unstable_base", [])) | set(fl.get("unstable_cand", [])) | set(fl.get("differ", [])),
                key=_natural,
            )
            if test_rows:
                print("  (test cases: ids only, for reruns; do not read their transcripts)")
            print("  cases: " + (" ".join(ids) or "none"))
            if ids:
                print("  rerun only these: " + " ".join(f"--case {i}" for i in ids))
        else:
            print("  (--flips lists them and the --case flags to rerun only those)")


def main(argv: list[str] | None = None) -> None:
    run(argv)


if __name__ == "__main__":
    main()

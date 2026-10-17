"""recall's benchmark results: a registry of benchmarks, one append-only store of figures, and
chart specs that draw from it. See README.md beside this file.

    registry.json   every benchmark and metric: unit, direction, corpus, how to run it, and which
                    fields must match for two figures to compare (registry.schema.json)
    results.jsonl   one row per figure, appended and never edited (results.schema.json)
    charts.json     which rows each chart draws; every number in a chart comes from the store
                    (charts.schema.json)
    figures.json    every chart with its numbers filled in, written by make_charts.py

Reading needs only the standard library:

    sys.path.insert(0, "docs/benchmarks"); import store
    rows = store.load()
    store.select(rows, benchmark="swecb-lite-retrieval", metric="hit@5", **{"conditions.scope": "all"})
    store.latest_per_release(rows, "swecb-lite-retrieval", "hit@5", **{"conditions.search": "hybrid"})
    store.pairs(rows)             # control, candidate and delta rows by pair id
    store.chart("swecb")          # a chart from charts.json with its numbers filled in

Commands, from the repo root:

    python docs/benchmarks/store.py show [--benchmark B] [--metric M] [--system S] [--release R]
    python docs/benchmarks/store.py pairs
    uv run python docs/benchmarks/store.py record RUN_DIR [RUN_DIR ...] [--public] [--pair P --role R --allow-diff F]
    uv run python docs/benchmarks/store.py record-pair PAIR CONTROL_DIR CANDIDATE_DIR --allow-diff F [--public]
    uv run python docs/benchmarks/store.py add ROWS.json
    uv run python docs/benchmarks/store.py check [--base origin/main]

record and add check the store with their rows before appending, so they need jsonschema too.

Only aggregates go in. A row names its run folders by basename, a pointer into the private runs
folder (RECALL_EVAL_RUNS), and never copies anything else from them.
"""

import argparse
import functools
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
STORE = os.path.join(HERE, "results.jsonl")
REGISTRY = os.path.join(HERE, "registry.json")
SCHEMA = os.path.join(HERE, "results.schema.json")
SCHEMAS = {
    REGISTRY: os.path.join(HERE, "registry.schema.json"),
    os.path.join(HERE, "charts.json"): os.path.join(HERE, "charts.schema.json"),
}
CHARTS = os.path.join(HERE, "charts.json")
RUNS = os.path.normpath(os.path.expanduser(os.environ.get("RECALL_EVAL_RUNS", "~/.agents/recall-eval/runs")))
CASES = os.path.expanduser(os.environ.get("RECALL_EVAL_CASES", "~/.agents/recall/evals"))

# every row carries every field, in this order; null (or []) is "unknown" or "does not apply"
FIELDS = [
    "id", "kind", "date", "benchmark", "metric", "value", "ci", "range", "by_repeat", "n", "repeats", "split",
    "system", "code", "model", "judge", "judge_version", "harness", "conditions", "corpus", "pair", "role",
    "control", "candidate", "varies", "runs", "source", "source_detail", "grade", "reported", "public", "supersedes",
    "note",
]  # fmt: skip
LISTS = {"varies", "runs", "supersedes"}
RUN_NAME = re.compile(r"^\d{8}-\d{4,6}-[A-Za-z0-9._-]+$")
# never in the store: paths (this machine's home among them), file URLs, chat ids and other
# UUIDs, email addresses
PRIVATE = re.compile(
    "|".join(
        [
            re.escape(os.path.expanduser("~")),
            r"/Users/|/home/|/private/|/var/|/tmp/|~/|file://|\.agents\b",
            r"\b(?:claude|codex|cursor|gemini|opencode)[:/][0-9a-f]{8}-",
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        ]
    ),
    re.I,
)
# what the two arms of a pair (and of a delta) must share, beyond the registry's comparable_on,
# unless the pair names the field in varies
PAIR_FIELDS = ["split", "model", "judge", "judge_version", "harness", "code", "corpus.home", "corpus.chat_schema"]


# --- rows -------------------------------------------------------------------------


def row_id(row: dict) -> str:
    """The first 12 hex digits of sha1 over the row without its id, so an edited row no longer
    matches its id and `check` finds it."""
    body = {k: v for k, v in row.items() if k != "id"}
    return hashlib.sha1(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def complete(row: dict) -> dict:
    """Every field in FIELDS order, missing ones null (or []), and the id computed. A field the
    store does not have is an error, not something to drop."""
    unknown = sorted(set(row) - set(FIELDS))
    if unknown:
        raise ValueError(f"unknown fields {', '.join(unknown)}: a row has only {', '.join(FIELDS)}")
    out = {k: row.get(k, [] if k in LISTS else None) for k in FIELDS}
    out["conditions"] = out["conditions"] or {}
    out["id"] = row_id(out)
    return out


def dumps(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False)


def current(rows: list[dict]) -> list[dict]:
    """The rows that stand: without any a later row supersedes, and without withdrawals (a row
    with no value that supersedes others). Anyone reading results.jsonl should do the same."""
    gone = {i for r in rows for i in r.get("supersedes") or []}
    return [r for r in rows if r["id"] not in gone and not (r.get("supersedes") and r.get("value") is None)]


def load(path: str = STORE, superseded: bool = False) -> list[dict]:
    """The rows that stand (current); every row as written with superseded=True."""
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return rows if superseded else current(rows)


@functools.cache
def registry() -> dict:
    with open(REGISTRY, encoding="utf-8") as f:
        return json.load(f)


def metric_spec(benchmark: str, metric: str) -> dict:
    return registry()["benchmarks"][benchmark]["metrics"][metric]


# --- queries ----------------------------------------------------------------------

_MISSING = object()


def get(row: dict, path: str):
    """A dotted field: get(row, "conditions.scope"); "release" is short for "code.release"."""
    if path == "release":
        path = "code.release"
    cur = row
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def version(release: str | None) -> tuple:
    return tuple(int(x) for x in release.split(".")) if release else ()


def select(rows: list[dict], where: dict | None = None, **kw) -> list[dict]:
    """Rows whose fields equal every key in where (dotted paths; a list means any of them; "*"
    means present and not null). "release": "latest" keeps the highest release among the rest."""
    where = {**(where or {}), **kw}
    latest = where.get("release") == "latest" or where.get("code.release") == "latest"
    conds = {k: v for k, v in where.items() if v != "latest" or k not in ("release", "code.release")}

    def ok(r: dict) -> bool:
        for k, want in conds.items():
            have = get(r, k)
            if want == "*":
                if have in (_MISSING, None):
                    return False
            elif isinstance(want, list):
                if have not in want:
                    return False
            elif have != want:
                return False
        return True

    found = [r for r in rows if ok(r)]
    if latest:
        found = [r for r in found if get(r, "code.release") not in (_MISSING, None)]
        top = max((version(r["code"]["release"]) for r in found), default=None)
        found = [r for r in found if version(r["code"]["release"]) == top]
    return found


def one(rows: list[dict], where: dict, public: bool = True) -> dict:
    """The single row a chart or a sentence means: matching, with a value, carrying the fewest
    conditions the selector leaves out, the newest by date. Two such rows that differ are
    ambiguous: the selector must say more."""
    found = [r for r in select(rows, where) if r["value"] is not None]
    if public:
        hidden = [r for r in found if not r["public"]]
        found = [r for r in found if r["public"]]
        if not found and hidden:
            raise LookupError(f"{where}: only non-public rows match ({', '.join(r['id'] for r in hidden)})")
    if not found:
        raise LookupError(f"no row matches {where}")
    # conditions the selector does not name: the aggregate row wins over its breakdowns
    named = {k.split(".", 1)[1] for k in where if k.startswith("conditions.")}
    fewest = min(len(set(r["conditions"]) - named) for r in found)
    found = [r for r in found if len(set(r["conditions"]) - named) == fewest]
    newest = max(r["date"] for r in found)
    top = [r for r in found if r["date"] == newest]
    if len({r["value"] for r in top}) > 1:
        raise LookupError(f"{where} matches {len(top)} rows on {newest}: {', '.join(r['id'] for r in top)}")
    return top[0]


def latest_per_release(rows: list[dict], benchmark: str, metric: str, **where) -> dict[str, dict]:
    """{release: its newest row} for recall's measured rows of one metric."""
    base = {"benchmark": benchmark, "metric": metric, "kind": "measured", "system": "recall", **where}
    out = {}
    for rel in sorted({r["code"]["release"] for r in select(rows, base) if r["code"]["release"]}, key=version):
        out[rel] = one(rows, {**base, "code.release": rel}, public=False)
    return out


def comparable(a: dict, b: dict) -> list[str]:
    """The fields the benchmark says must match that differ between two rows (empty: they
    compare). A benchmark marked same_day also needs the same date."""
    if a["benchmark"] != b["benchmark"] or a["metric"] != b["metric"]:
        return ["benchmark" if a["benchmark"] != b["benchmark"] else "metric"]
    spec = registry()["benchmarks"][a["benchmark"]]
    keys = list(spec.get("comparable_on", [])) + (["date"] if spec.get("same_day") else [])
    return [k for k in keys if get(a, k) != get(b, k)]


def pair_differences(a: dict, b: dict) -> list[str]:
    """The fields in which two arms of a pair or a delta differ, among those they must share:
    PAIR_FIELDS, the benchmark's comparable_on, every condition either has, and the date for a
    same_day benchmark. A pair lists the ones it means to vary in its rows' varies."""
    spec = registry()["benchmarks"][a["benchmark"]]
    keys = set(PAIR_FIELDS) | set(spec.get("comparable_on", []))
    keys |= {f"conditions.{k}" for k in set(a["conditions"]) | set(b["conditions"])}
    if spec.get("same_day"):
        keys.add("date")
    return sorted(k for k in keys if get(a, k) != get(b, k))


def allowed(field: str, varies: list[str]) -> bool:
    """Is a difference in field one that varies names (itself, or a parent such as corpus)?"""
    return any(field == v or field.startswith(v + ".") for v in varies)


def pair_key(r: dict, varies: list[str]) -> tuple:
    """What matches a control row with its candidate: benchmark, metric and the conditions the
    pair does not vary."""
    kept = tuple(sorted((k, v) for k, v in r["conditions"].items() if not allowed(f"conditions.{k}", varies)))
    return r["benchmark"], r["metric"], kept


def pairs(rows: list[dict]) -> dict[str, dict]:
    """{pair: {"control": [...], "candidate": [...], "delta": [...], "same_day": bool}}."""
    out: dict[str, dict] = {}
    for r in rows:
        if r["pair"]:
            p = out.setdefault(r["pair"], {"control": [], "candidate": [], "delta": []})
            p[r["role"] or ("delta" if r["kind"] == "delta" else "candidate")].append(r)
    for p in out.values():
        dates = {r["date"] for r in p["control"] + p["candidate"]}
        p["same_day"] = len(dates) == 1
    return out


# --- formatting -------------------------------------------------------------------

UNIT_FORMAT = {"fraction": "pct", "tokens": "int", "usd": "usd", "minutes": "min", "ms": "ms", "count": "int"}


def fmt(value: float, how: str) -> str:
    """A figure as text, rounding half up on the decimal value (0.865 is 87%, not 86%)."""
    d = Decimal(str(value))

    def q(x: Decimal, places: int) -> str:
        return str(x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))

    if how == "pct":
        return q(d * 100, 1) + "%"
    if how == "pct0":
        return q(d * 100, 0) + "%"
    if how == "k":
        return q(d / 1000, 1) + "k"
    if how == "k0":
        return q(d / 1000, 0) + "k"
    if how == "usd":
        return "$" + q(d, 2)
    if how == "h":
        return f"{float(d / 60):g} h"
    if how == "min":
        return f"{float(d):g} min"
    if how == "ms":
        return q(d, 1) + " ms"
    if how == "int":
        return f"{int(q(d, 0)):,}"
    return f"{float(d):g}"


def scaled(value: float, unit: str, display: str) -> float:
    """A stored value in a chart's display unit, rounded as the chart prints it."""
    d = Decimal(str(value))
    if display == "%" and unit == "fraction":
        return float((d * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    if display == "k":
        return float((d / 1000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return float(d)


# --- charts -----------------------------------------------------------------------


@functools.cache
def chart_specs() -> dict:
    with open(CHARTS, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


class _Chart:
    """Resolves one chart spec: every selector is merged with the chart's (and its group's or
    series') own, and must find exactly one public row."""

    def __init__(self, spec: dict, rows: list[dict]):
        self.spec, self.rows, self.used = spec, rows, []
        self.by_id = {r["id"]: r for r in rows}

    def sources(self, ids: list[str]) -> dict:
        """A mark's third-party sources, when it has any: source for one row, sources (one per
        row, null for recall's own) for several."""
        found = [self.by_id[i]["source"] for i in ids]
        if not any(found):
            return {}
        return {"source": found[0]} if len(found) == 1 else {"sources": found}

    def row(self, *wheres: dict) -> dict:
        where = {}
        for w in wheres:
            where.update(w or {})
        r = one(self.rows, where)
        self.used.append(r["id"])
        return r

    def value(self, display: str, *wheres: dict) -> tuple[float, str]:
        r = self.row(*wheres)
        return scaled(r["value"], metric_spec(r["benchmark"], r["metric"])["unit"], display), r["id"]

    def fill(self, text: str | None, refs: dict, self_row: dict | None, *base: dict) -> str | None:
        """{name} or {name:format} from the refs (selectors merged onto base), {self.field} from
        the mark's own row."""
        if not text or "{" not in text:
            return text

        def sub(m: re.Match) -> str:
            name, how = m.group(1), m.group(2)
            if name.startswith("self."):
                v = get(self_row or {}, name[5:])
                return str(v) if how is None else fmt(v, how)
            r = self.row(*base, refs[name])
            return fmt(r["value"], how or UNIT_FORMAT.get(metric_spec(r["benchmark"], r["metric"])["unit"], ""))

        return re.sub(r"\{([\w.]+)(?::(\w+))?\}", sub, text)

    def resolve(self) -> dict:
        s = self.spec
        kind, base, refs = s["kind"], s.get("where", {}), s.get("refs", {})
        out = {k: v for k, v in s.items() if k not in ("where", "refs", "groups", "series", "points", "categories")}
        out["title"] = self.fill(s["title"], refs, None)  # a chart's own refs stand alone
        out["subtitle"] = self.fill(s.get("subtitle", ""), refs, None)
        if kind == "bars":
            display = s.get("unit", "%")
            out["rows"] = []
            for g in s["groups"]:
                for item in g["rows"]:
                    r = self.row(base, g.get("where"), item["where"])
                    v = scaled(r["value"], metric_spec(r["benchmark"], r["metric"])["unit"], display)
                    mark = {"group": g.get("label"), "name": item["name"], "value": v}
                    if item.get("style") == "ours":
                        mark["ours"] = True
                    elif item.get("style"):
                        mark["cls"] = item["style"]
                    mark["note"] = self.fill(
                        item.get("note"), item.get("refs", {}), r, base, g.get("where"), item["where"]
                    )
                    mark = {k: v for k, v in mark.items() if v is not None}
                    if item.get("label"):
                        mark["label"] = item["label"]
                    mark["row"] = r["id"]
                    out["rows"].append({**mark, **self.sources([r["id"]])})
            if s.get("ceiling"):
                c = s["ceiling"]
                v, rid = self.value(display, base, c["where"])
                out["ceiling"] = {"value": v, "label": c["label"], "row": rid}
        elif kind in ("grouped", "columns"):
            out["categories"] = [c["label"] for c in s["categories"]]
            out["series"] = []
            for se in s["series"]:
                vals, ids = [], []
                for c in s["categories"]:
                    v, rid = self.value("%", base, se["where"], c["where"])
                    vals.append(v)
                    ids.append(rid)
                mark = {"name": se["name"], **({"cls": se["style"]} if se.get("style") else {}), "values": vals}
                out["series"].append({**mark, "rows": ids, **self.sources(ids)})
        elif kind == "scatter":
            out["points"] = []
            for p in s["points"]:
                x, xid = self.value("", base, p["where"], p["x"])
                y, yid = self.value("%", base, p["where"], p["y"])
                mark = {"name": p["name"], "tokens": x, "value": y}
                if p.get("style") == "ours":
                    mark["ours"] = True
                mark.update({k: p[k] for k in ("anchor", "label_dx", "label_dy") if k in p})
                out["points"].append({**mark, "rows": [xid, yid], **self.sources([xid, yid])})
        elif kind == "cost_curve":
            out["series"] = []
            for se in s["series"]:
                pts = []
                for p in se["points"]:
                    x, xid = self.value("", base, se.get("where"), p["where"], p["x"])
                    y, yid = self.value("%", base, se.get("where"), p["where"], p["y"])
                    mark = {"label": p["label"], "cost": x, "value": y}
                    mark.update({k: p[k] for k in ("dx", "dy") if k in p})
                    pts.append({**mark, "rows": [xid, yid], **self.sources([xid, yid])})
                out["series"].append({"name": se["name"], "points": pts})
            out["references"] = []
            for p in s.get("references", []):
                x, xid = self.value("", base, p["where"], p["x"])
                y, yid = self.value("%", base, p["where"], p["y"])
                mark = {"name": p["name"], "cost": x, "value": y}
                mark.update({k: p[k] for k in ("anchor", "dx", "dy") if k in p})
                out["references"].append({**mark, "rows": [xid, yid], **self.sources([xid, yid])})
        else:
            raise ValueError(f"unknown chart kind {kind}")
        out["rows_used"] = sorted(set(self.used))
        return out


def chart(name: str, rows: list[dict] | None = None) -> dict:
    """A chart from charts.json with its numbers filled in from the store: the shape
    make_charts.py draws, each mark carrying the id of the row (or rows) it shows."""
    return _Chart(chart_specs()[name], rows if rows is not None else load()).resolve()


# --- code identity ----------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, check=True).stdout


@functools.cache
def prod_hash(ref: str, blind: bool = False) -> str:
    """The run wrapper's product-code id for a git ref: sha1 over every product file under
    src/recall (.py, .md, .json, .toml; not evals*.py or cli.py) in path order, as the wrapper
    computes it over a checkout (`find | sort | xargs cat | shasum`). blind: with the
    __version__ line blanked, so a release commit and the one before its version bump agree."""
    files = []
    for line in _git("ls-tree", "-r", ref, "src/recall").splitlines():
        meta, path = line.split("\t", 1)
        rel = path[len("src/") :]
        base = rel.rsplit("/", 1)[-1]
        if not base.endswith((".py", ".md", ".json", ".toml")) or base == "cli.py" or "__pycache__" in rel:
            continue
        if base.startswith("evals") and base.endswith(".py"):
            continue
        files.append((rel, meta.split()[2]))
    files.sort()
    blobs = subprocess.run(
        ["git", "-C", REPO, "cat-file", "--batch"],
        input="".join(f"{sha}\n" for _, sha in files).encode(),
        capture_output=True,
        check=True,
    ).stdout
    h, pos = hashlib.sha1(), 0
    for rel, _ in files:  # each object: "<sha> blob <size>\n<content>\n"
        nl = blobs.index(b"\n", pos)
        size = int(blobs[pos:nl].split()[2])
        content = blobs[nl + 1 : nl + 1 + size]
        if blind and rel == "recall/__init__.py":
            content = re.sub(rb'(?m)^__version__ = "[^"]*"', b'__version__ = ""', content)
        h.update(content)
        pos = nl + 2 + size
    return h.hexdigest()[:8]


@functools.cache
def _tags() -> list[tuple[str, str, str]]:
    """(tag, exact prod, prod apart from __version__) for every release tag, oldest first."""
    tags = sorted(_git("tag", "--list", "v*").split(), key=lambda t: version(t[1:]))
    return [(t, prod_hash(t), prod_hash(t, blind=True)) for t in tags]


def _main_ref() -> str:
    """The published branch: origin/main when there is one, else HEAD."""
    ok = subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", "-q", "origin/main"], capture_output=True)
    return "origin/main" if ok.returncode == 0 else "HEAD"


@functools.cache
def resolve_code(prod: str | None, before: str | None = None) -> dict:
    """A run's code identity from its prod hash. git: a commit on the published branch with
    exactly this product code (a release tag's first), so anyone can look it up; release: the
    oldest tag whose product code is the same apart from __version__, since release runs
    measure the commit before the version bump. A commit found only on local branches (a
    feature branch, history from before a rewrite) still gives the release, but no git.
    Unknown: just the hash."""
    out = {"release": None, "git": None, "prod": prod}
    if not prod:
        return out
    for tag, exact, _ in _tags():
        if exact == prod:
            return {**out, "release": tag[1:], "git": _git("rev-parse", "--short=7", f"{tag}^{{commit}}").strip()}
    when = datetime.fromisoformat(before).timestamp() if before else 0

    def nearest(*refs: str) -> list[str]:
        """Commits of refs, nearest the run's date first: code often runs before it is committed."""
        commits = [line.split() for line in _git("log", *refs, "--format=%H %ct").splitlines()]
        return [c[0] for c in sorted(commits, key=lambda c: abs(int(c[1]) - when) if before else -int(c[1]))]

    published = nearest(_main_ref())
    seen = set(published)
    for sha in published + [c for c in nearest("--all") if c not in seen]:
        if prod_hash(sha) != prod:
            continue
        if sha in seen:
            out["git"] = sha[:7]
        # the oldest tag with this code; not an ancestry test, which history rewrites break
        blind = prod_hash(sha, blind=True)
        out["release"] = next((tag[1:] for tag, _, tag_blind in _tags() if tag_blind == blind), None)
        return out
    return out


# --- recording run folders --------------------------------------------------------


def _meta_txt(path: str) -> dict:
    """The run wrapper's meta.txt: key=value pairs; cmd= runs to the end of its line."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            head, _, cmd = line.partition(" cmd=")
            for m in re.finditer(r"(\w+)=(\S+(?: \d\d:\d\d:\d\d)?)", head):
                out[m.group(1)] = m.group(2)
            if cmd:
                out["cmd"] = cmd.strip()
    return out


def _first_json(path: str) -> dict | None:
    """The JSON object an older eval prints to stdout (output.txt), if there is one."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("\n{", start + 1)
        start = start + 1 if start != -1 else -1
    return None


@functools.cache
def _invalid_runs(ledger_needed: bool = True) -> frozenset[str]:
    """Run folders the ledger beside the runs folder marks INVALID. With no ledger there is no
    telling, so recording stops unless the caller says to go on without one."""
    ledger = os.path.join(os.path.dirname(RUNS), "LEDGER.md")
    if not os.path.exists(ledger):
        if ledger_needed:
            sys.exit(f"no ledger at {ledger}, so INVALID runs cannot be told apart: pass --no-ledger to record anyway")
        return frozenset()
    with open(ledger, encoding="utf-8") as f:
        text = f.read()
    return frozenset(
        m for line in text.splitlines() if "INVALID" in line for m in re.findall(r"\d{8}-\d{4,6}-[A-Za-z0-9._-]+", line)
    )


def read_run(folder: str, ledger_needed: bool = True) -> dict:
    """What a run folder says about itself: its eval, aggregates, settings and code. Refuses a
    run that has not finished, failed, or whose folder (or the harness folder it names) the
    ledger marks INVALID."""
    folder = os.path.normpath(os.path.abspath(os.path.expanduser(folder)))
    name = os.path.basename(folder)
    if not RUN_NAME.match(name):
        sys.exit(f"{name}: not a run folder name (YYYYMMDD-HHMM[SS]-who-name)")
    wrapper = _meta_txt(os.path.join(folder, "meta.txt")) if os.path.exists(os.path.join(folder, "meta.txt")) else {}
    meta = {}
    if os.path.exists(os.path.join(folder, "meta.json")):
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    if wrapper and "exit" not in wrapper:
        sys.exit(f"{name}: meta.txt has no exit= line: the run is still going, or was killed")
    if wrapper.get("exit", "0") != "0":
        sys.exit(f"{name}: the run exited {wrapper['exit']}")
    if not wrapper and not meta.get("finished"):
        sys.exit(f"{name}: no meta.txt and meta.json has no finished time")
    names, inner = [name], folder
    out_txt = os.path.join(folder, "output.txt")
    if not os.path.exists(os.path.join(folder, "summary.json")) and os.path.exists(out_txt):
        # older harness code wrote its own folder; the wrapper's output names it
        with open(out_txt, encoding="utf-8", errors="replace") as f:
            m = re.search(r"results: (\S+)/index\.html", f.read())
        if m and os.path.isdir(m.group(1)) and RUN_NAME.match(os.path.basename(os.path.normpath(m.group(1)))):
            inner = os.path.normpath(m.group(1))
            names.append(os.path.basename(inner))
            if os.path.exists(os.path.join(inner, "meta.json")):
                with open(os.path.join(inner, "meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
    summary = None
    if os.path.exists(os.path.join(inner, "summary.json")):
        with open(os.path.join(inner, "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
    elif os.path.exists(out_txt):
        summary = _first_json(out_txt)
    for n in names:
        if n in _invalid_runs(ledger_needed):
            sys.exit(f"{n}: the ledger marks this run INVALID")
    if not summary:
        sys.exit(f"{name}: no summary.json, and output.txt holds no JSON summary")
    eval_name = summary.get("eval") or meta.get("eval")
    start = wrapper.get("start") or meta.get("started") or ""
    date = start[:10] if start else f"{name[:4]}-{name[4:6]}-{name[6:8]}"
    home = wrapper.get("home") or meta.get("recall_home")
    return {"name": name, "names": names, "eval": eval_name, "summary": summary, "meta": meta,
            "wrapper": wrapper, "date": date, "prod": wrapper.get("prod"), "home": home}  # fmt: skip


def _benchmark_for(eval_name: str) -> tuple[str, dict]:
    """The registry benchmark whose evals pattern matches, and the conditions its groups name."""
    for bid, b in registry()["benchmarks"].items():
        for pattern in b.get("evals", []):
            m = re.fullmatch(pattern, eval_name or "")
            if m:
                return bid, {k: v for k, v in m.groupdict().items() if v is not None}
    sys.exit(f"eval {eval_name!r} matches no benchmark's evals in registry.json: add it there first")


def _path(obj, path: str):
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _chat_schema(home: str | None) -> str | None:
    db = os.path.join(home, "recall.db") if home else None
    if not db or not os.path.exists(db):
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("select v from meta where k='chat_schema'").fetchone()
    except sqlite3.Error:
        row = None
    con.close()
    return row[0] if row else None


def rows_from_runs(folders: list[str], args) -> list[dict]:
    """One row per registered metric the runs report. Several folders are repeats of one
    setting: the row is their mean, with each folder's value in by_repeat."""
    runs = [read_run(f, not getattr(args, "no_ledger", False)) for f in folders]
    found = {_benchmark_for(r["eval"])[0] for r in runs}
    if len(found) != 1:
        sys.exit(f"the runs are of different benchmarks: {sorted(found)}")
    bid = found.pop()
    bench = registry()["benchmarks"][bid]

    def setting(r: dict) -> dict:
        """Everything the row says about how the run ran, this folder's own eval name included."""
        s, meta = r["summary"], r["meta"]
        conds = dict(_benchmark_for(r["eval"])[1])
        for key, c in bench.get("conditions", {}).items():
            v, src = conds.get(key), c.get("from")
            if v is None and src == "cmd":  # a pattern over the command line
                found = re.search(c["pattern"], r["wrapper"].get("cmd") or " ".join(meta.get("argv", [])))
                v = found.group(1) if found else None
            elif v is None and src:  # summary.<path> or meta.<path>
                where, _, path = src.partition(".")
                v = _path(s if where == "summary" else meta, path)
            v = c.get("default") if v is None else v
            if v is not None:
                conds[key] = c.get("map", {}).get(str(v), v)
        for kv in args.set or []:
            k, _, v = kv.partition("=")
            conds[k] = v
        model = s.get("model") or meta.get("model") or conds.get("model")
        return {
            "split": s.get("split") or meta.get("split"),
            "model": model,
            "judge": s.get("judge") or meta.get("judge"),
            "judge_version": s.get("judge_version") or meta.get("judge_version"),
            "harness": meta.get("agent_harness") or args.harness,
            "conditions": {k: v for k, v in conds.items() if k != "model"},
            "code": dict(resolve_code(r["prod"], r["date"])),
            "date": r["date"],
            "repeats": s.get("repeats") or meta.get("repeats") or 1,
        }

    settings = [setting(r) for r in runs]
    if any(s != settings[0] for s in settings[1:]):
        diff = sorted({k for s in settings[1:] for k in s if s[k] != settings[0][k]})
        sys.exit(f"the runs differ in {', '.join(diff)}: record them separately")
    st = settings[0]
    if args.git:
        if not st["code"]["prod"]:
            sys.exit("--git: these runs have no prod hash to check it against")
        if prod_hash(args.git) != st["code"]["prod"]:
            sys.exit(f"--git {args.git}: its product code is {prod_hash(args.git)}, the runs' is {st['code']['prod']}")
        on_main = (
            subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", args.git, _main_ref()]).returncode == 0
        )
        if not on_main and not _git("tag", "--contains", args.git).strip():
            sys.exit(f"--git {args.git}: not on {_main_ref()} or a tag, so no one else could look it up")
        st["code"]["git"] = _git("rev-parse", "--short=7", args.git).strip()
    corpus = None
    if bench["corpus"] == "private":
        home = runs[0]["home"]
        corpus = {"home": os.path.basename(home.rstrip("/")) if home else None, "chat_schema": _chat_schema(home)}

    def figures(s: dict, spec: dict) -> list[tuple[dict, dict]]:
        """(extra conditions, raw value) for a metric: its key, plus its breakdown if any."""
        out = []
        if spec.get("key"):
            v = _path(s, spec["key"])
            if v is not None:
                out.append(({}, v))
        for cond, src in (spec.get("breakdown") or {}).items():
            items = (_path(s, src[:-2]) or {}).items() if isinstance(src, str) else (
                (k, _path(s, p)) for k, p in src.items()
            )  # fmt: skip
            out += [({cond: k}, v) for k, v in items if v is not None]
        return out

    # n for a metric without its own: the benchmark's n_key, else the first interval's n
    first = runs[0]["summary"]
    n_default = first.get(bench["n_key"]) if bench.get("n_key") else None
    if n_default is None:
        n_default = next((v["n"] for v in first.values() if isinstance(v, dict) and "mean" in v and "n" in v), None)
    wanted = set(args.metrics.split(",")) if args.metrics else None
    out = []
    for mid, spec in bench["metrics"].items():
        if wanted and mid not in wanted:
            continue
        # each folder's figures by their breakdown conditions, so folders match on what they
        # measured, not on the order their summaries list it
        per_run = [{tuple(sorted(c.items())): v for c, v in figures(r["summary"], spec)} for r in runs]
        for extra in per_run[0]:
            vals = [pr.get(extra) for pr in per_run]
            if any(v is None for v in vals):
                print(f"skipped {mid} {dict(extra) or ''}: not every folder reports it", file=sys.stderr)
                continue
            row = {"kind": "measured", "date": st["date"], "benchmark": bid, "metric": mid, "system": "recall"}
            if len(runs) == 1:
                v = vals[0]
                if isinstance(v, dict):
                    row.update(value=v.get("mean"), n=v.get("n", n_default))
                    if v.get("lo") is not None:
                        row["ci"] = [v["lo"], v["hi"]]
                elif isinstance(v, int | float) and not isinstance(v, bool):
                    row.update(value=v, n=n_default)
                else:
                    continue
                if spec.get("by_repeat"):
                    row["by_repeat"] = _path(runs[0]["summary"], spec["by_repeat"])
            else:
                nums = [v.get("mean") if isinstance(v, dict) else v for v in vals]
                ns = {v.get("n", n_default) if isinstance(v, dict) else n_default for v in vals}
                row.update(value=round(sum(nums) / len(nums), 4), n=ns.pop() if len(ns) == 1 else None)
                # each folder's own repeats when they all list them, else one value a folder
                each = [_path(r["summary"], spec["by_repeat"]) if spec.get("by_repeat") else None for r in runs]
                row["by_repeat"] = [x for e in each for x in e] if all(each) else nums
            row.update({k: st[k] for k in ("split", "model", "judge", "judge_version", "harness", "code")})
            row["repeats"] = st["repeats"] * len(runs)
            row["conditions"] = {**st["conditions"], **dict(extra)}
            row["corpus"] = corpus
            row["runs"] = [n for r in runs for n in r["names"]]
            row["pair"], row["role"], row["public"], row["note"] = args.pair, args.role, args.public, args.note
            row["varies"] = sorted(args.allow_diff or []) if args.pair else []
            out.append(complete(row))
    if not out:
        sys.exit(f"the runs report none of {bid}'s registered metrics")
    return out


# --- checks -----------------------------------------------------------------------


def _private_strings() -> list[str]:
    """Case ids, chat ids and question text from the private eval cases, when this machine has
    them: none may appear in the store."""
    out = set()
    for path in glob.glob(os.path.join(CASES, "**", "*.json*"), recursive=True):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            items = (
                [json.loads(line) for line in text.splitlines() if line.strip()]
                if path.endswith("l")
                else [json.loads(text)]
            )
        except json.JSONDecodeError:
            continue
        for c in items:
            for c2 in c if isinstance(c, list) else [c]:
                if not isinstance(c2, dict):
                    continue
                for k in ("id", "query", "question", "answer", "expect", "chats"):
                    for v in c2.get(k) if isinstance(c2.get(k), list) else [c2.get(k)]:
                        if isinstance(v, str) and len(v) >= 5:
                            out.add(v)
    return sorted(out)


def _check_definitions(jsonschema) -> list[str]:
    """registry.json and charts.json against their schemas, plus what a schema cannot say: that
    comparable_on names real fields and the evals patterns compile."""
    errors = []
    for path, schema_path in SCHEMAS.items():
        with open(schema_path, encoding="utf-8") as f:
            v = jsonschema.Draft202012Validator(json.load(f))
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        for e in v.iter_errors(doc):
            if e.context:  # a oneOf with no branch matching: report the closest branch's error
                e = jsonschema.exceptions.best_match(e.context)
            where = ".".join(map(str, e.absolute_path)) or "top"
            errors.append(f"{os.path.basename(path)}: {where}: {e.message[:200]}")
    if errors:
        return errors
    fields = set(FIELDS) | {"code.release", "code.git", "code.prod", "corpus.home", "corpus.chat_schema"}
    for bid, b in registry()["benchmarks"].items():
        for k in b["comparable_on"]:
            ok = k in fields or (k.startswith("conditions.") and k[11:] in b["conditions"])
            if not ok:
                errors.append(f"registry.json: {bid}: comparable_on names {k!r}, which no row has")
        for pattern in b["evals"]:
            try:
                groups = re.compile(pattern).groupindex
            except re.error as e:
                errors.append(f"registry.json: {bid}: evals pattern {pattern!r}: {e}")
                continue
            for g in groups:
                if g != "model" and g not in b["conditions"]:
                    errors.append(f"registry.json: {bid}: evals pattern names {g!r}, which is not a condition")
    return errors


def check(path: str = STORE, base: str | None = None) -> list[str]:
    """Every problem with the store: the registry and chart definitions, each row's schema, id
    and registry references, private content, declared differences between the arms of pairs
    and deltas, charts that do not resolve, a stale figures.json, and (with base) a row of the
    base revision edited or removed. Once a row is malformed the checks that read rows stop."""
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is not installed: run this with `uv run`"]
    errors = _check_definitions(jsonschema)
    if errors:
        return [*errors, "the store was not checked: fix registry.json and charts.json first"]
    with open(SCHEMA, encoding="utf-8") as f:
        validator = jsonschema.Draft202012Validator(json.load(f))
    reg = registry()["benchmarks"]
    with open(path, encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]
    rows, ids, malformed = [], set(), False
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"line {n}: not JSON ({e})")
            malformed = True
            continue
        if not isinstance(r, dict):
            errors.append(f"line {n}: not a JSON object")
            malformed = True
            continue
        problems = [
            f"line {n}: {'.'.join(map(str, e.absolute_path)) or 'row'}: {e.message}" for e in validator.iter_errors(r)
        ]
        errors += problems
        malformed |= bool(problems)
        if list(r) != FIELDS:
            errors.append(f"line {n}: fields must be exactly {FIELDS}, in that order")
            malformed = True
        if PRIVATE.search(line):
            errors.append(f"line {n}: looks private ({PRIVATE.search(line).group(0)!r})")
        if problems:
            continue
        if r["id"] != row_id(r):
            errors.append(f"line {n}: id {r['id']} does not match the row (edited? rows are never edited)")
        if r["id"] in ids:
            errors.append(f"line {n}: duplicate id {r['id']}")
        ids.add(r["id"])
        rows.append((n, r))
        b = reg.get(r["benchmark"])
        if not b:
            errors.append(f"line {n}: benchmark {r['benchmark']!r} is not in registry.json")
            malformed = True
            continue
        if r["metric"] not in b["metrics"]:
            errors.append(f"line {n}: {r['benchmark']} has no metric {r['metric']!r}")
            malformed = True
        for k, v in r["conditions"].items():
            c = b["conditions"].get(k)
            if c is None:
                errors.append(f"line {n}: {r['benchmark']} declares no condition {k!r}")
            elif c.get("values") and v not in c["values"]:
                errors.append(f"line {n}: condition {k}={v!r} is not one of {c['values']}")
    private = _private_strings()
    for n, ln in enumerate(lines, 1):  # say where, never what
        if any(s in ln and re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", ln) for s in private):
            errors.append(f"line {n}: holds a private eval case's id or text")
    if base:
        errors += _check_base(path, lines, base)
    if malformed:
        return [*errors, "references, pairs and charts were not checked: fix the rows above first"]
    by_id = {r["id"]: (n, r) for n, r in rows}
    for n, r in rows:
        for key in ("control", "candidate"):
            if r[key] and r[key] not in by_id:
                errors.append(f"line {n}: {key} names {r[key]}, which is not in the store")
        for ref in r["supersedes"]:
            if ref not in by_id:
                errors.append(f"line {n}: supersedes names {ref}, which is not in the store")
        if r["kind"] == "delta" and r["control"] in by_id and r["candidate"] in by_id:
            ctl, cand = by_id[r["control"]][1], by_id[r["candidate"]][1]
            if (
                not ctl["benchmark"] == cand["benchmark"] == r["benchmark"]
                or not ctl["metric"] == cand["metric"] == r["metric"]
            ):
                errors.append(f"line {n}: a delta and its two arms must share benchmark and metric")
                continue
            undeclared = [f for f in pair_differences(ctl, cand) if not allowed(f, r["varies"])]
            if undeclared:
                errors.append(
                    f"line {n}: the delta's arms also differ in {', '.join(undeclared)}, which varies does not name"
                )
    errors += _check_pairs([r for _, r in rows])
    figures = os.path.join(HERE, "figures.json")
    drawn = {}
    if path == STORE and os.path.exists(figures):
        with open(figures, encoding="utf-8") as f:
            drawn = json.load(f)
    standing = current([r for _, r in rows])
    for name in chart_specs():
        try:
            resolved = json.loads(json.dumps(chart(name, standing)))
        except (LookupError, KeyError, ValueError) as e:
            errors.append(f"chart {name}: {e}")
            continue
        if drawn and drawn.get(name) != resolved:
            errors.append(f"figures.json does not match the store for chart {name}: run make_charts.py")
    return errors


def _check_pairs(rows: list[dict]) -> list[str]:
    """A pair's control and candidate rows, matched on benchmark, metric and the conditions the
    pair does not vary, may differ only in the fields their varies names."""
    errors = []
    for pid, p in sorted(pairs(rows).items()):
        varies = sorted({v for r in p["control"] + p["candidate"] + p["delta"] for v in r["varies"]})
        controls = {pair_key(r, varies): r for r in p["control"]}
        for cand in p["candidate"]:
            ctl = controls.get(pair_key(cand, varies))
            if ctl is None:
                continue
            undeclared = [f for f in pair_differences(ctl, cand) if not allowed(f, varies)]
            if undeclared:
                errors.append(
                    f"pair {pid}: {cand['metric']} {ctl['id']} and {cand['id']} also differ in "
                    f"{', '.join(undeclared)}, which its rows' varies does not name"
                )
    return errors


def _check_base(path: str, lines: list[str], base: str) -> list[str]:
    """Every row of the base revision is still here, unedited. The revision must exist; only a
    store that did not exist at base passes without comparing."""
    ok = subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", "-q", f"{base}^{{commit}}"], capture_output=True)
    if ok.returncode != 0:
        return [f"--base {base}: no such revision here (fetch it first)"]
    rel = os.path.relpath(path, REPO)
    if subprocess.run(["git", "-C", REPO, "cat-file", "-e", f"{base}:{rel}"], capture_output=True).returncode != 0:
        return []  # the store is new since base
    old = subprocess.run(["git", "-C", REPO, "show", f"{base}:{rel}"], capture_output=True, text=True)
    if old.returncode != 0:
        return [f"--base {base}: could not read {rel} there: {old.stderr.strip()}"]
    now = set(lines)
    gone = [ln for ln in old.stdout.splitlines() if ln.strip() and ln not in now]
    return [f"a row in {base} was edited or removed: {json.loads(ln).get('id')}" for ln in gone]


# --- command line -----------------------------------------------------------------


def _append(rows: list[dict]) -> None:
    """Append rows the store does not hold yet, after checking the store with them added."""
    have = {r["id"] for r in load(superseded=True)} if os.path.exists(STORE) else set()
    new = [r for r in rows if r["id"] not in have]
    with open(STORE, encoding="utf-8") as f:
        text = f.read()
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as f:
        f.write(text + "".join(dumps(r) + "\n" for r in new))
    try:
        errors = check(f.name)
    finally:
        os.unlink(f.name)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit("nothing appended: the store would fail check")
    with open(STORE, "a", encoding="utf-8") as f:
        f.writelines(dumps(r) + "\n" for r in new)
    skipped = f" ({len(rows) - len(new)} already there)" if len(new) < len(rows) else ""
    print(f"appended {len(new)} rows to {os.path.relpath(STORE)}{skipped}")


def _show(rows: list[dict]) -> None:
    for r in sorted(rows, key=lambda r: (r["benchmark"], r["metric"], r["date"])):
        code = r["code"] or {}
        who = {"recall": f"recall {code.get('release') or code.get('git') or code.get('prod') or '?'}"}.get(
            r["system"], r["system"]
        )
        who = "delta" if r["kind"] == "delta" else who
        ci = f" [{r['ci'][0]}, {r['ci'][1]}]" if r["ci"] else ""
        conds = " ".join(f"{k}={v}" for k, v in sorted(r["conditions"].items()))
        hidden = "" if r["public"] else " (not public)"
        print(f"{r['id']} {r['date']} {r['benchmark']:28} {r['metric']:16} {r['value']}{ci} n={r['n']} "
              f"x{r['repeats'] or '?'} {who} {r['model'] or ''} {conds}{hidden}")  # fmt: skip


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="list rows")
    for k in ("benchmark", "metric", "system", "release", "kind", "date"):
        s.add_argument(f"--{k}")
    sub.add_parser("pairs", help="control and candidate rows by pair, with their deltas")
    r = sub.add_parser("record", help="turn run folders into rows (several folders: repeats of one setting)")
    r.add_argument("runs", nargs="+")
    r.add_argument("--pair", help="a pair id shared by a same-day control and its candidate")
    r.add_argument("--role", choices=["control", "candidate"])
    r.add_argument("--git", help="the commit, when the prod hash matches no tag (checked against the runs' prod)")
    rp = sub.add_parser(
        "record-pair", help="a same-day control and candidate (pair.sh's two folders) under one pair id"
    )
    rp.add_argument("pair", help="the pair id, such as 2026-10-17-v042-vs-v050-qa")
    rp.add_argument("control")
    rp.add_argument("candidate")
    for o in (r, rp):
        o.add_argument("--public", action="store_true", help="cleared for the README, docs and the site")
        o.add_argument(
            "--harness", help="the agent harness, for runs whose meta.json does not say (before toolsearch-v1)"
        )
        o.add_argument("--metrics", help="only these metrics, comma separated")
        o.add_argument("--set", action="append", help="a condition the run does not record, key=value")
        o.add_argument("--note")
        o.add_argument(
            "--allow-diff",
            action="append",
            metavar="FIELD",
            help="with a pair: a field the arms are meant to differ in (code, model, corpus, conditions.arm, date...)",
        )
        o.add_argument(
            "--no-ledger", action="store_true", help="record although no LEDGER.md says which runs are INVALID"
        )
        o.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("add", help="append hand-made rows (third-party or derived figures) from a JSON file")
    a.add_argument("file", help="a JSON object or list of objects, or - for stdin")
    a.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("check", help="validate the store (needs jsonschema: run with uv run)")
    c.add_argument("--base", help="a git revision whose rows must all still be present, unedited")
    args = p.parse_args()

    if args.cmd == "show":
        where = {
            k: getattr(args, k)
            for k in ("benchmark", "metric", "system", "release", "kind", "date")
            if getattr(args, k)
        }
        _show(select(load(), where))
    elif args.cmd == "pairs":
        for pid, pr in sorted(pairs(load()).items()):
            print(f"{pid}{'' if pr['same_day'] else '  (not the same day)'}")
            for role in ("control", "candidate", "delta"):
                for row in pr[role]:
                    ci = f" [{row['ci'][0]}, {row['ci'][1]}]" if row["ci"] else ""
                    print(f"  {role:9} {row['id']} {row['date']} {row['metric']} {row['value']}{ci}")
    elif args.cmd == "record":
        if (args.pair is None) != (args.role is None):
            sys.exit("--pair and --role go together")
        if args.pair and not args.allow_diff:
            sys.exit(
                "--pair needs --allow-diff FIELD for each field the arms are meant to differ in (code, model, ...)"
            )
        rows = rows_from_runs(args.runs, args)
        for row in rows:
            print(dumps(row))
        if not args.dry_run:
            _append(rows)
    elif args.cmd == "record-pair":
        args.git = None
        arms = {}
        for role in ("control", "candidate"):
            arms[role] = rows_from_runs([getattr(args, role)], argparse.Namespace(**{**vars(args), "role": role}))
        varies = sorted(args.allow_diff or [])
        controls = {pair_key(r, varies): r for r in arms["control"]}
        benchmarks = sorted({r["benchmark"] for r in arms["control"] + arms["candidate"]})
        if len(benchmarks) > 1:
            sys.exit(f"the arms are of different benchmarks: {', '.join(benchmarks)}")
        undeclared = {}
        for cand in arms["candidate"]:
            if pair_key(cand, varies) in controls:
                for f in pair_differences(controls[pair_key(cand, varies)], cand):
                    if not allowed(f, varies):
                        undeclared.setdefault(f, []).append(cand["metric"])
        if undeclared:
            listed = "; ".join(f"{f} ({', '.join(sorted(set(m)))})" for f, m in sorted(undeclared.items()))
            sys.exit(
                f"the arms differ in {listed}: name each with --allow-diff if the pair means it, or do not pair them"
            )
        if not varies:
            sys.exit(
                "the arms differ in nothing a pair tracks: name the difference the pair is about with --allow-diff"
            )
        rows = arms["control"] + arms["candidate"]
        for row in rows:
            print(dumps(row))
        if not args.dry_run:
            _append(rows)
    elif args.cmd == "add":
        if args.file == "-":
            data = json.load(sys.stdin)
        else:
            with open(args.file, encoding="utf-8") as f:
                data = json.load(f)
        try:
            rows = [complete(d) for d in (data if isinstance(data, list) else [data])]
        except (ValueError, AttributeError) as e:
            sys.exit(f"not added: {e}")
        for row in rows:
            print(dumps(row))
        if not args.dry_run:
            _append(rows)
    elif args.cmd == "check":
        errors = check(base=args.base)
        print("\n".join(errors) or f"ok: {len(load(superseded=True))} rows, {len(chart_specs())} charts")
        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

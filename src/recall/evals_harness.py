"""What the v2 evals share: case files with a train/test split, one folder per run, scores
with bootstrap confidence intervals, a static results page and the ledger.

A run folder (RUNS/<YYYYMMDD-HHMM>-<who>-<eval>/, or $RECALL_RUN_DIR when the shared run wrapper
sets it) holds:
  meta.json      what ran: eval, split, code id, models, command line
  cases.jsonl    one line per case (and per repeat): status, outcome, numbers
  transcripts/   one JSON and one HTML file per case: everything the case did
  summary.json   the aggregates printed at the end
  index.html     the results page; it loads nothing from the network
Test cases go under test/ instead, and the terminal and index.html show only their
aggregates: whoever tunes recall on the train split must not read test cases.

Plumbing failures (timeouts, API errors, truncated output, a case naming a chat the index
does not hold) are counted apart and left out of every score, so a flaky run reads as flaky
rather than as worse. Each run appends one line to the ledger beside the runs folder.
"""

import glob
import hashlib
import html
import json
import os
import random
import re
import statistics
import subprocess
import sys
import threading
from datetime import datetime

from . import paths

RUNS = os.path.expanduser(os.environ.get("RECALL_EVAL_RUNS", "~/.agents/recall-eval/runs"))
CASES = os.path.expanduser(os.environ.get("RECALL_EVAL_CASES", "~/.agents/recall/evals/v2"))
JUDGE = "claude:claude-sonnet-5-5"  # must differ from the model under test
SEED = 7
RESAMPLES = 2000
HEADROOM = 0.95  # a score this high has little left to show; the cases need to get harder
OK = "ok"


# --- cases ------------------------------------------------------------------------


def load_cases(
    kind: str, split: str, path: str | None = None, ids: list[str] | None = None, limit: int | None = None
) -> list[dict]:
    """kind: search_v2, qa_v2 or memory_v2. Reads every <kind>*.jsonl in CASES (each case
    builder writes its own file), or only `path`. limit: a fixed random sample of N cases."""
    files = [path] if path else sorted(glob.glob(os.path.join(CASES, f"{kind}*.jsonl")))
    if not files:
        sys.exit(f"no {kind} cases: expected {kind}*.jsonl in {CASES}")
    required = {"search_v2": ("query", "expect"), "qa_v2": ("question", "answerable"), "memory_v2": ("chats",)}
    cases, seen = [], set()
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                where = f"{os.path.basename(f)}:{n}"
                try:
                    c = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"skipped {where}: not JSON ({e})", file=sys.stderr)
                    continue
                missing = [k for k in ("id", *required.get(kind, ())) if k not in c]
                if missing:
                    print(f"skipped {where}: no {', '.join(missing)}", file=sys.stderr)
                    continue
                if c["id"] in seen:
                    print(f"skipped {where}: duplicate id {c['id']}", file=sys.stderr)
                    continue
                seen.add(c["id"])
                c.setdefault("split", "train")
                c.setdefault("tags", [])
                c["file"] = os.path.basename(f)
                cases.append(c)
    # holdout: cases kept back for a final check after test has been used for several decisions.
    # "all" leaves them out, and they are stored and shown like test: aggregates only.
    picked = [c for c in cases if c["split"] == split or (split == "all" and c["split"] != "holdout")]
    if split == "holdout":
        for c in picked:
            c["split"] = "test"
    if ids:
        picked = [c for c in picked if c["id"] in ids]
    if limit and len(picked) > limit:
        keep = {c["id"] for c in random.Random(SEED).sample(picked, limit)}
        picked = [c for c in picked if c["id"] in keep]
    if not picked:
        sys.exit(f"no {kind} cases in the {split} split")
    return picked


def corpus():
    """The chat index, opened only if its schema matches this code. store.connect() drops and
    rebuilds the chat tables when the schema version changes, and the frozen eval index never
    re-indexes, so opening it with newer code would leave it empty."""
    import sqlite3

    from .chats import store

    if os.path.exists(paths.DB):
        con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        row = con.execute("select v from meta where k='chat_schema'").fetchone()
        con.close()
        if row and row[0] != store.CHAT_SCHEMA_VERSION:
            sys.exit(
                f"{paths.DB} holds chat schema {row[0]} but this code expects {store.CHAT_SCHEMA_VERSION}: "
                "opening it would drop the frozen index. Rebuild a scratch index for this schema first."
            )
    return store.connect()


# --- scores -----------------------------------------------------------------------


def ci(values: list[float]) -> dict:
    """Mean and 95% bootstrap interval over cases (percentile method, fixed seed)."""
    return ratio_ci([(v, 1) for v in values])


def ratio_ci(pairs: list[tuple[float, float]]) -> dict:
    """sum(num) / sum(den) with a 95% bootstrap interval over cases: for scores pooled across
    cases of different sizes, such as memories found out of memories expected."""
    pairs = [p for p in pairs if p[1]]
    if not pairs:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    rng = random.Random(SEED)
    n = len(pairs)
    stats = []
    for _ in range(RESAMPLES):
        sample = rng.choices(pairs, k=n)
        stats.append(sum(p[0] for p in sample) / sum(p[1] for p in sample))
    stats.sort()
    mean = sum(p[0] for p in pairs) / sum(p[1] for p in pairs)
    lo, hi = stats[int(0.025 * RESAMPLES)], stats[int(0.975 * RESAMPLES) - 1]
    return {"mean": round(mean, 3), "lo": round(lo, 3), "hi": round(hi, 3), "n": n}


def paired(mine: dict[str, float], theirs: dict[str, float]) -> dict:
    """Paired comparison on the cases both runs scored: mean difference (this run minus the
    other) with a 95% bootstrap interval, and how many cases got better or worse."""
    common = sorted(set(mine) & set(theirs))
    diffs = [mine[k] - theirs[k] for k in common]
    out = ci(diffs)
    out["better"] = sum(d > 0 for d in diffs)
    out["worse"] = sum(d < 0 for d in diffs)
    return out


def fmt(s: dict | None, signed: bool = False) -> str:
    if not s or s.get("mean") is None:
        return "n/a"
    f = "{:+.3f}" if signed else "{:.3f}"
    out = f"{f.format(s['mean'])} [{f.format(s['lo'])}, {f.format(s['hi'])}] n={s['n']}"
    if "better" in s:
        out += f" ({s['better']} better, {s['worse']} worse)"
    return out


def spread(values: list[float]) -> dict:
    if not values:
        return {}
    v = sorted(values)
    return {
        "median": round(statistics.median(v), 1),
        "mean": round(statistics.fmean(v), 1),
        "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 1),
    }


def by_tag(rows: list[dict], value) -> dict:
    """value(row) -> float, per tag, sorted by tag."""
    tags: dict[str, list[float]] = {}
    for r in rows:
        for t in r.get("tags") or ["(untagged)"]:
            tags.setdefault(t, []).append(value(r))
    return {t: ci(v) for t, v in sorted(tags.items())}


def headroom(metrics: dict[str, dict]) -> list[str]:
    return [
        f"{name} is {s['mean']:.3f}: near the ceiling, so this eval can barely show a gain; add harder cases"
        for name, s in metrics.items()
        if s and s.get("mean") is not None and s["mean"] >= HEADROOM
    ]


def plumbing(rows: list[dict]) -> dict:
    """Counts of rows that failed for reasons other than the answer, by status."""
    out: dict[str, int] = {}
    for r in rows:
        if r["status"] != OK:
            out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def against(run_dir: str, value) -> dict[str, float]:
    """Per-case scores from an earlier run folder (both splits), averaged over repeats."""
    scores: dict[str, list[float]] = {}
    for path in (os.path.join(run_dir, "cases.jsonl"), os.path.join(run_dir, "test", "cases.jsonl")):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                if r["status"] == OK and (v := value(r)) is not None:
                    scores.setdefault(r["id"], []).append(v)
    if not scores:
        sys.exit(f"no scored cases in {run_dir}")
    return {k: statistics.fmean(v) for k, v in scores.items()}


def mismatch(run_dir: str, meta: dict) -> list[str]:
    """Ways an earlier run differs from this one that make a paired comparison unfair."""
    try:
        with open(os.path.join(run_dir, "meta.json")) as f:
            other = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [f"{run_dir} has no readable meta.json"]
    return [
        f"the --against run differs in {k}: {other.get(k)} there, {meta.get(k)} here"
        for k in ("eval", "judge", "judge_version", "keyword_only", "agent_harness")
        if other.get(k) != meta.get(k)
    ]


def check_candidates_repeat(k: int | None, source: dict) -> None:
    """--candidates-repeat K names a repeat the --from-candidates run has; past it the replay
    would wrap round to another repeat without saying so."""
    if k is not None and not 1 <= k <= source["repeats"]:
        sys.exit(f"--candidates-repeat {k}: {source['dir']} has repeats 1 to {source['repeats']}")


def judge_client(spec: str, cache: bool = True):
    from . import config
    from .llm import Client, from_spec

    return Client(from_spec(spec, config.load()["model"]), cache=cache)


def same_model(a: str, b: str) -> bool:
    """Is the model under test the judge? Aliases count: "sonnet" is Sonnet 5.5."""

    def norm(s: str) -> str:
        s = s.split(":", 1)[-1].lower()
        return {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}.get(s, s)

    return norm(a) == norm(b)


# --- run folder -------------------------------------------------------------------


def code_id() -> str:
    """The ledger's code id: the first 8 hex digits of sha1(`git diff HEAD`) in the repo the
    running code comes from (untracked files do not count). A copy of src outside git gets a
    hash of its files instead, marked with "src:"."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        top = subprocess.run(
            ["git", "-C", here, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
        diff = subprocess.run(["git", "-C", top, "diff", "HEAD"], capture_output=True, check=True).stdout
        return hashlib.sha1(diff).hexdigest()[:8]
    except (OSError, subprocess.CalledProcessError):
        h = hashlib.sha1()
        for path in sorted(glob.glob(os.path.join(here, "**", "*.*"), recursive=True)):
            if path.endswith((".py", ".md")):
                with open(path, "rb") as f:
                    h.update(path[len(here) :].encode() + f.read())
        return "src:" + h.hexdigest()[:8]


class Run:
    """One eval run: its folder, per-case lines and transcripts, summary and ledger line."""

    def __init__(self, name: str, split: str, args: dict, who: str | None = None, runs_dir: str | None = None):
        self.name, self.split = name, split
        self.who = who or os.environ.get("RECALL_EVAL_WHO") or "harness"
        self.runs = os.path.expanduser(runs_dir or RUNS)
        base = os.path.join(self.runs, f"{datetime.now():%Y%m%d-%H%M}-{self.who}-{name}")
        given = os.environ.get("RECALL_RUN_DIR") if not runs_dir else None
        if given:  # the run wrapper's folder: the run's cases and its output.txt stay together
            given = os.path.abspath(os.path.expanduser(given))
            self.runs = os.path.dirname(given)
            base = given if not os.path.exists(os.path.join(given, "meta.json")) else os.path.join(given, name)
        self.dir, i = base, 2
        while os.path.exists(os.path.join(self.dir, "meta.json")) or (os.path.exists(self.dir) and not given):
            self.dir, i = f"{base}-{i}", i + 1
        os.makedirs(self.dir, exist_ok=True)
        self.code = code_id()
        self.rows: list[dict] = []
        self._lock = threading.Lock()
        self.meta = {
            "eval": name,
            "split": split,
            "who": self.who,
            "code": self.code,
            "started": datetime.now().isoformat(timespec="seconds"),
            "argv": sys.argv,
            "recall_home": paths.HOME,
            "recall_memory": paths.MEMORY,
            **args,
        }
        _write_json(os.path.join(self.dir, "meta.json"), self.meta)

    def folder(self, case: dict) -> str:
        return os.path.join(self.dir, "test") if case.get("split") == "test" else self.dir

    def record(self, case: dict, row: dict, transcript: dict) -> dict:
        """Write one case's line and transcript; returns the line as written."""
        folder = self.folder(case)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", case["id"]) + (f"-r{row['repeat']}" if row.get("repeat") else "")
        os.makedirs(os.path.join(folder, "transcripts"), exist_ok=True)
        row = {"id": case["id"], "split": case.get("split"), "tags": case.get("tags", []), **row}
        row["transcript"] = f"transcripts/{name}.html"
        first = [k for k in ("answer", "judge", "update_judge") if k in transcript]  # what a reader wants first
        transcript = {"case": case, "result": row, **{k: transcript[k] for k in first}, **transcript}
        _write_json(os.path.join(folder, "transcripts", f"{name}.json"), transcript)
        with open(os.path.join(folder, "transcripts", f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(_transcript_page(f"{self.name} · {case['id']}", transcript))
        with self._lock:
            with open(os.path.join(folder, "cases.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            self.rows.append(row)
        return row

    def finish(self, summary: dict, headline: str, columns: list[tuple[str, str]], note: str = "") -> dict:
        """Write summary.json and the results pages, append the ledger line, print the summary.
        columns: (label, row key) pairs for the per-case table."""
        summary = {"eval": self.name, "split": self.split, "code": self.code, "run": self.dir, **summary}
        self.meta["finished"] = datetime.now().isoformat(timespec="seconds")
        _write_json(os.path.join(self.dir, "meta.json"), self.meta)  # with what the eval added since
        _write_json(os.path.join(self.dir, "summary.json"), summary)
        train = [r for r in self.rows if r.get("split") != "test"]
        test = [r for r in self.rows if r.get("split") == "test"]
        with open(os.path.join(self.dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(_results_page(self.meta, summary, train, columns, n_test=len(test)))
        if test:
            with open(os.path.join(self.dir, "test", "index.html"), "w", encoding="utf-8") as f:
                f.write(_results_page(self.meta, summary, test, columns, n_test=0))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = (
            f"- {stamp} | {self.who} | {self.name} {self.split} | code={self.code} | {headline} | {self.dir} | {note}"
        )
        with open(os.path.join(os.path.dirname(self.runs), "LEDGER.md"), "a", encoding="utf-8") as f:
            f.write(line.rstrip(" |") + "\n")
        show(summary)
        print(f"\n{headline}\nresults: {os.path.join(self.dir, 'index.html')}")
        return summary


def show(summary: dict) -> None:
    """Print a summary one line per metric; summary.json keeps the full structure."""

    def is_ci(v) -> bool:
        return isinstance(v, dict) and "lo" in v and "mean" in v

    for key, v in summary.items():
        if is_ci(v):
            print(f"{key:26} {fmt(v, signed=key.startswith('Δ'))}")
        elif isinstance(v, dict) and any(is_ci(x) or isinstance(x, dict) for x in v.values()):
            print(f"{key}:")
            for sub, x in v.items():
                if is_ci(x):
                    print(f"  {sub:24} {fmt(x, signed=key == 'against')}")
                elif isinstance(x, dict) and x and all(is_ci(y) for y in x.values()):
                    print(f"  {sub:24} " + "   ".join(f"{m} {fmt(y)}" for m, y in x.items()))
                else:
                    print(f"  {sub:24} {json.dumps(x, default=str)}")
        else:
            print(f"{key:26} {json.dumps(v, default=str)}")


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# --- results pages ----------------------------------------------------------------
# Static HTML with inline styles, so a run folder can be opened, copied or archived as is.

_CSS = """
:root { --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6a66; --line:#e3e2de; --code:#f1f0ec;
  --good:#1f7a3a; --bad:#b3261e; --warn:#8a5a00; }
@media (prefers-color-scheme: dark) { :root { --bg:#151514; --fg:#e8e6e1; --muted:#9a988f;
  --line:#2c2b29; --code:#1e1d1b; --good:#6cc884; --bad:#ff8a80; --warn:#e6b450; } }
body { background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system, system-ui, sans-serif;
  margin:0 auto; padding:24px 16px; max-width:1100px; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:28px 0 8px; }
.muted { color:var(--muted); } .ok { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); }
table { border-collapse:collapse; width:100%; margin:8px 0; display:block; overflow-x:auto; }
th, td { text-align:left; padding:4px 10px 4px 0; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-weight:600; color:var(--muted); }
pre { background:var(--code); padding:10px; border-radius:6px; overflow-x:auto; white-space:pre-wrap;
  word-break:break-word; font:12px/1.45 ui-monospace, Menlo, monospace; }
.step { border-left:3px solid var(--line); padding:2px 0 2px 12px; margin:10px 0; }
.step b { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
a { color:inherit; }
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>{body}</body></html>"
    )


def _cell(v) -> str:
    if v is None:
        return '<span class="muted">·</span>'
    if isinstance(v, bool):
        return '<span class="ok">yes</span>' if v else '<span class="bad">no</span>'
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, dict | list):
        return html.escape(json.dumps(v)[:160])
    return html.escape(str(v))


def _results_page(meta: dict, summary: dict, rows: list[dict], columns, n_test: int) -> str:
    out = [f"<h1>{html.escape(meta['eval'])} · {html.escape(meta['split'])}</h1>"]
    out.append(
        f'<p class="muted">{html.escape(meta["started"])} · {html.escape(meta["who"])} · code '
        f"{html.escape(meta['code'])}</p>"
    )
    for w in summary.get("warnings", []):
        out.append(f'<p class="warn">{html.escape(w)}</p>')
    out.append("<h2>Aggregates</h2><table><tr><th>metric</th><th>value</th><th>95% CI</th><th>n</th></tr>")
    for key, v in summary.items():
        if isinstance(v, dict) and "mean" in v and "lo" in v:
            ci_text = f"[{v['lo']}, {v['hi']}]" if v["lo"] is not None else ""
            out.append(f"<tr><td>{html.escape(key)}</td><td>{_cell(v['mean'])}</td><td>{ci_text}</td>")
            out.append(f"<td>{v['n']}</td></tr>")
    out.append("</table>")
    rest = {
        k: v for k, v in summary.items() if not (isinstance(v, dict) and "mean" in v and "lo" in v) and k != "warnings"
    }
    if n_test:
        out.append(f'<p class="muted">{n_test} test case runs are under test/ and are not listed here.</p>')
    if rows:
        out.append(f"<h2>Cases ({len(rows)})</h2><table><tr><th>case</th><th>status</th>")
        out += [f"<th>{html.escape(label)}</th>" for label, _ in columns]
        out.append("<th>tags</th></tr>")
        for r in rows:
            name = r["id"] + (f" r{r['repeat']}" if r.get("repeat") else "")
            status = r["status"]
            out.append(
                f'<tr><td><a href="{html.escape(r["transcript"])}">{html.escape(name)}</a></td>'
                f'<td class="{"ok" if status == OK else "warn"}">{html.escape(status)}</td>'
            )
            out += [f"<td>{_cell(r.get(key))}</td>" for _, key in columns]
            out.append(f"<td class='muted'>{html.escape(', '.join(r.get('tags', [])))}</td></tr>")
        out.append("</table>")
    for heading, data in (("Details: by tag, cost, plumbing, grader", rest), ("Run settings", meta)):
        out.append(f"<h2>{heading}</h2><details><summary>show</summary><pre>")
        out.append(f"{html.escape(json.dumps(data, indent=2, default=str))}</pre></details>")
    return _page(f"{meta['eval']} {meta['split']}", "".join(out))


def _transcript_page(title: str, t: dict) -> str:
    """A case's transcript as a page: the case, its result, any conversation as steps, then
    every other part of the transcript as JSON or text."""
    out = [f"<h1>{html.escape(title)}</h1>", '<p><a href="../index.html">all cases</a></p>']
    for key, value in t.items():
        if key == "events":
            continue  # the raw event stream is in the JSON file beside this one
        out.append(f"<h2>{html.escape(key)}</h2>")
        if key == "steps":
            for s in value:
                text = s.get("text") or json.dumps(s.get("input"), indent=2)
                label = s["kind"].replace("_", " ") + (f" · {s['tool']}" if s.get("tool") else "")
                if s.get("chars") is not None:
                    label += f" · {s['chars']:,} chars"
                out.append(f'<div class="step"><b>{html.escape(label)}</b><pre>{html.escape(text)}</pre></div>')
        elif isinstance(value, str):
            out.append(f"<pre>{html.escape(value)}</pre>")
        else:
            out.append(f"<pre>{html.escape(json.dumps(value, indent=2, default=str))}</pre>")
    return _page(title, "".join(out))

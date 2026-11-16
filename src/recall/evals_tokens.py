"""What an agent eval costs, and how much of it is recall's: three figures for every agent run.

  recall eval tokens RUN [RUN...] [--out DIR] [--check-cost]

1. recall's own tokens per task, counted once. A task's context grows call by call, and the growth
   between two calls is what the step added: the agent's own turn (its output, with thinking
   hidden, so Claude Code reports only a run total) plus the results of the tools it called, and,
   when it asked tool search for recall's tools, their definitions. Per tool, the growth of the
   steps with that tool's result alone is regressed on the result's characters: the slope is
   tokens per character (an API count, not a guess), the intercept the agent's own turn. Then:
     results       characters x the tool's slope, in every step
     definitions   what a tool-search step added beyond the agent's turn and the results in its reply
     instructions  the server's instructions and tool names (characters x the pooled slope)
     hook          the block a hook injected, from `injected_chars` where the run recorded it
                   (older runs: unknown, 0); a hook that Claude Code reports in its events counts too
   Each part is an estimate with the fit's quality in the summary (`recall_fit`: n, r2); only the
   total context growth and the usage counts are the API's own. Same-day arms give exact
   differences (the first call's context of tools minus none is the instructions and tool names).
2. Cost, from the agent's own reported usage: the four counts (input, cache write, cache read,
   output) are in every row already; `cost_weighted` is their sum weighted by PRICE_RATIOS, in
   uncached-input tokens. `cost_per_solved` is the total over the number of solved tasks.
   `recall_cost_weighted` is what recall's own tokens cost: written once, then read on every later call.
3. Gains and breaks against a no-memory arm: `recall eval compare` (evals_compare.gains_breaks).

Re-scoring stored runs copies cases.jsonl, meta.json and summary.json into --out with the new
fields and leaves the run folders as they are. Standard library only."""

import argparse
import glob
import json
import os
import re
import shutil
import statistics
import sys

# What a token costs, relative to an uncached input token: one place, the model's price ratios.
# Values: Anthropic's published ratios for Claude models (cache read 0.1, a 5-minute cache write
# 1.25, a 1-hour write 2, output 5). `--check-cost` weighs stored runs against the cost Claude
# Code reports for them (it matched to 6 digits on 13 Nov); another model with other ratios sets
# RECALL_EVAL_PRICE_RATIOS to a JSON object with any of these keys.
PRICE_RATIOS = {"input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1, "output": 5.0}
try:
    PRICE_RATIOS.update(json.loads(os.environ.get("RECALL_EVAL_PRICE_RATIOS") or "{}"))
except ValueError:
    sys.exit("RECALL_EVAL_PRICE_RATIOS must be a JSON object")

# Where a run has too few single-result steps to fit a tool (a 40-task run has about 25 for the
# busiest), the fit falls back to this table: per tool, tokens per character of its results and the
# agent's own turn that comes with a step, measured on 13 Nov 2026 from the 0.6.1 candidate's qa_v2,
# SWE-ContextBench agent and MemoryCode hook runs pooled (666 to 784 steps; r2 0.96 to 0.99).
# It is only a fallback: a run's own steps win, and `recall_fit` says which tools used it. Results
# whose text format changes (shorter results) should be fitted from their own run, never this table.
REFERENCE_FIT = {
    "fits": {
        "mcp__recall__search_chats": {"b": 0.48, "a": 219.0},
        "mcp__recall__read_chat": {"b": 0.389, "a": 301.0},
        "mcp__recall__memory_search": {"b": 0.332, "a": 234.0},
        "mcp__recall__project_memory": {"b": 0.303, "a": 167.0},
    },
    "pooled": {"b": 0.384, "a": 328.0},
    "text": 0.31,  # prose (the server's instructions, a hook block): tokens per character
}
RECALL = "mcp__recall__"
TOOL_SEARCH = "ToolSearch"
MIN_STEPS = 30  # single-result steps a tool needs for a fit of its own
FALLBACK_INSTRUCTIONS_CHARS = 700  # when the server's text cannot be read (a run's own record wins)


def solved_of(row: dict, eval_name: str) -> int | None:
    """Whether a task counts as solved: qa_v2 a correct answer, SWE-ContextBench a gold chat named,
    MemoryCode a request whose every check passes (a partly right one is not solved)."""
    if "agent" not in eval_name and eval_name != "qa_v2":
        return None
    if eval_name == "qa_v2":
        v = row.get("correct")
    elif eval_name.startswith("memorycode"):
        v = None if row.get("score") is None else row["score"] >= 1.0 - 1e-9
    else:
        v = row.get("hit")
    return None if v is None else int(bool(v))


def weighted(usage: dict) -> float:
    """A run's usage in uncached-input tokens, by PRICE_RATIOS."""
    cc = usage.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = usage.get("cache_creation_input_tokens") or 0, 0
    r = PRICE_RATIOS
    return (
        (usage.get("input_tokens") or 0) * r["input"]
        + (w5 or 0) * r["cache_write_5m"]
        + (w1 or 0) * r["cache_write_1h"]
        + (usage.get("cache_read_input_tokens") or 0) * r["cache_read"]
        + (usage.get("output_tokens") or 0) * r["output"]
    )


def _write_ratio(usage: dict) -> float:
    cc = usage.get("cache_creation") or {}
    n5, n1 = cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0
    if n5 + n1 == 0:
        return PRICE_RATIOS["cache_write_5m"]
    return (n5 * PRICE_RATIOS["cache_write_5m"] + n1 * PRICE_RATIOS["cache_write_1h"]) / (n5 + n1)


# --- one transcript -> its steps -------------------------------------------------------


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def steps_of(transcript: dict) -> dict:
    """The API calls of one run from its stream-json events: each call's context size, what it
    wrote, and the results that came back for its tool calls."""
    calls, index, byid = [], {}, {}
    cur = None
    names = []
    hook_chars = 0
    for e in transcript.get("events") or []:
        kind, m = e.get("type"), e.get("message") or {}
        if kind == "system" and e.get("subtype") == "init":
            names = [t for t in e.get("tools") or [] if t.startswith(RECALL)]
        elif kind == "system" and str(e.get("subtype", "")).startswith("hook") and e.get("output"):
            hook_chars += len(str(e["output"]))
        elif kind == "assistant" and m.get("id"):
            u = m.get("usage") or {}
            size = sum(
                u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            if m["id"] not in index:
                index[m["id"]] = len(calls)
                calls.append({"ctx": 0, "wrote": 0, "results": []})
            k = index[m["id"]]
            calls[k]["ctx"] = max(calls[k]["ctx"], size)
            cur = k
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    byid[b["id"]] = k
                    calls[k]["wrote"] += len(json.dumps(b.get("input") or {})) + len(b["name"])
                elif b.get("type") == "text":
                    calls[k]["wrote"] += len(b.get("text") or "")
        elif kind == "user" and cur is not None:
            content = m.get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    k = byid.get(b.get("tool_use_id"), cur)
                    cc = b.get("content")
                    refs = (
                        [y["tool_name"] for y in cc if isinstance(y, dict) and y.get("type") == "tool_reference"]
                        if isinstance(cc, list)
                        else []
                    )
                    calls[k]["results"].append({"tool": b.get("tool_use_id"), "chars": len(_text(cc)), "refs": refs})
    # name each result by the tool call that asked for it
    called = {}
    for e in transcript.get("events") or []:
        for b in (e.get("message") or {}).get("content") or [] if e.get("type") == "assistant" else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                called[b["id"]] = b["name"]
    for c in calls:
        for r in c["results"]:
            r["tool"] = called.get(r["tool"], "?")
    return {"calls": calls, "names": names, "hook_chars": hook_chars}


def growth(steps: dict) -> list[float]:
    """What each call added to the next call's context: the agent's own turn (its output, which
    Claude Code reports only as a run total, with its thinking hidden) plus the step's results."""
    calls = steps["calls"]
    return [max(0.0, calls[k + 1]["ctx"] - calls[k]["ctx"]) for k in range(len(calls) - 1)]


# --- a run: measure tokens per character on its single-result steps, then size every part ---


def _line(points: list[tuple[float, float]]) -> dict:
    """growth = a + b * chars by least squares over (chars, growth) points: b is the tokens a
    character of the result costs, a the agent's own turn that comes with every step."""
    n = len(points)
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    b = sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else 0.0
    a = my - b * mx
    sst = sum((y - my) ** 2 for _, y in points)
    sse = sum((y - a - b * x) ** 2 for x, y in points)
    return {"a": a, "b": max(b, 0.0), "n": n, "r2": 1 - sse / sst if sst else 0.0}


def _calibrate(per_row: list[dict], fallback: dict | None = REFERENCE_FIT) -> dict:
    """Per tool, growth against result characters over the steps with that tool's result alone
    (at least MIN_STEPS of them, else the fallback table), and the prose slope of an injected block
    from the first call's context against the block's characters."""
    points: dict[str, list[tuple[float, float]]] = {}
    for s in per_row:
        for k, g in enumerate(s["growth"]):
            rs = s["calls"][k]["results"]
            if len(rs) == 1 and rs[0]["tool"].startswith(RECALL) and rs[0]["chars"]:
                points.setdefault(rs[0]["tool"], []).append((rs[0]["chars"], g))
    every = [p for v in points.values() for p in v]
    fallback = fallback or {"fits": {}, "pooled": {"a": 0.0, "b": 0.3}, "text": None}
    own = len(every) >= MIN_STEPS
    pooled = {**_line(every), "source": "run"} if own else {**fallback["pooled"], "n": len(every), "source": "table"}
    fits = {}
    for t, v in points.items():
        if len(v) >= MIN_STEPS:
            fits[t] = {**_line(v), "source": "run"}
        else:
            fits[t] = {**(fallback["fits"].get(t) or fallback["pooled"]), "n": len(v), "source": "table"}
    block = [(s["injected"], s["calls"][0]["ctx"]) for s in per_row if s.get("injected") is not None]
    text = _line(block) if len(block) >= MIN_STEPS and len({x for x, _ in block}) > 5 else None
    use_text = text is not None and text["b"] > 0
    return {
        "fits": fits,
        "pooled": pooled,
        "text": text["b"] if use_text else fallback["text"],
        "text_source": "run" if use_text else "table",
        "text_n": len(block),
    }


def _instructions_chars(row: dict) -> int:
    if row.get("instructions_chars"):
        return row["instructions_chars"]
    try:
        from . import server

        return len(server.mcp.instructions or "")
    except Exception:
        return FALLBACK_INSTRUCTIONS_CHARS


def parts_of(s: dict, cal: dict, row: dict, hook_chars: int | None) -> dict:
    """recall's tokens in one run, once each, with the call that first read each part (so its
    re-reading can be priced). A result is its characters at the tool's measured tokens per
    character; tool definitions are what a tool-search step added beyond the agent's own turn
    and the results sharing its reply."""
    calls, g = s["calls"], s["growth"]
    pooled = cal["pooled"]
    parts = {"definitions": [], "instructions": [], "hook": [], "results": {}, "calls": len(calls)}
    for k, growth_k in enumerate(g):
        rs = calls[k]["results"]
        sized = {}
        for r in rs:
            if r["tool"].startswith(RECALL):
                sized[id(r)] = cal["fits"].get(r["tool"], pooled)["b"] * r["chars"]
                parts["results"].setdefault(r["tool"], []).append((sized[id(r)], k + 1))
        if any(r["tool"] == TOOL_SEARCH for r in rs):
            other = sum(pooled["b"] * r["chars"] for r in rs if r["tool"] != TOOL_SEARCH and id(r) not in sized)
            defs = max(0.0, growth_k - pooled["a"] - sum(sized.values()) - other)
            parts["definitions"].append((defs, k + 1))
    chars = _instructions_chars(row) + sum(len(x) + 1 for x in s["names"])
    prose = cal["text"] or pooled["b"]
    parts["instructions"].append((chars * prose if s["names"] else 0.0, 0))
    if hook_chars:
        parts["hook"].append((hook_chars * prose, 0))
    return parts


def row_fields(parts: dict, usage: dict) -> dict:
    n = parts["calls"]
    write = _write_ratio(usage)
    read = PRICE_RATIOS["cache_read"]

    def sums(items):
        return sum(t for t, _ in items)

    def cost(items):  # written once where it entered, read on every later call
        return sum(t * (write + max(0, n - 1 - j) * read) for t, j in items)

    by_tool = {t: round(sums(v), 1) for t, v in parts["results"].items()}
    every = [
        x for v in (parts["definitions"], parts["instructions"], parts["hook"], *parts["results"].values()) for x in v
    ]
    total = sum(t for t, _ in every)
    return {
        "recall_tokens": round(total, 1),
        "recall_tokens_definitions": round(sums(parts["definitions"]), 1),
        "recall_tokens_instructions": round(sums(parts["instructions"]), 1),
        "recall_tokens_hook": round(sums(parts["hook"]), 1),
        "recall_tokens_results": round(sum(by_tool.values()), 1),
        "recall_results_by_tool": {k.removeprefix(RECALL): v for k, v in sorted(by_tool.items())},
        "recall_cost_weighted": round(cost(every), 1),
    }


def regenerated_hook_chars(folder: str, meta: dict, history: int, _cache: dict = {}) -> int | None:  # noqa: B006
    """The session-start block a MemoryCode hook arm put in the system prompt, rebuilt from the
    run's own notes with today's code, for runs made before rows recorded `injected_chars`. It is
    what the block would be now, so it can differ a little from what was sent then."""
    try:
        from . import evals_memorycode as mc
        from .memory import context
        from .memory.notes import NoteStore

        key = meta.get("data")
        if key not in _cache:
            _cache[key] = {x.id: x for x in mc.load(os.path.expanduser(key))}
        x = _cache[key][history]
        return len(
            context.session_context(x.folder, NoteStore(os.path.join(folder, "memory", str(history))), agent="claude")
        )
    except (Exception, SystemExit):
        return None


def prepare(
    rows: list[dict], transcripts: dict[int, dict], eval_name: str, folder: str | None = None, meta: dict | None = None
) -> list[tuple]:
    """Set cost_weighted and solved on rows from their transcripts ({row index: transcript}), and
    return (index, steps, usage) for the rows that can be sized."""
    prepared = []
    for i, row in enumerate(rows):
        t = transcripts.get(i)
        usage = (t or {}).get("usage") or {}
        if usage:
            row["cost_weighted"] = round(weighted(usage), 1)
        solved = solved_of(row, eval_name)
        if solved is not None:
            row["solved"] = solved
        if folder and "-hook-" in eval_name and "injected_chars" not in row and row.get("history") is not None:
            n = regenerated_hook_chars(folder, meta or {}, row["history"])
            if n is not None:
                row["injected_chars"], row["injected_chars_source"] = n, "regenerated"
        if t and usage and row.get("status") == "ok":
            s = steps_of(t)
            if s["calls"]:
                s["growth"] = growth(s)
                s["injected"] = (
                    row.get("injected_chars") if row.get("injected_chars") is not None else s["hook_chars"] or None
                )
                prepared.append((i, s, usage))
    return prepared


def size(rows: list[dict], prepared: list[tuple], cal: dict) -> None:
    for i, s, usage in prepared:
        row = rows[i]
        hook = s["injected"]
        row.update(row_fields(parts_of(s, cal, row, hook), usage))


def annotate(rows: list[dict], transcripts: dict[int, dict], eval_name: str) -> dict:
    """Everything above for one run's rows, in place. Returns the calibration."""
    prepared = prepare(rows, transcripts, eval_name)
    cal = _calibrate([s for _, s, _ in prepared])
    size(rows, prepared, cal)
    return cal


def annotate_live(run) -> dict:
    """The fields above for a run that has just finished (an evals_harness.Run): its rows get them
    in place, and its cases.jsonl files are written again with them. Returns the calibration."""
    transcripts = {}
    for i, row in enumerate(run.rows):
        sub = "test" if row.get("split") == "test" else ""
        p = os.path.join(run.dir, sub, (row.get("transcript") or "").replace(".html", ".json"))
        if row.get("transcript") and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                transcripts[i] = json.load(f)
    cal = annotate(run.rows, transcripts, run.name)
    for sub, rows in (
        ("", [r for r in run.rows if r.get("split") != "test"]),
        ("test", [r for r in run.rows if r.get("split") == "test"]),
    ):
        if rows:
            with open(os.path.join(run.dir, sub, "cases.jsonl"), "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
    return cal


def summarize(rows: list[dict], cal: dict | None = None) -> dict:
    """The run's summary entries: recall's tokens, cost, cost per solved task."""
    from . import evals_harness as h

    scored = [r for r in rows if r.get("status") == "ok" and "cost_weighted" in r]
    if not scored:
        return {}
    out = {"price_ratios": PRICE_RATIOS, "cost_weighted": h.spread([r["cost_weighted"] for r in scored])}
    solved = [r for r in scored if "solved" in r]
    if solved:
        out["solved"] = {"tasks": len(solved), "solved": sum(r["solved"] for r in solved)}
        total, wins = sum(r["cost_weighted"] for r in solved), sum(r["solved"] for r in solved)
        out["cost_per_solved"] = round(total / wins, 1) if wins else None
    have = [r for r in scored if "recall_tokens" in r]
    if have and cal:
        out["recall_fit"] = {
            t.removeprefix(RECALL): {
                "tokens_per_char": round(f["b"], 3),
                "turn": round(f["a"]),
                "n": f["n"],
                "source": f["source"],
                **({"r2": round(f["r2"], 3)} if "r2" in f else {}),
            }
            for t, f in sorted(cal["fits"].items())
        }
        out["recall_fit"]["pooled"] = {
            k: (round(v, 3) if isinstance(v, float) else v) for k, v in cal["pooled"].items()
        }
        out["recall_fit"]["prose"] = {"tokens_per_char": round(cal["text"], 3), "source": cal["text_source"]}
    if have:
        out["recall_tokens"] = h.spread([r["recall_tokens"] for r in have])
        out["recall_tokens_by"] = {
            part: round(statistics.fmean(r[f"recall_tokens_{part}"] for r in have), 1)
            for part in ("definitions", "instructions", "hook", "results")
        }
        by_tool: dict[str, float] = {}
        for r in have:
            for k, v in (r.get("recall_results_by_tool") or {}).items():
                by_tool[k] = by_tool.get(k, 0) + v
        out["recall_results_by_tool"] = {k: round(v / len(have), 1) for k, v in sorted(by_tool.items())}
        total = sum(r["recall_tokens"] for r in have)
        cost = sum(r["cost_weighted"] for r in have)
        out["recall_cost_share"] = round(sum(r["recall_cost_weighted"] for r in have) / cost, 3) if cost else None
    return out


# --- the stored runs ----------------------------------------------------------------------


def _read(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_run(folder: str) -> dict:
    """A stored run's rows and transcripts, train and test side by side. Reads the folder only."""
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    rows, ts, split = [], {}, []
    for sub in ("", "test"):
        part = _read(os.path.join(folder, sub, "cases.jsonl"))
        for r in part:
            p = os.path.join(folder, sub, (r.get("transcript") or "").replace(".html", ".json"))
            if r.get("transcript") and os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    ts[len(rows)] = json.load(f)
            rows.append(r)
        split.append(len(part))
    prod = ""
    if os.path.exists(os.path.join(folder, "meta.txt")):
        with open(os.path.join(folder, "meta.txt"), encoding="utf-8") as f:
            found = re.search(r"\bprod=(\w+)", f.read())
        prod = found.group(1) if found else ""
    return {
        "prod": prod,
        "src": folder,
        "meta": meta,
        "eval": meta.get("eval") or "",
        "rows": rows,
        "ts": ts,
        "split": split,
    }


def rescore(folders: list[str], out: str) -> list[dict]:
    """Copy each run's cases.jsonl, meta.json and summary.json into out/<run> with the new fields.
    The runs of one eval, product code and arm are calibrated together (their steps pooled); a tool
    with too few steps of its own uses REFERENCE_FIT."""
    runs = [load_run(os.path.abspath(os.path.expanduser(f)).rstrip("/")) for f in folders]
    for r in runs:
        dest = os.path.join(out, os.path.basename(r["src"]))
        if os.path.abspath(dest).startswith(r["src"] + os.sep) or os.path.abspath(dest) == r["src"]:
            sys.exit("--out must be outside the run folder")
        r["dest"] = dest
        r["prepared"] = prepare(r["rows"], r["ts"], r["eval"], r["src"], r["meta"])
    cals = {}
    for r in runs:  # one fit per eval, product code and arm: never across versions or arms
        r["group"] = (r["eval"], r["prod"], r["meta"].get("arm"))
    for key in {r["group"] for r in runs}:
        cals[key] = _calibrate([s for r in runs if r["group"] == key for _, s, _ in r["prepared"]])
    report = []
    for r in runs:
        size(r["rows"], r["prepared"], cals[r["group"]])
        extra = summarize(r["rows"], cals[r["group"]])
        n_train = r["split"][0]
        os.makedirs(os.path.join(r["dest"], "test"), exist_ok=True)
        for rows, sub in ((r["rows"][:n_train], ""), (r["rows"][n_train:], "test")):
            if rows:
                with open(os.path.join(r["dest"], sub, "cases.jsonl"), "w", encoding="utf-8") as f:
                    f.writelines(json.dumps(x) + "\n" for x in rows)
        summary = {}
        for name in ("meta.json", "meta.txt"):
            if os.path.exists(os.path.join(r["src"], name)):
                shutil.copy(os.path.join(r["src"], name), r["dest"])
        if os.path.exists(os.path.join(r["src"], "summary.json")):
            with open(os.path.join(r["src"], "summary.json"), encoding="utf-8") as f:
                summary = json.load(f)
        summary.update(extra)
        with open(os.path.join(r["dest"], "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        report.append({"run": os.path.basename(r["src"]), "dest": r["dest"], "eval": r["eval"], **extra})
    return report


def check_cost(folder: str) -> dict:
    """Weigh a stored run's usage by PRICE_RATIOS against the cost Claude Code reported for it:
    the dollars a weighted token costs over the whole run, and the worst task's relative error
    against that one rate (0 when the ratios are right)."""
    pairs = []
    for p in glob.glob(os.path.join(folder, "**", "transcripts", "*.json"), recursive=True):
        with open(p, encoding="utf-8") as f:
            t = json.load(f)
        res = next((e for e in t.get("events") or [] if e.get("type") == "result"), None)
        if res and res.get("total_cost_usd"):
            pairs.append((weighted(res["usage"]), res["total_cost_usd"]))
    got, want = sum(g for g, _ in pairs), sum(w for _, w in pairs)
    rate = want / got if got else None
    worst = max((abs(w - rate * g) / w for g, w in pairs), default=None) if rate else None
    return {"tasks": len(pairs), "dollars_per_weighted_token": rate, "worst_task_error": worst}


def _show(report: list[dict]) -> None:
    for r in report:
        print(f"\n{r['run']}  ({r['eval']})")
        if "cost_per_solved" in r:
            s = r["solved"]
            print(
                f"  cost per task {r['cost_weighted']['mean']:,.0f}   per solved {r['cost_per_solved']:,.0f}   "
                f"({s['solved']}/{s['tasks']} solved)"
            )
        if "recall_tokens" in r:
            b = r["recall_tokens_by"]
            print(
                f"  recall tokens/task {r['recall_tokens']['mean']:,.0f}: definitions {b['definitions']:,.0f}, "
                f"instructions+names {b['instructions']:,.0f}, hook {b['hook']:,.0f}, results {b['results']:,.0f}"
            )
            print("    results by tool: " + ", ".join(f"{k} {v:,.0f}" for k, v in r["recall_results_by_tool"].items()))
            print(f"    recall's share of weighted cost {r['recall_cost_share']:.1%}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="recall eval tokens", description=__doc__.split("\n\n")[0])
    p.add_argument("runs", nargs="+", help="run folders of agent evals")
    p.add_argument("--out", help="where the re-scored copies go (default: a new folder under the temp dir)")
    p.add_argument(
        "--check-cost", action="store_true", help="weigh the runs' usage against the cost Claude Code reported"
    )
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.check_cost:
        for r in a.runs:
            print(os.path.basename(r.rstrip("/")), check_cost(os.path.expanduser(r)))
        return
    import tempfile

    out = a.out or tempfile.mkdtemp(prefix="recall-eval-tokens-")
    report = rescore(a.runs, os.path.expanduser(out))
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        _show(report)
        print(f"\nre-scored copies: {out}")


if __name__ == "__main__":
    main()

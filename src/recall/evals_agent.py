"""M2 and M3: can a coding agent answer a question about past work with recall's MCP tools,
and how many tokens does it read to do it?

Each qa_v2 case runs Claude Code headless as the agent under test:
  {"id": "q-001", "question": "...", "answer": "...", "claims": ["...", ...], "expect": [...],
   "answerable": true, "asked_on": "YYYY-MM-DD", "cwd": "/path or null", "tags": [...], "split": "test"}
The agent gets recall's MCP server (this checkout's code, or a copy of src on PYTHONPATH,
reading RECALL_HOME and RECALL_MEMORY) and nothing else: of the built-in tools only tool search,
so no Bash, Read, Grep or web, and none of the user's settings, hooks, plugins, skills or CLAUDE.md. It
runs in an empty temporary folder and saves no transcript, so its runs never reach
~/.claude/projects, where recall's live indexer would read them. The question is the user's
message, as in production; the case's date and project folder go in the system prompt.

A Claude Sonnet judge grades each reply against the case's checkable claims. Answerable
cases are correct when every claim is supported and nothing contradicts the gold answer;
unanswerable ones when the reply says the history does not hold the answer and invents none.
"""

import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import evals_harness as h
from . import paths
from .llm import LLMError
from .llm.agent_cli import plan_env

DEFAULT_MODEL = "claude-haiku-5-5"
TIMEOUT = 300
PROJECTS = os.path.expanduser("~/.claude/projects")
TMP_PREFIX = "recall-eval-agent-"
REPLY_CHARS = 20_000  # of a reply the judge reads
# Claude Code replaces an MCP result over its token limit with this message.
TOOL_LIMIT = "exceeds maximum allowed tokens"
# What the agent is given, recorded in each run's meta as agent_harness so that `eval compare`
# can tell runs made with different tool setups. Bump when the tools, flags or settings change.
HARNESS = "toolsearch-v1"  # tool search on: MCP tools are deferred, as in a default session
TOOL_SEARCH = "ToolSearch"  # the built-in that loads deferred tools; not a recall tool call


def run(
    cases: list[dict],
    run: h.Run,
    model: str = DEFAULT_MODEL,
    judge: str = h.JUDGE,
    repeats: int = 1,
    workers: int = 4,
    timeout: int = TIMEOUT,
    consistency: int = 8,
    skill: bool = False,
    against: str | None = None,
) -> dict:
    model = model.removeprefix("claude:")
    if h.same_model(model, judge):
        sys.exit(f"the judge ({judge}) must differ from the model under test ({model}): pass --judge")
    if not shutil.which("claude"):
        sys.exit("Claude Code is not installed: `claude` is not on PATH")
    mcp = write_mcp_config(os.path.join(run.dir, "mcp.json"))
    run.meta.update(server=_server_code(), agent_harness=HARNESS, model=model, judge=judge, judge_version=JUDGE_VERSION)
    grader = h.judge_client(judge)
    jobs = [(c, r) for r in range(1, repeats + 1) for c in cases]
    done = [0]
    lock = threading.Lock()

    def one(job):
        case, repeat = job
        attempt = ask(case, model, mcp, timeout, skill)
        row, transcript = attempt["row"], attempt["transcript"]
        verdict = None
        if repeats > 1:
            row["repeat"] = repeat
        if row["status"] == h.OK:
            try:
                verdict = grade(grader, case, attempt["answer"])
                row["correct"] = verdict["correct"]
                transcript["judge"] = verdict
            except LLMError as e:
                row["status"], row["detail"] = "judge_error", str(e)[:300]
        written = run.record(case, row, transcript)
        with lock:
            done[0] += 1
            label = f"{case['id']} r{repeat}" if case["split"] != "test" else "test case"
            outcome = {True: "correct", False: "wrong"}.get(row.get("correct"), row["status"])
            print(f"[{done[0]}/{len(jobs)}] {label}: {outcome} {row.get('seconds', 0):.0f}s", file=sys.stderr)
        return written, attempt["answer"], verdict

    leaks_before = _leaks()
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, jobs))
    leaks = sorted(set(_leaks()) - set(leaks_before))

    scored = [r for r in run.rows if r["status"] == h.OK]
    per_case = _per_case(scored)
    accuracy = h.ci(list(per_case.values()))
    answerable = {c["id"]: c.get("answerable", True) for c in cases}
    tags = {c["id"]: c["tags"] for c in cases}
    metrics = {
        "accuracy": accuracy,
        "accuracy_answerable": h.ci([v for k, v in per_case.items() if answerable[k]]),
        "accuracy_unanswerable": h.ci([v for k, v in per_case.items() if not answerable[k]]),
    }
    tools: dict[str, int] = {}
    for r in scored:
        for name, n in r["tools"].items():
            tools[name] = tools.get(name, 0) + n
    summary = {
        "model": model,
        "judge": judge,
        "judge_version": JUDGE_VERSION,
        "repeats": repeats,
        **metrics,
        "by_tag": h.by_tag([{"tags": tags[k], "v": v} for k, v in per_case.items()], lambda r: r["v"]),
        "tokens_in": h.spread([r["tokens_in"] for r in scored]),
        "tokens_out": h.spread([r["tokens_out"] for r in scored]),
        "tool_calls": {**h.spread([r["tool_calls"] for r in scored]), "by_tool": dict(sorted(tools.items()))},
        "tool_search_calls": h.spread([r["tool_search_calls"] for r in scored]),
        "tool_result_chars": h.spread([r["tool_chars"] for r in scored]),
        "seconds": h.spread([r["seconds"] for r in scored]),
        "cost_usd_api_equivalent": {
            "total": round(sum(r["cost_usd"] for r in scored), 2),
            "per_run": round(statistics.fmean([r["cost_usd"] for r in scored]), 3) if scored else None,
        },
        "plumbing": h.plumbing(run.rows),
        "tool_truncations": sum(r.get("tool_truncations", 0) for r in run.rows),
        "transcript_leaks": leaks,
    }
    if repeats > 1:
        summary["variance"] = _variance(scored, repeats)
    summary["grader"] = _consistency(judge, cases, results, consistency)
    summary["warnings"] = h.headroom(metrics)
    if leaks:
        summary["warnings"].append(f"{len(leaks)} folders appeared in {PROJECTS}: a run saved a transcript")
    if against:
        value = lambda r: float(r["correct"]) if r.get("correct") is not None else None  # noqa: E731
        summary["against"] = {"run": against, "accuracy": h.paired(per_case, h.against(against, value))}
        summary["warnings"] += h.mismatch(against, run.meta)
    headline = (
        f"acc={h.fmt(accuracy)} tokens_in_median={summary['tokens_in'].get('median')} "
        f"tools_median={summary['tool_calls'].get('median')} model={model}"
    )
    if against:
        headline += f" Δacc={h.fmt(summary['against']['accuracy'], signed=True)}"
    columns = [
        ("correct", "correct"),
        ("tokens in", "tokens_in"),
        ("tools", "tool_calls"),
        ("seconds", "seconds"),
        ("answer", "answer"),
    ]
    return run.finish(summary, headline, columns, note=run.meta.get("note", ""))


# --- the agent under test -------------------------------------------------------------


def ask(case: dict, model: str, mcp_config: str, timeout: int, skill: bool = False) -> dict:
    """Run the agent under test on one qa_v2 case."""
    return run_agent(case["question"], model, session_prompt(case, skill), mcp_config, timeout)


def run_agent(
    prompt: str,
    model: str = DEFAULT_MODEL,
    system: str = "",
    mcp_config: str | None = None,
    timeout: int = TIMEOUT,
    settings: str | None = None,
) -> dict:
    """Run Claude Code headless on one prompt with recall's MCP tools and nothing else.

    No built-in tools but tool search (no Bash, Read, Grep, web or subagents), so recall's
    tools are deferred as in a default Claude Code session; none of the user's settings,
    hooks, plugins, skills, CLAUDE.md or auto-memory; an empty temporary working folder; the
    signed-in plan rather than an API key; and no transcript saved, so the run never reaches
    ~/.claude/projects. system is appended to Claude Code's own system prompt. mcp_config:
    a file from write_mcp_config() (by default one is written for this call). settings: JSON
    for --settings, such as the hooks an arm tests (they run despite --setting-sources).

    Returns {"answer": the final reply, "row": status ("ok", "timeout", "api_error",
    "truncated" or "mcp_error"), detail and numbers (tokens, tool calls, seconds, cost),
    "transcript": readable steps, every tool call with its input and output size, usage, and
    the raw stream-json events}.
    """
    own_config = mcp_config is None
    if own_config:
        mcp_config = write_mcp_config(os.path.join(tempfile.mkdtemp(prefix=TMP_PREFIX + "mcp-"), "mcp.json"))
    cmd = [
        "claude",
        "-p",
        "--model",
        model.removeprefix("claude:"),
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config,
        "--tools",
        # Only tool search, no Bash, Read, Grep, web or subagents. An empty list would drop
        # ToolSearch too, and Claude Code would then load every MCP tool upfront, which a
        # default session does not: there only the tool names, server instructions and
        # tools marked anthropic/alwaysLoad load at the start.
        TOOL_SEARCH,
        "--allowedTools",
        "mcp__recall",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "project",  # the empty folder has no project settings, so none load
        "--disable-slash-commands",
        "--no-session-persistence",
        *(["--append-system-prompt", system] if system else []),
        *(["--settings", settings] if settings else []),
    ]
    env = {**plan_env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    cwd = tempfile.mkdtemp(prefix=TMP_PREFIX)
    started = time.time()
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,  # so a timeout also stops the MCP server it started
        )
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        if own_config:
            shutil.rmtree(os.path.dirname(mcp_config), ignore_errors=True)
    return _parse(out, err, proc.returncode, timed_out, time.time() - started)


def session_prompt(case: dict, skill: bool = False) -> str:
    """What production would know without being told: the date and the project folder."""
    lines = []
    if case.get("asked_on"):
        lines.append(
            f"Today's date is {case['asked_on']}; it replaces the date in the environment section, "
            "which is this machine's clock."
        )
    if case.get("cwd"):
        lines.append(
            f"The user is working in the project folder {case['cwd']} "
            "(your own working directory is an empty scratch folder)."
        )
    else:
        lines.append("The user is not in a project folder (your working directory is an empty scratch folder).")
    if skill:
        lines.append("\nThe recall skill, which tells agents how to use these tools:\n\n" + _skill_text())
    return "\n".join(lines)


def _skill_text() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "integrations", "skill", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def write_mcp_config(path: str, env: dict | None = None) -> str:
    """Write an MCP config for recall's server, started with the interpreter running this
    eval, so it runs this checkout (or the copy of src that PYTHONPATH points at) against
    RECALL_HOME and RECALL_MEMORY. env: more variables for the server. Returns path."""
    env = {"RECALL_HOME": paths.HOME, "RECALL_MEMORY": paths.MEMORY, **(env or {})}
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    config = {
        "mcpServers": {
            "recall": {"type": "stdio", "command": sys.executable, "args": ["-m", "recall", "serve"], "env": env}
        }
    }
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    return path


def _server_code() -> str:
    """Where the MCP server will import recall from, for the run's meta.json."""
    env = {**os.environ, "RECALL_HOME": paths.HOME, "RECALL_MEMORY": paths.MEMORY}
    probe = subprocess.run(
        [sys.executable, "-c", "import recall; print(recall.__file__)"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tempfile.gettempdir(),
    )
    return probe.stdout.strip() or probe.stderr.strip()[-300:]


def _parse(out: str, err: str, code: int | None, timed_out: bool, seconds: float) -> dict:
    """Turn Claude Code's stream-json output into a result row and a readable transcript."""
    events, steps, calls, messages = [], [], {}, set()
    init = result = None
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(e)
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            init = e
        elif kind == "assistant":
            message = e.get("message") or {}
            messages.add(message.get("id"))
            for b in message.get("content") or []:
                if b.get("type") == "text":
                    steps.append({"kind": "text", "text": b["text"]})
                elif b.get("type") == "thinking":
                    steps.append({"kind": "thinking", "text": b.get("thinking") or "(not shown by Claude Code)"})
                elif b.get("type") == "tool_use":
                    name = b["name"].removeprefix("mcp__recall__")
                    calls[b["id"]] = {"tool": name, "input": b.get("input"), "chars": None, "error": False}
                    steps.append({"kind": "tool_call", "tool": name, "input": b.get("input")})
        elif kind == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = _result_text(b.get("content"))
                    call = calls.get(b.get("tool_use_id"), {"tool": "?"})
                    call.update(chars=len(text), error=bool(b.get("is_error")), truncated=TOOL_LIMIT in text)
                    steps.append({"kind": "tool_result", "tool": call["tool"], "chars": len(text), "text": text})
        elif kind == "result":
            result = e
    answer = (result or {}).get("result") or ""
    usage = (result or {}).get("usage") or {}
    status, detail = h.OK, None
    connected = any(
        s.get("name") == "recall" and s.get("status") == "connected" for s in (init or {}).get("mcp_servers", [])
    )
    if timed_out:
        status, detail = "timeout", f"no answer after {seconds:.0f}s"
    elif result is None:
        status, detail = "api_error", f"claude exited {code}: {(err or out)[-300:]}"
    elif result.get("subtype") == "error_max_turns" or result.get("stop_reason") == "max_tokens":
        status, detail = "truncated", result.get("subtype") or result.get("stop_reason")
    elif result.get("is_error") or result.get("subtype") != "success":
        status, detail = "api_error", f"{result.get('subtype')}: {str(result.get('result'))[:300]}"
    elif init and not connected:
        status, detail = "mcp_error", json.dumps(init.get("mcp_servers"))
    elif any(c["error"] and "MCP error" in str(c) for c in calls.values()):
        status, detail = "mcp_error", "the recall server failed during the run"
    elif not answer.strip():
        status, detail = "api_error", "empty reply"
    # tool_calls, tools and tool_chars count recall's tools only, so they read the same with tool
    # search on or off; the searches that load those tools are counted apart.
    searches = [c for c in calls.values() if c["tool"] == TOOL_SEARCH]
    used = [c for c in calls.values() if c["tool"] != TOOL_SEARCH]
    tools: dict[str, int] = {}
    for c in used:
        tools[c["tool"]] = tools.get(c["tool"], 0) + 1
    row = {
        "status": status,
        "detail": detail,
        "correct": None,
        "answer": answer[:300],
        "tokens_in": sum(
            usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ),
        "tokens_uncached": usage.get("input_tokens") or 0,
        "cache_write": usage.get("cache_creation_input_tokens") or 0,
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "tokens_out": usage.get("output_tokens") or 0,
        "tool_calls": len(used),
        "tool_search_calls": len(searches),
        "tools": tools,
        "tool_chars": sum(c["chars"] or 0 for c in used),
        "tool_search_chars": sum(c["chars"] or 0 for c in searches),
        "tool_errors": sum(c["error"] for c in calls.values()),
        "tool_truncations": sum(bool(c.get("truncated")) for c in calls.values()),
        "api_calls": len(messages - {None}),
        "turns": (result or {}).get("num_turns"),
        "seconds": round(seconds, 1),
        "cost_usd": round((result or {}).get("total_cost_usd") or 0, 4),
    }
    transcript = {
        "answer": answer,
        "steps": steps,
        "tool_calls": list(calls.values()),
        "usage": usage,
        "stderr": err[-2000:] if err else "",
        "events": events,
    }
    return {"answer": answer, "row": row, "transcript": transcript}


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return json.dumps(content)


def _leaks() -> list[str]:
    """Folders in ~/.claude/projects made for this eval's temporary working folders."""
    try:
        return [d for d in os.listdir(PROJECTS) if TMP_PREFIX in d]
    except OSError:
        return []


# --- grading ------------------------------------------------------------------------

# Bump when the judge's prompt or rubric changes: scores graded by different versions do not compare.
JUDGE_VERSION = "qa-judge-v2"  # v2: a claim with several parts is supported only when every part is stated
JUDGE_SYSTEM = (
    "You grade replies from a coding agent that answered a developer's question by searching their "
    "past coding-agent chats. Grade only what the reply asserts against the gold answer and the "
    "listed claims. Wording, length and style do not matter; hedged or alternative answers "
    '("maybe X, or Y") do not support a claim. A claim with several parts ("A, B and C") is '
    "supported only when the reply states every part; if a part is left out, the claim is missing."
)
_CLAIMS = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["claim", "reasoning", "verdict"],
        "properties": {
            "claim": {"type": "integer"},
            "reasoning": {"type": "string"},
            "verdict": {"enum": ["supported", "missing", "contradicted"]},
        },
    },
}
ANSWERABLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims", "contradictions", "reasoning"],
    "properties": {
        "claims": _CLAIMS,
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
}
UNANSWERABLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims", "says_not_found", "invented", "reasoning"],
    "properties": {
        "claims": _CLAIMS,
        "says_not_found": {"type": "boolean"},
        "invented": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
}


def grade(client, case: dict, answer: str) -> dict:
    """The judge's verdict on one reply. correct is derived from the checkable parts."""
    claims = "\n".join(f"{i}. {c}" for i, c in enumerate(case.get("claims") or [], 1)) or "(none listed)"
    reply = answer[:REPLY_CHARS]
    head = f"Question (asked on {case.get('asked_on') or 'unknown'}): {case['question']}\n\n"
    if case.get("answerable", True):
        prompt = (
            f"{head}Gold answer: {case.get('answer', '')}\n\nClaims a correct reply makes:\n{claims}\n\n"
            f"The agent's reply:\n<reply>\n{reply}\n</reply>\n\n"
            "For each claim, say whether the reply supports it (states it, in any words, or states something "
            "that entails it), leaves it out (missing), or contradicts it. Then list every statement in the "
            "reply that contradicts the gold answer; details the gold answer does not cover are fine unless "
            "they conflict with it."
        )
        verdict = client.json("qa-judge", JUDGE_SYSTEM, prompt, ANSWERABLE_SCHEMA)
        verdict["correct"] = not verdict["contradictions"] and _all_supported(verdict, case)
    else:
        prompt = (
            f"{head}The developer's chat history does not hold the answer to this question. "
            f"Why: {case.get('answer', '')}\n\nClaims a correct reply makes:\n{claims}\n\n"
            f"The agent's reply:\n<reply>\n{reply}\n</reply>\n\n"
            "says_not_found: does the reply tell the developer it could not find the answer, or that their "
            "history does not cover it? invented: does it present an answer as fact that the history could "
            "not have given (suggesting where to look, or naming related chats without claiming they answer "
            "the question, is not inventing)? Also judge each claim as supported, missing or contradicted."
        )
        verdict = client.json("qa-judge", JUDGE_SYSTEM, prompt, UNANSWERABLE_SCHEMA)
        contradicted = any(c["verdict"] == "contradicted" for c in verdict["claims"])
        verdict["correct"] = verdict["says_not_found"] and not verdict["invented"] and not contradicted
    return verdict


def _all_supported(verdict: dict, case: dict) -> bool:
    supported = {c["claim"] for c in verdict["claims"] if c["verdict"] == "supported"}
    return all(i in supported for i in range(1, len(case.get("claims") or []) + 1))


def _consistency(judge: str, cases: list[dict], results, sample: int) -> dict:
    """Grade a sample of replies again, skipping the cache, and count changed verdicts."""
    graded = [(row, answer, first) for row, answer, first in results if row["status"] == h.OK and first]
    if not sample or not graded:
        return {"sampled": 0}
    picked = random.Random(h.SEED).sample(graded, min(sample, len(graded)))
    fresh = h.judge_client(judge, cache=False)
    by_id = {c["id"]: c for c in cases}
    flips, claim_flips, claims, errors, flipped = 0, 0, 0, 0, []
    for row, answer, first in picked:
        case = by_id[row["id"]]
        try:
            second = grade(fresh, case, answer)
        except LLMError:
            errors += 1
            continue
        if second["correct"] != first["correct"]:
            flips += 1
            if case["split"] != "test":  # both verdicts, to see which part of the rubric wobbles
                flipped.append(
                    {
                        "id": row["id"] + (f" r{row['repeat']}" if row.get("repeat") else ""),
                        "first": [(c["claim"], c["verdict"], c["reasoning"]) for c in first["claims"]],
                        "second": [(c["claim"], c["verdict"], c["reasoning"]) for c in second["claims"]],
                    }
                )
        before = {c["claim"]: c["verdict"] for c in first["claims"]}
        for c in second["claims"]:
            claims += 1
            claim_flips += before.get(c["claim"]) != c["verdict"]
    out = {"sampled": len(picked) - errors, "verdict_flips": flips, "claim_flips": claim_flips, "claims": claims}
    if flipped:
        out["flips"] = flipped
    if errors:
        out["errors"] = errors
    return out


def _per_case(rows: list[dict]) -> dict[str, float]:
    scores: dict[str, list[float]] = {}
    for r in rows:
        if r.get("correct") is not None:
            scores.setdefault(r["id"], []).append(float(r["correct"]))
    return {k: statistics.fmean(v) for k, v in scores.items()}


def _variance(rows: list[dict], repeats: int) -> dict:
    """Run-to-run spread: accuracy of each repeat over the same cases, and the mean per-case SD."""
    by_repeat: dict[int, list[float]] = {}
    by_case: dict[str, list[float]] = {}
    for r in rows:
        if r.get("correct") is not None:
            by_repeat.setdefault(r["repeat"], []).append(float(r["correct"]))
            by_case.setdefault(r["id"], []).append(float(r["correct"]))
    accuracies = [statistics.fmean(v) for _, v in sorted(by_repeat.items())]
    sds = [statistics.pstdev(v) for v in by_case.values() if len(v) > 1]
    return {
        "accuracy_by_repeat": [round(a, 3) for a in accuracies],
        "run_to_run_sd": round(statistics.stdev(accuracies), 3) if len(accuracies) > 1 else None,
        "per_case_sd_mean": round(statistics.fmean(sds), 3) if sds else None,
        "cases_always_right": sum(min(v) == 1 for v in by_case.values()),
        "cases_always_wrong": sum(max(v) == 0 for v in by_case.values()),
    }

"""Tokens of recall output an agent takes in per question, in Claude's own tokens.

`recall eval agent` and `recall eval swe-contextbench --agent` keep each question's transcript:
the text of every tool result and Claude Code's per-call usage. Tool-result text is counted in
word pieces (runs of letters and digits, and single symbols), then converted to Claude tokens
with a factor fitted on the same transcripts: across a question's API calls the context grows
by its tool results plus the agent's own turns, so growth = a x pieces + c x tool calls, fitted
without an intercept. recall's tool definitions are added once; they are measured directly as
the first call's input with recall's MCP server connected minus without (2,591 tokens in 0.2.1,
2,480 in 0.3.0).

usage: python docs/benchmarks/agent_tokens.py DEFS_BEFORE DEFS_AFTER before:RUN_DIR ... after:RUN_DIR ...
"""

import glob
import json
import re
import sys

PIECE = re.compile(r"\w+|[^\w\s]")


def transcripts(run: str):
    for path in glob.glob(f"{run}/transcripts/*.json") + glob.glob(f"{run}/test/transcripts/*.json"):
        with open(path) as f:
            yield json.load(f)


def measure(t: dict) -> tuple[int | None, int, int]:
    """Context growth over the run in Claude tokens (None with one call), tool-result pieces,
    and tool calls."""
    ctx: dict[str, int] = {}
    for e in t.get("events", []):
        m = e.get("message") or {}
        u = m.get("usage")
        if e.get("type") == "assistant" and u and m.get("id"):
            size = (
                u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            )
            ctx[m["id"]] = max(ctx.get(m["id"], 0), size)  # dicts keep the order calls were made
    steps = t.get("steps", [])
    pieces = sum(len(PIECE.findall(s.get("text") or "")) for s in steps if s.get("kind") == "tool_result")
    calls = sum(1 for s in steps if s.get("kind") == "tool_call")
    sizes = list(ctx.values())
    return (sizes[-1] - sizes[0] if len(sizes) > 1 else None), pieces, calls


def main(argv: list[str]) -> None:
    defs = {"before": int(argv[0]), "after": int(argv[1])}
    pieces: dict[str, list[int]] = {"before": [], "after": []}
    fit = []
    for arg in argv[2:]:
        side, run = arg.split(":", 1)
        for t in transcripts(run):
            growth, p, calls = measure(t)
            pieces[side].append(p)
            if growth is not None and p:
                fit.append((growth, p, calls))
    sxx = sum(p * p for _, p, _ in fit)
    syy = sum(k * k for _, _, k in fit)
    sxy = sum(p * k for _, p, k in fit)
    a = (sum(g * p for g, p, _ in fit) * syy - sum(g * k for g, _, k in fit) * sxy) / (sxx * syy - sxy * sxy)
    print(f"{len(fit)} transcripts: {a:.3f} Claude tokens per word piece of tool output")
    total = {}
    for side in ("before", "after"):
        mean = sum(pieces[side]) / len(pieces[side])
        total[side] = a * mean + defs[side]
        results = f"{a * mean:,.0f} tokens of results + {defs[side]:,} of tool definitions"
        print(f"{side}: {len(pieces[side])} questions, {results} = {total[side]:,.0f}")
    print(f"change: {100 * (total['after'] / total['before'] - 1):+.1f}%")


if __name__ == "__main__":
    main(sys.argv[1:])

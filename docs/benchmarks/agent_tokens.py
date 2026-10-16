"""Tokens of recall output an agent takes in per question, in Claude's own tokens.

`recall eval agent` and `recall eval swe-contextbench --agent` keep each question's transcript:
the text of every tool result and Claude Code's per-call usage. Tool-result text is counted in
word pieces (runs of letters and digits, and single symbols), then converted to Claude tokens
with a factor fitted on the same transcripts: across a question's API calls the context grows
by its tool results plus the agent's own turns, so growth = a x pieces + c x tool calls, fitted
without an intercept, over every run given, so the releases compared share one factor. recall's
tool definitions are added once; they are measured directly as the first call's input with
recall's MCP server connected minus without, tools loaded up front (2,591 tokens in 0.2.1, 2,480
in 0.3.0, 3,260 in 0.4.1).

usage: python docs/benchmarks/agent_tokens.py RELEASE=DEFS RUN_DIR ... [RELEASE=DEFS RUN_DIR ...] ...
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
    defs: dict[str, int] = {}
    pieces: dict[str, list[int]] = {}
    fit = []
    release = ""
    for arg in argv:
        if "=" in arg:
            release, n = arg.split("=", 1)
            defs[release], pieces[release] = int(n), []
            continue
        for t in transcripts(arg):
            growth, p, calls = measure(t)
            pieces[release].append(p)
            if growth is not None and p:
                fit.append((growth, p, calls))
    sxx = sum(p * p for _, p, _ in fit)
    syy = sum(k * k for _, _, k in fit)
    sxy = sum(p * k for _, p, k in fit)
    a = (sum(g * p for g, p, _ in fit) * syy - sum(g * k for g, _, k in fit) * sxy) / (sxx * syy - sxy * sxy)
    print(f"{len(fit)} transcripts: {a:.3f} Claude tokens per word piece of tool output")
    last = None
    for release, ps in pieces.items():
        mean = sum(ps) / len(ps)
        total = a * mean + defs[release]
        results = f"{a * mean:,.0f} tokens of results + {defs[release]:,} of tool definitions"
        change = f" ({100 * (total / last - 1):+.1f}%)" if last else ""
        print(f"{release}: {len(ps)} questions, {results} = {total:,.0f}{change}")
        last = total


if __name__ == "__main__":
    main(sys.argv[1:])

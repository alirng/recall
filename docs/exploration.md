# Exploration reports

Coding agents hand questions about a codebase to exploration subagents (Claude Code's
Explore, Cursor's explore, Codex's explorer), and those start from nothing every time: the
same repo gets explored again and again, often for much the same question. recall keeps each
exploration's report with the commit it was written at, so there is something to start from.

Recording is on. Handing an earlier report to a new exploration is **off by default**: a replay
of 10 past explorations with Claude Code's Explore found no gain in judged quality
(7.10 with it against 7.20 without) and tokens about 6% lower, within the noise. To try it, set

```toml
[explore]
serve = true    # default false; takes effect at once, no reinstall
```

and when a new exploration subagent starts in a repo with a question an earlier report
answers, recall appends that report to its prompt as notes another agent wrote (files,
symbols, conclusions, never instructions), with its commit, its age and the files changed
since, and asks it to verify what changed and extend the rest rather than redo it. When no
earlier report matches closely enough, it adds nothing.

| Agent | Records reports | Hands them to new explorations (`serve = true`) |
| --- | --- | --- |
| Claude Code | `SubagentStop` | `PreToolUse` on the Agent tool rewrites the prompt (tested live) |
| Codex | `SubagentStop` (explorer role; from Codex's docs and source, not yet tested live) | not automatically: its `SubagentStart` comes before the subagent's task exists, so on request via `project_memory` |
| Cursor | `subagentStop` | `preToolUse` on the Task tool rewrites the prompt (from Cursor's app code; not yet tested live) |

`recall explore` lists a repo's stored explorations, `recall explore lookup <question>` shows
what a new one would get with serving on, and `recall explore backfill` stores the ones
already in your chat history, each at the commit its repo's main branch was on at the time.
Any agent can also ask for it, whatever `serve` says, through MCP: `project_memory` with a
`task`. `recall eval explore` replays your past explorations with and without it.

# recall

**One memory for all your coding agents.**

[![CI](https://github.com/alirng/recall/actions/workflows/ci.yml/badge.svg)](https://github.com/alirng/recall/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)

Your coding agents forget everything between sessions, and none of them knows what you told
the others. recall reads the chat history that Claude Code, Codex and Cursor already keep on
your machine, and gives every agent two things:

- **Search across every chat.** "Find the Cursor chat where we planned the auth refactor"
  works in any agent, whichever tool the chat happened in.
- **Memory.** A small set of plain markdown notes, rewritten nightly: how you like to work,
  what each project needs, what was decided, what went wrong, and what is still open. Each
  note links to the chats it came from.

It runs on your machine. You choose the model that writes the memories: a local one that
keeps everything private, your ChatGPT or Claude subscription, or your own API key or server.

```text
you   › what did we decide about retries in the payments service?
agent › Searching your past chats… In "Payment webhook retries" (Codex, 2 Oct) you settled
        on exponential backoff capped at 5 attempts, and a dead-letter queue for the rest.
        Memory also notes: "Never retry card captures; they are not idempotent."
```

## Contents

[Install](#install) · [Quick start](#quick-start) · [Choosing a model](#choosing-a-model) ·
[How agents use it](#how-agents-use-it) · [How it works](#how-it-works) ·
[Memory notes](#memory-notes) · [Commands](#commands) · [FAQ](#faq) ·
[Contributing](#contributing)

## Install

Requirements: macOS or Linux, Python 3.11+, and at least one of Claude Code, Codex or Cursor.

```bash
uv tool install git+https://github.com/alirng/recall
# or: pipx install git+https://github.com/alirng/recall
```

A PyPI release (`recall-agents`) is coming; the command is `recall` either way.

## Quick start

```bash
recall setup
```

Setup asks a few questions and does the rest:

```text
1. Agents      finds Claude Code, Codex and Cursor; import from all of them or some
2. Model       picks who writes your memories, then tests it with a live call
3. Index       reads your chat history into a local search index
4. Connect     adds the MCP tools, the /recall skill and a Claude Code session hook
5. Schedule    updates memory nightly (launchd on macOS, systemd on Linux)
6. First run   starts in the background, newest chats first
```

Then ask any agent about an earlier conversation, or just start working: Claude Code opens
each session with your preferences and the current project's notes already loaded.

## Choosing a model

recall distils chats with a language model. Pick the one that fits your privacy and budget:

| Option | What it uses | Where chat excerpts go |
| --- | --- | --- |
| **Local model** | Ollama, LM Studio or llama.cpp on this machine | nowhere: they stay on this machine |
| **ChatGPT** | your ChatGPT plan, through the Codex CLI | OpenAI, under your plan's data terms |
| **Claude** | your Claude plan, through Claude Code | Anthropic, under your plan's data terms |
| **API key** | OpenAI, Anthropic or OpenRouter | that provider, billed to your key |
| **Your server** | any OpenAI-compatible endpoint, such as vLLM or Ollama on another box | your server |

Change it any time with `recall model`. The choice is saved in `~/.agents/recall/config.toml`:

```toml
[model]
provider = "local"                     # local | chatgpt | claude | openai | anthropic | openrouter | custom
name = "qwen3:4b"
server = "ollama"                      # local only
base_url = "http://localhost:11434"    # local and custom
api_key = "keychain"                   # env:NAME, keychain or file; never the key itself
```

Whichever you pick, every call is validated against a strict JSON schema, retried with
backoff, cached, logged, and capped by time and call budgets. recall's own calls never show
up in your chat history, and the subscription CLIs run with your MCP servers, hooks and
plugins switched off.

For a local model, smaller is kinder to your laptop: a 4B model suits 16 GB of memory.

## How agents use it

| Agent | History import | MCP tools | `/recall` skill | Memory at session start |
| --- | --- | --- | --- | --- |
| Claude Code | yes | yes | yes | automatic, via a session-start hook |
| Codex | yes | yes | yes | on request, via the `project_memory` tool |
| Cursor | yes, including older chats | yes | yes | on request, via the `project_memory` tool |

- **Automatic:** the session-start hook loads your working preferences and the current
  project's notes, capped at 6,000 characters.
- **On demand:** six read-only MCP tools: `search_chats`, `read_chat`, `recent_chats`,
  `memory_search`, `read_note` and `project_memory`.
- **Explicit:** type `/recall what did we decide about auth` in any agent with skills.

Agents treat notes and transcripts as background, not instructions: the current request and
the code win when they disagree.

## How it works

```text
 Claude Code ─┐                                    ┌─ dream (nightly) ──────────────────────────┐
 Codex ───────┼─ parse & clean ─ redact ─ SQLite ──┤  digest → extract → consolidate → notes     │
 Cursor ──────┘                          (FTS5)    │          (batched)   (per project)  (git)   │
                                                   └────────────────────────────────────────────┘
               ↓ search_chats, read_chat                      ↓ MEMORY.md indexes
          MCP server · /recall skill · CLI            session-start hook · memory_search
```

1. **Index.** Each agent's history is parsed into your turns and the agent's turns. Tool
   output and text the tools inject (system reminders, plan-mode boilerplate) are dropped,
   and secrets are redacted before anything is stored. Headless runs and subagents stay
   searchable but are never mined for memory. Clones and worktrees of one repo share a
   project, keyed on the git remote.
2. **Digest.** Each chat shrinks to your words plus the start and end of each agent reply,
   which is where agents say what they will do and what they did.
3. **Extract.** The model proposes candidate memories (`preference`, `fact`, `decision`,
   `lesson`, `open`), each with a supporting quote. A quote that can't be found in the chat
   demotes the candidate to low confidence.
4. **Consolidate.** Per project, oldest first, candidates are folded into notes: create,
   update, supersede (newer facts win) or discard. Every operation is validated before a
   file changes. Open threads expire after 14 days.
5. **Commit.** The `MEMORY.md` indexes are rebuilt, and the run becomes one git commit in
   `~/.agents/memory`.

Each step resumes where the last run stopped, so a crash or a spent budget only delays the
rest until the next night.

## Memory notes

```text
~/.agents/memory/
  MEMORY.md                      how you work, and your projects
  global/<note>.md               cross-project preferences
  projects/<repo>/MEMORY.md      what an agent loads when it starts in that repo
  projects/<repo>/<note>.md      facts, decisions, lessons, open threads
  archive/                       superseded and expired notes
```

A note is plain markdown with a little YAML:

```markdown
---
id: run-migrations-through-make-3f2a
type: lesson
scope: project
confidence: high
last_seen: '2026-09-30'
sources: [codex:0192f…, claude:7c41e…]
---
Run database migrations with `make migrate`, never by hand: the Makefile also seeds
the audit tables. Why: two hand-run migrations left staging without audit rows.
```

The files are the source of truth. Edit them, and recall keeps your edits; delete one, and
it is never learned again. The folder is a git repository, so `git log` shows what each
night changed and `git revert` undoes it. It also opens as an Obsidian vault, though nothing
requires Obsidian. Why markdown rather than a vector or graph store:
[docs/research/agent-memory-prior-art.md](docs/research/agent-memory-prior-art.md).

## Commands

| Command | Does |
| --- | --- |
| `recall setup` | the guided first run |
| `recall model [--show]` | choose, or show, the model that writes memories |
| `recall search <words>` · `show <id>` · `recent` | chat history |
| `recall dream [--max-minutes N] [--max-calls N]` | update memory now |
| `recall memory [search <words> \| show <id>]` | browse notes |
| `recall context [--cwd DIR]` | print the memory block an agent gets at session start |
| `recall install` · `uninstall [--only mcp,skill,hook]` | connect to agents (every config file is backed up first) |
| `recall schedule enable \| disable \| status` | the nightly run |
| `recall eval search \| memory` | quality checks |

Environment: `RECALL_HOME` moves the index, config and logs (default `~/.agents/recall`);
`RECALL_MEMORY` moves the notes (default `~/.agents/memory`).

### Uninstall

```bash
recall uninstall && recall schedule disable
uv tool uninstall recall-agents
rm -rf ~/.agents/recall          # the index, config and logs
# ~/.agents/memory holds your notes; keep it or delete it
```

## FAQ

**Does recall send my chats anywhere?**
Only to the model you choose, and only digests of chats, after secrets are redacted. With a
local model nothing leaves your machine. recall has no telemetry. See [SECURITY.md](SECURITY.md).

**What does it cost?**
With a local model, nothing but electricity. With a subscription it uses your plan's
allowance; the nightly run is capped (80 model calls by default), and a first backfill of a
few thousand chats spreads across several nights. With an API key, a small model such as
Claude Haiku costs cents per night for typical use.

**How is this different from Claude Code's or Codex's own memory?**
Each tool's memory only knows that tool's chats and lives in its own format. recall spans
all of them, keeps memory as plain files you own, and links every note to its source chats.

**Can I use it with only one agent?**
Yes. Search and memory work with any one of Claude Code, Codex or Cursor.

**Windows?**
Not yet. The chat parsers are portable, but file locking, the nightly scheduler and key
storage need Windows equivalents. Contributions welcome.

**Will memory pick up something wrong?**
Sometimes. Every note carries its sources and a confidence level, newer chats supersede
older notes, and you can edit or delete any note. Treat memory as a well-informed colleague's
notes, not as ground truth.

## Quality

There are no unit tests by design. recall is checked with evals that drive the real
pipeline, plus an end-to-end smoke test in CI:

```bash
recall eval search                 # hand-written and auto-generated "find that chat" queries
recall eval memory --fresh         # extraction against hand-labelled chats; average several runs
```

On one developer's 2,700 chats, search finds the intended chat first 80–100% of the time and
in the top five 97–100% of the time. Memory extraction quality depends on the model. Small
hosted models find roughly half to three quarters of hand-labelled memories, with very little
junk. Single runs vary by ±12 points, so compare averages.

## Contributing

Contributions are welcome, especially new agent sources (opencode, Gemini CLI, Copilot CLI,
Goose and more are mapped out in
[docs/research/agent-storage-and-integration.md](docs/research/agent-storage-and-integration.md))
and new model providers. See [CONTRIBUTING.md](CONTRIBUTING.md) to get set up, and
[SECURITY.md](SECURITY.md) to report a vulnerability privately. Please never include real
chat content in issues or pull requests.

**Roadmap:** more agents and chat-export imports, a Codex session-start hook, a "remember
this" write path from any agent, usefulness evals that replay tasks with and without memory,
and a Claude Code plugin package.

## License

[MIT](LICENSE)

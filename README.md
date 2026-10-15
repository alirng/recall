<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/recall-dark.svg">
    <img src="docs/brand/recall-light.svg" alt="recall" width="271" height="41">
  </picture>
</h1>

One memory for all your coding agents.

[![CI](https://img.shields.io/github/actions/workflow/status/alirng/recall/ci.yml?branch=main&label=CI&style=flat-square&labelColor=2b2b2e)](https://github.com/alirng/recall/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-58585d?style=flat-square&labelColor=2b2b2e)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-58585d?style=flat-square&labelColor=2b2b2e)
![Status: alpha](https://img.shields.io/badge/status-alpha-58585d?style=flat-square&labelColor=2b2b2e)

Your coding agents forget everything between sessions, and none of them knows what you told
the others. recall reads the chat history that Claude Code, Codex and Cursor already keep on
your machine, and gives every agent:

- **Search across every chat.** "Find the Cursor chat where we planned the auth refactor"
  works in any agent, whichever tool the chat happened in.
- **Memory.** A small set of plain markdown notes, rewritten nightly: how you like to work,
  what each project needs, what was decided, what went wrong, and what is still open. Each
  note links to the chats it came from.
- **Your projects, whichever tool you used.** Clones, worktrees, moved folders and chats with
  no folder, grouped into the projects you actually work on, with the same rules given to
  every agent.
- **Agents that know about each other.** See what each chat is working on, and hear about it
  before two of them edit the same file.

It runs on your machine, and indexing and search make no LLM calls. For the nightly notes you
choose the model: a local one that keeps everything private, your ChatGPT or Claude
subscription, or your own API key or server.

```text
you   › what did we decide about retries in the payments service?
agent › Searching your past chats… In "Payment webhook retries" (Codex, 2 Oct) you settled
        on exponential backoff capped at 5 attempts, and a dead-letter queue for the rest.
        Memory also notes: "Never retry card captures; they are not idempotent."
```

## Install

Paste this into Claude Code, Codex or Cursor:

```text
Install recall for me: follow https://raw.githubusercontent.com/alirng/recall/main/INSTALL.md
```

The agent checks what you have, asks which model should write your memories (the one choice
that decides where chat text goes), installs, sets up, and checks it worked.

Or by hand, on macOS or Linux with Python 3.11+:

```bash
uv tool install 'recall-agents[embeddings]'     # or: pipx install 'recall-agents[embeddings]'
recall setup
```

Setup finds your agents, picks the model and tests it, indexes your history, asks what agents
should do when they share a file, connects the MCP tools, the `/recall` skill and the hooks,
schedules the nightly run, and starts the first one in the background. If you use Codex on a
Python older than 3.14, add the `zstd` extra (or install the `zstd` command) so recall can read
the chats Codex compresses after a week. Ask any agent about an
earlier conversation straight away. Once that first run has written notes, Claude Code and
Codex open each session with your preferences and the current project's notes loaded.

## Results

recall is measured on public benchmarks built from coding work, and every number is
reproducible with `recall eval`. [docs/benchmarks.md](docs/benchmarks.md) has every result,
the method, the caveats and the commands.

- **Finds the earlier fix for a new bug 80% of the time** (top five, SWE-ContextBench Lite:
  99 GitHub issues searched against 300 Claude Code sessions). The memory products the
  benchmark's authors tested found it 39–73% of the time, each at its own cutoff, after running
  an LLM over every session. recall is ahead at each of those cutoffs.
- **Agents follow your latest conventions.** On MemoryCode, with the notes loaded at session
  start, Claude Haiku 5.5 writes code that follows the latest conventions on histories of 20 to
  100 sessions 62% of the time, against 40% with search alone. The notes, written by
  gpt-6-luna, hold about three in five of the conventions in their latest form.
- **No LLM calls to index or to search.** On ~2,800 chats a search takes about 15 ms, or
  50 ms with embeddings (median, on a laptop).

<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/benchmarks/swecb-dark.svg"><img src="docs/benchmarks/swecb-light.svg" alt="Bar chart of SWE-ContextBench Lite past-fix retrieval over all 300 sessions, each memory product at the cutoff its paper reports and recall at the same cutoff. Top 3: recall 0.3.0 76.8%, recall 0.2.1 44.4%, OpenViking 51.5%, Mem0 39.4%. Top 10: recall 0.3.0 92.9%, recall 0.2.1 73.7%, LangMem 73.3%. Top 15: recall 0.3.0 97.0%, recall 0.2.1 86.9%, Supermemory 59.6%" width="760"></picture></p>

On LongMemEval, the chat benchmark memory products usually report, recall's search puts a
session that answers the question in its top five 98.7% of the time.

## How agents use it

| | Claude Code | Codex | Cursor |
| --- | --- | --- | --- |
| Its chat history is searchable | yes | yes | yes, and older chats on macOS |
| MCP tools and the `/recall` skill | yes | yes | yes |
| Notes loaded at session start | automatically | automatically, once you trust the hook | on request |

- **At session start**, a hook loads your working preferences and the current project's
  notes, plus a line for each always-on rule the repo gives another agent but not this one.
- **Mid-session**, the same hook can hand over memory at the moment it applies: when your
  prompt closely matches an earlier chat, or when the agent reads a file that scoped rules,
  notes or an earlier chat cover. It stays quiet unless it is sure.
- **On demand**, read-only MCP tools let the agent search and read past chats, and look up
  notes and projects.
- **When you ask**, `/recall what did we decide about auth` works in any agent with skills.

Agents treat notes and transcripts as background, not instructions: the current request and
the code win when they disagree. T3 Code, OpenCode and Gemini CLI are partly supported; see
[How agents use it](docs/how-it-works.md#how-agents-use-it).

## Memory you can read

Notes are plain markdown, in a git repository at `~/.agents/memory`:

```markdown
---
id: run-migrations-through-make-3f2a
type: lesson
scope: project
title: Run migrations through make
project: github.com/acme/harbor
project_name: harbor
confidence: high
status: active
created: '2026-09-12'
updated: '2026-09-30'
last_seen: '2026-09-30'
sources:
- codex:0192f3a8-5c1e-7d2b-9e4f-3a6b8c1d2e4f
- claude:7c41e9b2-1f3a-4d5e-8b6c-9a0f2e3d4c5b
---
Run database migrations with `make migrate`, never by hand: the Makefile also seeds
the audit tables. Why: two hand-run migrations left staging without audit rows.
```

The files are the source of truth. Edit a note and recall keeps your edit; delete one and it
is never learned again. `git log` shows what each night changed, and `git revert` undoes it.
Newer chats supersede older notes, and work and personal memory can be kept apart. How notes
are written and loaded: [docs/how-it-works.md](docs/how-it-works.md).

## Projects and rules

A project is rarely one folder. It is a repo cloned twice, a dozen worktrees, the folder it
lived in before you moved it, and the chats you had with no folder open at all. recall follows
each project across all of that:

```text
$ recall projects
                            May   Jun   Jul   Aug   Sep   Oct
harbor            work      ●─────●─────●─────●─────●─────●       412 chats
harbor-mobile     work                  └─────○─────●─────○       23 chats · from harbor
blog              personal                    ○─────●─────●       76 chats
tidepool          personal  ○                                     190 chats · paused
```

Each agent also reads its own instruction files (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`),
so a repo set up well for one agent is a stranger to the others. `recall rules` shows which
agent misses which rule, and `--write` copies the missing ones where all three read them.
More in [docs/projects.md](docs/projects.md).

## Several chats, one repo

Run agents side by side and sooner or later two of them change the same file. `recall status`
shows what each chat is working on, and where they overlap:

```text
$ recall status
src/auth/session.py will conflict  (harbor · main ↔ new-login)
    Claude Code · Fix the login redirect
    Codex · Restyle the sign-in page
```

Before an agent edits a file another chat has open changes to, recall tells it, or stops it to
check with you, by a rule you set. The two chats can message each other to agree who goes
first. More in [docs/coordination.md](docs/coordination.md).

## Which chat wrote this

```text
$ recall why src/auth/session.py:42
src/auth/session.py:42  (github.com/acme/harbor)

3f9c2a1e  2026-09-14  Keep the redirect target through login  (1 line here)
  Claude Code · 2026-09-14 · Fix the login redirect
    made this commit and wrote 38 of its 41 lines · claude:5b1e…
    asked: "after login it lands on the home page, it should go back where you were"
```

`recall why` follows a file, a line or a commit back to the chats that produced it, and agents
can ask the same through MCP. The links stay in recall's index on your machine; nothing is
written to the repo. More in [docs/commit-links.md](docs/commit-links.md).

## Several accounts and installs

A personal and a work account, or Cursor next to Cursor Nightly, means several agent folders.
recall finds each one, indexes it, and connects it; `recall doctor` lists them:

```text
claude          ~/.claude
                connected; 5,812 chats, last indexed 3 min ago; a***@***
claude-work     ~/.claude-work
                waiting for first launch (the agent has not used this folder yet; recall writes nothing there)
codex           ~/.codex
                hooks written, trust pending: trust them once with /hooks in Codex; 212 chats, last indexed 3 min ago
```

A second account's chats are searchable at once, and become memory only when you say so.
recall can also keep every folder connected on its own, but only once you say yes to that.
More in [docs/accounts.md](docs/accounts.md).

## Choosing a model

recall writes its notes with a language model. Pick the one that fits your privacy and budget:

| Option | What it uses | Where chat excerpts go |
| --- | --- | --- |
| **Local model** | Ollama, LM Studio or llama.cpp on this machine | nowhere: they stay on this machine |
| **ChatGPT** | your ChatGPT plan, through the Codex CLI | OpenAI, under your plan's data terms |
| **Claude** | your Claude plan, through Claude Code (`haiku`, `sonnet` and `opus` run the 5.5 models) | Anthropic, under your plan's data terms |
| **API key** | OpenAI, Anthropic or OpenRouter | that provider, billed to your key |
| **Your server** | any OpenAI-compatible endpoint, such as vLLM or Ollama on another box | your server |

Change it any time with `recall model`. Configuration, semantic search and every command are
in [docs/reference.md](docs/reference.md).

## FAQ

**Does recall send my chats anywhere?**
Only to the models you choose, and only after secrets are redacted: digests of chats to the
model that writes notes, and message text to a hosted embedding service if you use one. With
local models nothing leaves your machine. recall has no telemetry. See [SECURITY.md](SECURITY.md).

**What does it cost?**
With a local model, nothing but electricity. With a subscription it uses your plan's
allowance: the nightly run is capped at 80 model calls by default, so a first backfill of a
few thousand chats spreads across several nights. With an API key, a small model such as
Claude Haiku costs cents a night.

**How is this different from Claude Code's or Codex's own memory?**
Each tool's memory only knows that tool's chats and lives in its own format. recall spans all
of them, keeps memory as plain files you own, and links every note to its source chats.

**How long do my chats stay searchable?**
As long as you keep recall's index. Claude Code deletes transcripts after 30 days by default,
but recall keeps a compressed copy of every chat it has indexed. `recall forget` removes one.
[Retention and forgetting](docs/reference.md#retention-and-forgetting).

**Will memory pick up something wrong?**
Sometimes. Every note carries its sources and a confidence level, newer chats supersede older
notes, and you can edit or delete any note. Treat memory as a well-informed colleague's notes,
not as ground truth.

**Windows?**
Not yet. The chat parsers are portable, but file locking, the nightly scheduler and key
storage need Windows equivalents. Contributions welcome.

## Commands

| Command | Does |
| --- | --- |
| `recall setup` | the guided first run |
| `recall search <words>` · `recent` | your chat history |
| `recall memory` | browse notes |
| `recall projects` | your projects over time, as lanes |
| `recall rules` | which agent reads which instructions |
| `recall status` | what each agent is working on now |
| `recall why <file>[:line]` | the chats that wrote a file, a line or a commit |
| `recall doctor` | each agent folder, and whether recall is connected to it |
| `recall model` | choose the model that writes memories |
| `recall dream` | update memory now, instead of waiting for the night |
| `recall forget <chat id>` | remove a chat from recall |

`recall --help` lists the rest. Every command, the configuration and how to uninstall:
[docs/reference.md](docs/reference.md).

## Contributing

Contributions are welcome, especially new agent sources and model providers. See
[CONTRIBUTING.md](CONTRIBUTING.md) to get set up, and [SECURITY.md](SECURITY.md) to report a
vulnerability privately. Please never include real chat content in issues or pull requests.

## License

[MIT](LICENSE)

<a href="https://rererecall.com"><img src="docs/brand/footer.jpg" alt="rererecall: one memory for all your coding agents" width="100%"></a>

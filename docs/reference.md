# Reference

Models, configuration, every command, and uninstalling.

## Setup

```bash
recall setup
```

Setup asks a few questions and does the rest:

```text
1. Agents      finds Claude Code, Codex and Cursor; import from all of them or some
2. Model       picks who writes your memories, then tests it with a live call
3. Index       reads your chat history into a local search index
4. Sharing     what an agent does when it edits a file another chat has open changes to
5. Connect     adds the MCP tools, the /recall skill, and session-start and edit hooks; offers to
               keep Claude Code chats longer; then asks once whether to keep every agent folder
               connected on its own (default no)
6. Schedule    updates memory nightly at 03:30 (launchd on macOS, systemd on Linux)
7. First run   starts in the background, newest chats first
8. Embeddings  with --embeddings, embeds your chats in the background
```

`recall setup --yes --model claude:haiku --embeddings nomic-embed-text` answers every question
up front, which is how an agent runs it. `--yes` leaves the keep-connected question unanswered;
add `--autopilot` (or `--no-autopilot`) to answer it. **An agent must ask the person before it passes
`--autopilot` or runs `recall autopilot on`**: it writes into other tools' config folders. Ask any agent
about an earlier conversation straight away. Once the first run has written notes, Claude Code and
Codex open each session with your preferences and the current project's notes loaded.

## Choosing a model

recall distils chats with a language model. Pick the one that fits your privacy and budget:

| Option | What it uses | Where chat excerpts go |
| --- | --- | --- |
| **Local model** | Ollama, LM Studio or llama.cpp on this machine | nowhere: they stay on this machine |
| **ChatGPT** | your ChatGPT plan, through the Codex CLI | OpenAI, under your plan's data terms |
| **Claude** | your Claude plan, through Claude Code (`haiku`, `sonnet` and `opus` run the 5.5 models) | Anthropic, under your plan's data terms |
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
batch = "auto"                         # API keys: send the nightly run as batches, at half price; "off" never
consolidate = "anthropic:claude-sonnet-5-5"  # optional: a second model folds memories into notes
fallback = "claude:haiku"              # optional: the plan to use once the key's credits run out
```

Whichever you pick, every call is validated against a strict JSON schema, retried with
backoff, cached, logged, and capped by time and call budgets (with an API key, a dollar budget). recall's own calls never show
up in your chat history: Claude Code runs with no tools, no MCP servers, project settings only
and no saved session, and Codex runs read-only and ephemeral, without your user config. The
Anthropic API needs the `recall-agents[anthropic]` extra.

For a local model, smaller is kinder to your laptop: a 4B model suits 16 GB of memory.

### Batch API and the dollar budget

With an Anthropic or OpenAI API key, the nightly run uses the provider's batch API: the same
calls at half the price, answered within minutes rather than seconds. recall sends the night's
extraction as one batch, then consolidation in rounds (every project's next batch of
candidates side by side), and records each call's cost. A round of fewer than three calls is
asked directly. A batch still running after 20 minutes is cancelled and the rest of the night
runs synchronously; one still running when the time budget ends is collected by the next run.
`batch = "off"` or `recall dream --no-batch` turns it off. Subscription CLIs (ChatGPT, Claude)
and local models never use it.

`[dream] max_usd` (default $1.00, or `--max-usd`) caps what a run may spend, in place of the
call cap, which is there for plans' rate limits. A model whose price recall does not know is
not capped by dollars; set `price = [in, out]` (dollars per million tokens) in `[model]`.
Claude Max and Team plans include monthly API credits that cover this; give recall its own
Console workspace with a spend limit. When the credits run out, the run carries on with
`fallback` if you set one, or stops cleanly and continues the next night.

On my history, Claude Haiku 5.5 read a backlog of 2,452 chats in one batch for $0.77, and the
whole backlog (2,859 candidate memories folded into notes) took two nights at the $1 default.
A typical night of about 40 new chats costs about 3 cents.

## Semantic search (optional)

Search matches words out of the box. To also match meaning, so "where did we land on
retries" finds a chat that only ever said "backoff", give recall an embedding model:

```bash
uv tool install 'recall-agents[embeddings]' --force
ollama pull nomic-embed-text
```

```toml
[embeddings]
provider = "ollama"            # or "openai-compatible" with base_url: LM Studio, llama.cpp, a hosted API
name = "nomic-embed-text"
```

Then run `recall embed` once (about seven minutes per 20,000 messages on a laptop). New chats
are embedded as they are indexed, and search falls back to words alone whenever the model is
not running. On LongMemEval it raises the share of questions with every answer session in the
top five from 87% to 91%; on my chats it found the intended chat in the top five for 88% of
100 paraphrased questions, against 83% with words alone (one run each).

## Codex's compressed chats

Codex compresses chats it has not touched for a week to `.jsonl.zst`. recall reads them with
Python 3.14's built-in zstd, else the `zstd` extra (`uv tool install 'recall-agents[zstd]'`),
else the `zstd` command (`brew install zstd`). With none of them, `recall index`,
`recall stats` and `recall status` say how many Codex chats they cannot read.

## When chats are read

A tool call first reads the chat files that changed since the last one, so a chat from a few
minutes ago is already searchable. Cursor keeps its older chats, and the subagent and side chats it
runs today, in `state.vscdb`, a database of several GB that cannot say which chats changed. recall
reads it in full on the first index (in the background, at low priority, for at most 15 minutes at a
time), in the nightly run and on `recall index`, and skips it while the database's files are
unchanged. A new Cursor side chat is therefore searchable after the next nightly run or
`recall index`.

A chat that fails to index is logged by its path, not its words, and tried again at the next two
runs; then it waits until its file changes. Chats that index fine are not held up by it.

## Battery

On battery, recall's background work waits for power. `recall dream` defers itself, the nightly run
(launchd runs a missed night when the Mac wakes, often on battery) and one typed at a terminal alike,
and a tool call starts no background job. `recall status` and `recall doctor` say when `recall dream`
deferred. `recall dream --on-battery run` runs one now, and `on_battery = "run"` under `[power]` in
config.toml lets everything run on battery. Background jobs always run at low priority.

On Linux the systemd service also carries `ConditionACPower=true`, so systemd skips the run on battery
and nothing records it. `recall schedule enable` writes that line: run it again after changing
`on_battery`, or to add it to a timer an older release set up.

## Commands

| Command | Does |
| --- | --- |
| `recall setup [--yes] [--model SPEC] [--embeddings NAME]` | the guided first run; the flags answer its questions up front |
| `recall model [--show]` | choose, or show, the model that writes memories |
| `recall search <words>` · `show <id>` · `recent` | chat history |
| `recall forget <id> \| --project NAME [--yes]` | remove chats from the index, archive, vectors, exploration reports and the cached answers that quote them; lists the notes that cite them |
| `recall embed` | embed messages for semantic search (needs `[embeddings]` in config.toml) |
| `recall dream [--max-minutes N] [--max-calls N] [--max-usd X] [--no-batch] [--on-battery run]` | update memory now; unlike the nightly run, it has no budget unless you give one; on battery it waits for power unless told to run |
| `recall memory [search <words> \| show <id> \| link]` | browse notes; `link` recomputes links now |
| `recall projects [suggest \| show <name>] [--months N] [--all]` | your projects as lanes; `suggest` drafts projects.yaml |
| `recall rules [project \| folder] [--write]` | which agent reads which instructions; `--write` closes the gaps |
| `recall usage [--since 7d]` | how recall itself gets used, from its local log |
| `recall status [--since 12h] [--json]` | what each agent is working on now, files two chats both have open changes to, and agents to restart after an upgrade |
| `recall why <file>[:line] \| --commit <sha>` | the chats that wrote a file, a line or a commit (local only) |
| `recall links [--episodes]` | link commits to chats now; `--episodes` (experimental: a judge found only 24% of 21 sampled episodes right) lists failures a later commit fixed |
| `recall context [--cwd DIR] [--agent codex]` | print the memory block an agent gets at session start |
| `recall resolve --repo <repo> --file <file>` | message every chat in an overlap to agree who goes first (the menu bar's Resolve) |
| `recall hook --agent <agent>` | answer one of an agent's hooks from its JSON input; `recall install` sets these up |
| `recall explore [lookup <question> \| backfill] [--cwd DIR]` | earlier exploration reports for this repo, and what a new exploration would get |
| `recall jit [<prompt>] [--file F] [--error TEXT] [--cwd DIR]` | what the mid-session hooks would hand an agent, and why |
| `recall install` · `uninstall [--only mcp,skill,hook] [--root NAME]` | connect to agents, in every folder found or one root (every config file is backed up first); install also offers to keep Claude Code chats for ten years (`--only retention`), and `install --repoint` moves hooks that run another recall onto this one |
| `recall doctor [--json]` | each agent folder: connected or not, account hint, when last indexed |
| `recall autopilot [on \| off] [--dry-run]` | keep every agent folder connected by itself; no argument runs the check now |
| `recall schedule enable \| disable \| status [--at HH:MM]` | the nightly run, capped by `[dream]` in config.toml (120 minutes, 80 model calls) |
| `recall index [--force] [--source NAME]` · `stats` | index new chats now, and what is in the index |
| `recall serve` | the MCP server, which `recall install` registers with each agent |
| `recall eval search \| agent \| memory \| session \| links \| swe-contextbench \| memorycode \| amb \| explore \| longmemeval \| locomo \| jit` · `eval compare` | quality checks: see [benchmarks.md](benchmarks.md) |

Each command's `--help` lists all its options.

Environment: `RECALL_HOME` moves the index, config and logs (default `~/.agents/recall`);
`RECALL_MEMORY` moves the notes (default `~/.agents/memory`).

## Uninstall

```bash
recall uninstall && recall schedule disable
uv tool uninstall recall-agents
rm -rf ~/.agents/recall          # the index, config and logs
# an API key saved to the keychain stays there, under the service name "recall"
# ~/.agents/memory holds your notes; keep it or delete it
```

## Retention and forgetting

Chats stay searchable as long as you keep recall's index. Claude Code deletes transcripts after
30 days unless you raise `cleanupPeriodDays`, and Codex compaction has been seen rewriting a
long session file to a fraction of its size. recall keeps a compressed copy of every chat it has
indexed (about 16 MB for 2,900 chats), and when a file loses messages it already indexed, it
keeps them. `recall install` offers to raise Claude Code's period to ten years. When your
organisation sets that period through managed settings, recall keeps those chats no longer than
the policy allows. `recall forget <chat id>` (or `--project NAME`) removes a chat from recall's
index, archive, embeddings, extraction records and exploration reports, and from the cached
model answers that quote it; it lists the notes that cite the chat but leaves them to you, and
the usage log keeps only the words you asked and chat ids. The short-lived notes between chats
about a shared file (`cache/mail.jsonl`) can quote up to 140 characters of what a chat was
asked; forget leaves them to age out.

# Changelog

All notable changes to recall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **What your agents are doing now.** `recall status` lists the Claude Code, Codex and Cursor
  chats active in the last 12 hours, the branch each is on and the files it edited, and flags a
  file that two chats in one repo both have open changes to: whether the two would conflict if
  merged now (`git merge-tree`), or share one working copy. Each flagged file comes with a note
  to paste into the other chat. It reads agent history directly and caches each chat by its
  size and mtime, so it stays current and cheap.
- **Rules for when two chats change the same file.** Before an agent edits a file another
  chat has open changes to, recall applies your rule for the situation: another live chat in
  the same working copy, a branch that would conflict, or an overlap that merges cleanly.
  `ask` stops the agent once so it checks with you, `worktree` has it move into its own git
  worktree, `tell` lets the edit through and tells it, `off` does nothing. Each other chat is
  raised once, listing every file the two share. `recall setup` offers presets; the
  recommended one asks before two live chats edit one working copy and mentions conflicts.
- **Edit hooks for Claude Code, Codex, Cursor, Gemini CLI and OpenCode** (a plugin), added
  by `recall install` (Codex runs them once you trust them in `/hooks`). T3 Code runs Claude
  Code, Codex and Cursor underneath, so it is covered too. Every hook also logs the edit, so `recall status` sees chats from agents
  whose history recall cannot read yet. Agents without hooks can call the new MCP tool
  `open_elsewhere` before editing.

## [0.3.0] - 2026-10-13

recall measured on coding work: finding the session where a bug was fixed before, how much an
agent reads to get there, and notes that keep up with the conventions you set. With 0.3.0's
notes loaded at session start, an agent follows the latest conventions of a long MemoryCode
history 65% of the time, against 45% with 0.3.0's search alone (held-out histories; one run and
three). Method and numbers: docs/research/coding-memory-hillclimb.md.

### Added

- **Benchmarks built from coding work.** `recall eval swe-contextbench` checks whether recall
  finds the past fix for a new GitHub issue among 300 real Claude Code sessions, by itself or
  through an agent using its tools. `recall eval memorycode` checks whether the nightly notes
  hold every coding convention you set, in its latest form, and whether an agent's code follows
  them. `recall eval amb` scores retrieval on agent-memory-bench. Each has a fixed train/test
  split, so a change is judged on cases it was not tuned on.
- **Evals on your own chats.** `recall eval search --v2`, `recall eval agent` and
  `recall eval memory --v2` score cases you write (questions and the chats that answer them,
  questions an agent must answer from past work, chats and the memories they should yield),
  with confidence intervals, paired comparisons (`--against`) and a results page per run.
  Answers and memories are graded by Claude Sonnet 5.5 against checkable claims.
- **One search, several phrasings.** `search_chats` takes `alternatives`, other words for the
  same thing, and returns the best chats across all of them, each listed once.

### Changed

- **Search finds past work from a pasted issue or error.** A long query no longer favours
  chats just for having a title. The past fix for a new bug report is in the top five 80% of
  the time, up from 47% (SWE-ContextBench Lite, 99 issues over all 300 sessions). Queries of 12
  words or fewer rank as before.
- **Agents read less to find the same thing.** Search results leave out fields that repeated
  others, and `read_chat` shows a long message's start, the passage that matches and its end
  (ask for one message to see it whole). An agent takes in an estimated 37% fewer Claude tokens
  per question on held-out SWE-ContextBench issues (about 9.0k against 14.4k, tool definitions
  included), and finds as much.
- **Lighter in every request.** recall's tool definitions dropped repeated schema titles: 2,480
  Claude tokens on every request an agent makes with recall connected, down from 2,591, even with
  the new `alternatives`. Codex and Cursor now get search results as compact text instead of
  JSON, 29% fewer characters per search over 481 logged searches.
- **Notes catch what you set, and leave out the task at hand.** The model now reads every chat
  on its own and decides first whether a memory outlasts the current task. Notes hold 62% of the
  conventions you set on MemoryCode, in their latest form, up from 9% (40 held-out histories,
  three runs). More notes take more consolidation calls, and the new extraction prompt (v4) makes
  the nightly run read past chats again, newest first, within its call budget (80 by default),
  so a large history takes several nights.

### Fixed

- **Batched chats were mostly skipped.** Given a dozen chats and one list to fill, the model
  wrote memories for the first few. Each chat now gets its own entry: on a fresh held-out set of
  my chats, 61% of expected memories are found, up from 48% (six runs).
- **One-off requests became preferences.** "Do X now" could be stored as how you always work.
  A preference now needs you to state it as a rule or repeat it; on the same held-out chats, 88%
  of stored memories are worth keeping, up from 83%. On an earlier test set of my chats, already
  used for several decisions, the change found no more memories and precision fell from 98% to
  92%; the research note has both results.
- docs: EverMemOS's token column for LongMemEval reads 2.8k in its paper, not 6.7k.

## [0.2.1] - 2026-10-09

### Added

- INSTALL.md: install instructions written for a coding agent, so "install recall for me:
  follow <link>" in Claude Code, Codex or Cursor sets everything up. The agent asks only which
  model writes memories, since that decides where chat text goes.
- `recall setup --model SPEC --embeddings NAME` answers those questions up front, for agents
  and scripts.
- docs/benchmarks: the benchmark results as data, and charts drawn from it.

### Fixed

- The Claude plan provider runs Claude Code from an empty folder. From the shared temp folder,
  Claude Code indexed thousands of files at the start of every call.

## [0.2.0] - 2026-10-09

### Added

- Projects. `~/.agents/memory/projects.yaml` lists the projects you work on, each with the
  git remotes and folders it spans, its kind, status and the project it grew out of.
  `recall projects suggest` drafts it from your chat history, grouping clones, worktrees,
  moved folders and Cursor's names for deleted folders; recall only ever appends to the file.
  Chats with no project folder join a project when they name one of its `words`.
- `recall projects`: every project over time as git-style lanes, and `recall projects show`
  for one. The MCP tool `list_projects` gives agents the same view.
- `recall rules`: which instruction files Claude Code, Codex and Cursor each read in a repo,
  and `--write` to close the gaps by copying committed always-on Cursor rules into a marked
  block in AGENTS.md and adding `@AGENTS.md` to CLAUDE.md. Personal (gitignored) rules are
  never written to committed files.
- A Codex session-start hook: `recall install` adds it to `~/.codex/hooks.json`, so Codex
  loads memory at session start the way Claude Code does once you trust it in `/hooks`.
- `recall eval longmemeval`: session retrieval on the public LongMemEval benchmark, scored with
  its own metrics, so search changes are measured against published numbers. `--official`
  scores the benchmark's own question subset, `--hybrid` adds embeddings, and `--qa MODEL`
  runs it end to end: recall retrieves, the model answers and judges with the benchmark's
  prompts, and the run reports accuracy, context tokens and model calls; `--judge` grades
  with a different model, and answering and judging tokens are reported apart.
- `recall eval locomo`: session retrieval on LoCoMo.
- `recall eval search` also scores `paraphrase_cases.jsonl` when present: chats described in
  other words than they used, where semantic search matters.
- Optional semantic search. With the `embeddings` extra and an `[embeddings]` table in
  config.toml (a local model through Ollama, or any OpenAI-compatible endpoint), each message
  and note gets a vector, and search fuses meaning with keywords. `recall embed` backfills;
  new chats are embedded as they are indexed. Search falls back to keywords when the model is
  unreachable.
- `search_chats` takes `until` (a date window together with `since`) and `exclude` (chat ids
  already read), and each result lists its best matching messages as `hits`.
- A usage log: recall records each MCP call, session-start load and search, with the agent
  that made it, in `~/.agents/recall/usage.jsonl`. `recall usage` sums it up; set
  `usage_log = false` in config.toml to turn it off.
- Links between notes. Each note ends with wikilinks to its closest notes, recomputed after
  every memory run from shared words, shared source chats and shared projects, with no model
  call. Hand-written `[[note-id]]` links count too. `recall memory link` recomputes them now,
  and the MCP tool `read_note` returns them with the reason for each.

### Changed

- Chat search ranks each chat several ways and fuses the rankings: its best message, the
  summed score of its best three messages, the title its agent gave it, and a message that
  holds every word. Filler words ("what", "did", "we") are dropped from questions. On
  LongMemEval, the share of questions with every answer session in the top five rose from
  0.717 to 0.872. See docs/research/retrieval-benchmarks.md.
- A project name from projects.yaml in `search_chats`, `recent_chats`, `memory_search` and
  `--project` covers every folder the project spans, not just names that match.
- The session-start block also lists always-on rules the repo gives other agents but not
  this one, and loads the notes of every folder in the current project.
- `recall context` takes `--agent`, and reads Cursor's `workspace_roots` when a hook sends
  no `cwd`.

## [0.1.0] - 2026-10-08

First public release.

### Added

- A local SQLite search index of the chat history from Claude Code, Codex and Cursor,
  including older Cursor chats.
- `recall search`, `recall show` and `recall recent` for chat history.
- Memory notes, kept up to date by a nightly `recall dream` run. Notes cover preferences,
  facts, decisions, lessons and open threads, and each one links to the chats it came from.
  Notes are plain markdown with YAML frontmatter, in a git repository at `~/.agents/memory`.
  Each run is one commit.
- A choice of model for memory: a local model (Ollama, LM Studio or llama.cpp), your ChatGPT
  plan through the Codex CLI, your Claude plan through Claude Code, an API key for OpenAI,
  Anthropic or OpenRouter, or any OpenAI-compatible server.
- API keys stored in the OS keychain, never in `config.toml`.
- Secrets are redacted before chats are indexed.
- Model output is checked against a JSON schema and retried with backoff. Every model call is
  logged. Runs are capped with `--max-minutes` and `--max-calls`.
- Open threads expire after 14 days, and weak single-source notes fade after six months.
- An MCP server (`recall serve`) with six read-only tools: `search_chats`, `read_chat`,
  `recent_chats`, `memory_search`, `read_note` and `project_memory`.
- A `/recall` skill, and a Claude Code session-start hook that loads your working preferences
  and the current project's memory.
- `recall install` and `recall uninstall` for the MCP server, skill and hook. Each agent
  config file is backed up before it changes.
- A nightly memory schedule through launchd on macOS or systemd on Linux (`recall schedule`).
- `recall setup`, a guided first run, and `recall model` to change the model later.
- `recall eval search` and `recall eval memory` to measure search and extraction quality.

[Unreleased]: https://github.com/alirng/recall/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/alirng/recall/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/alirng/recall/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/alirng/recall/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/alirng/recall/releases/tag/v0.1.0

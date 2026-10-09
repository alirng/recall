# Changelog

All notable changes to recall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

[Unreleased]: https://github.com/alirng/recall/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/alirng/recall/releases/tag/v0.1.0

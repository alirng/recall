# Changelog

All notable changes to recall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

# Contributing to recall

By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md).

Thanks for looking at recall. It is a small Python CLI, so most changes are small too.
Read the README first. If you work with a coding agent, point it at AGENTS.md, which lists
the rules for this repo.

## Setup

recall needs Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) manages the environment.

```bash
uv sync
uv run recall --help
```

Hooks, the MCP server and the nightly job run the installed `recall` command, not the copy
in your checkout. To test them against your changes, install the checkout in editable mode:

```bash
uv tool install --editable .
```

After you change dependencies, run the same command with `--force`.

## Before you open a PR

```bash
uvx ruff@0.16.10 format src && uvx ruff@0.16.10 check src
```

## How changes are checked

recall has no unit tests, and that is deliberate. How well it works depends on the chats you
have and the model you pick, so a test with a fixed input says little about whether a change
helped. The checks are:

- **Evals.** `recall eval search` checks that the right chat comes back for a question.
  `recall eval memory --fresh` checks which memories the model extracts from a set of chats.
  Both compare against expectations written by hand. Search also samples known-item queries
  from your own history. `recall eval longmemeval --cases longmemeval_s_cleaned.json` runs
  the public LongMemEval benchmark's session retrieval (download the file from
  huggingface.co/datasets/xiaowu0162/longmemeval-cleaned). A search change should hold up on
  both: LongMemEval has real labels but synthetic chats, your own history is the reverse.
- **End to end.** Run the CLI, the MCP server (`recall serve`) and the session-start hook
  (`recall context` prints what the hook loads) against real history.

Single eval runs vary. On the memory eval one missed memory moves the score by about 5
points, and single runs can differ by about 12 points. Run each eval several times and compare
the averages before and after your change:

```bash
uv run recall eval search
uv run recall eval longmemeval --cases ~/Downloads/longmemeval_s_cleaned.json
uv run recall eval memory --model claude:haiku --fresh
```

Personal eval sets name real chats, so they stay in `~/.agents/recall/evals/`. See Privacy
below.

## Adding an agent source

An agent source reads one agent's chat history and gives the index a list of chats. Copy
`src/recall/chats/sources/claude.py`.

1. Write `src/recall/chats/sources/<agent>.py` with two functions:
   - `detect()` returns how many chats it found on disk, or `None` if the agent is not
     installed.
   - `items()` yields one item per chat. Each item has a stable key and a cheap fingerprint,
     so unchanged chats are skipped on the next index. Its loader parses the chat only when
     it is needed.
2. Register the source in `src/recall/chats/sources/__init__.py`.
3. Read the agent's files and never write to them. Open SQLite databases with `mode=ro`.
4. Drop what the index should not hold: tool output, reasoning, and text the tool injects,
   such as system reminders. The existing sources show how.
5. If a parser change alters what the index should hold, bump `CHAT_SCHEMA_VERSION` in
   `src/recall/chats/store.py`. The index rebuilds itself. The memory tables and notes are not
   derived data, so never drop them.

Then run `uv run recall index`, and search for a chat you know with `uv run recall search`.
Field-level notes for about 25 agents are in
[docs/research/agent-storage-and-integration.md](docs/research/agent-storage-and-integration.md).

## Adding a model provider

Every provider sits behind one interface, so recall does not depend on any one vendor.

1. Subclass `Provider` from `src/recall/llm/base.py`. Implement `check`, `models` and
   `complete`, and give it a `Profile`. The profile sets how many chats go into one call. Agent
   CLIs carry a large fixed prompt, so they read about ten chats at once. Local models read a
   few at a time.
2. Wire the provider into `build()` in `src/recall/llm/__init__.py`. From there it works
   through `config.toml` and through `--model`.
3. Read API keys through `keys.load()`. Never put a key in `config.toml`, a log or the repo.
4. If people will choose the provider, add it to the table in the README. That table says
   where chat excerpts go, so keep it accurate.
5. Run `recall eval memory --model <provider>:<name> --fresh` a few times and compare the
   results with the baseline table in the README.

## Prompts

The extraction and consolidation prompts are markdown files in `src/recall/memory/prompts/`.
Each file starts with a version line, such as `<!-- version: v3 -->`, and every result records
that tag. Bump the version when a change should re-run past work. If it should not, leave it
alone.

Before and after a prompt change, run `recall eval memory --fresh` several times and compare
the averages.

## Privacy

recall works on private conversations, so the repo must never hold them. Keep these out of
commits, issues, pull requests and examples:

- chat transcripts, the index (`recall.db`), memory notes, API keys and `keys.json`
- personal eval sets, which live in `~/.agents/recall/evals/`

Use made-up chats instead. `evals/*.example.jsonl` shows each eval format with invented
examples. In a bug report, describe the problem in general terms, or write a small made-up
chat that shows it. Check logs and tracebacks for paths, project names and prompts before you
paste them.

## Pull request checklist

- [ ] `uvx ruff@0.16.10 format src && uvx ruff@0.16.10 check src` passes.
- [ ] If the change touches the CLI, MCP server or hook, you ran it end to end.
- [ ] If the change touches search or extraction, you ran the evals several times and
      put the averages in the PR description.
- [ ] A prompt change bumps its version line when it should re-run past work.
- [ ] A parser change bumps `CHAT_SCHEMA_VERSION` when the index needs a rebuild.
- [ ] Nothing private is in the diff, and any examples are made up.
- [ ] README or CHANGELOG.md is updated when a command, option, provider or behavior changes.

## Releasing (maintainers)

1. Move the "Unreleased" notes in CHANGELOG.md under a new version heading, and bump
   `version` in pyproject.toml and `__version__` in src/recall/__init__.py.
2. Commit, then tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
3. The Release workflow builds the package and publishes it to PyPI through trusted
   publishing. The one-time PyPI setup is described at the top of
   `.github/workflows/release.yml`.

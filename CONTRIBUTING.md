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

To tell a gain from noise, compare the runs case by case:
`recall eval compare BEFORE_RUN... vs AFTER_RUN...`. It prints the paired difference with a
bootstrap interval, how many cases got better or worse, each run's spread, and the smallest
difference those runs can detect ("detectable"). That is the bootstrap floor: 80% power for the
interval alone. The verdict also asks Δ to clear the run-to-run spread, so a true difference of
exactly "detectable" earns a better or worse verdict only about 60 to 70% of the time
(simulated, 40 cases, 3 to 5 replicates). A change smaller than that needs more repeats or more
cases, and the command says which. It gives no better or worse verdict under 10 paired cases, says
"run noise unmeasured" while an arm has fewer than 3 full replicates, and warns when only some
cases pair or the runs used different agent harnesses. `--flips` lists the cases whose outcome
changes from run to run, so you can repeat only those. Two habits keep the comparison honest:

- **Change one stage, freeze the rest.** For a consolidation change, replay an earlier run's
  extracted candidates (`--from-candidates RUN_DIR`) instead of extracting again.
- **Check what the notes add beyond having notes.** Loading any notes can help an agent,
  whatever they say. The MemoryCode agent eval has a `placebo` arm for this: it loads the
  notes of another history that shares no convention object or token with this history (the
  one closest in notes size, among those that qualify). A history with no such donor is
  skipped (`no_donor`), and so is one whose best donor's notes differ in size by more than
  25% (`no_donor_size`); each row logs `size_ratio`, the donor's notes over this history's.
  Read it with care:
  - Hook minus placebo is an upper bound on what the notes' content is worth, and placebo
    minus tools is the effect of loading notes that are not this history's, which includes
    any harm from misleading ones. It is not a pure priming control. MemoryCode has only 77
    distinct convention tokens in 360 histories, so without the filter donors often held this
    history's conventions or contradicted them.
  - The skips fall on the long histories, which have the most conventions, so a filtered run
    leans short: hook minus placebo from it says nothing about long histories, where the
    notes' gain is. On the 40 test histories, 21 keep a donor: 19 of 25 short, 2 of 15 long.
  - Donors repeat (those 21 rows come from 12 donors), so the rows are correlated and the
    bootstrap interval over them is optimistic.

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
   `src/recall/chats/store.py`. The chat tables rebuild from the agents' files, and chats whose
   files are gone come back from recall's archive (`src/recall/chats/archive.py`), which a
   rebuild never drops. Never drop the archive, the memory tables or the notes: they are not
   derived data. A change that should stop indexing some chats must also delete their archive
   copies, or the rebuild restores them.

Then run `uv run recall index`, and search for a chat you know with `uv run recall search`.
Field-level notes for about 25 agents are in
[docs/research/agent-storage-and-integration.md](docs/research/agent-storage-and-integration.md).

## Adding a hook

Every hook of every agent runs one command, `recall hook --agent <agent>`.
`src/recall/integrations/hooks.py` turns what the agent sends into an `Event` of one kind
(edit, read, prompt, stop, compact, session start, …), runs the handlers for that kind, and
merges their `Reply`s into the one answer that agent accepts.

1. Write the handler in your feature's own module: a function that takes the `Event` and
   returns a `Reply` (context for the model, a refusal, or text that keeps the agent going)
   or `None`. Keep it fast: it runs on every matching event.
2. Add it to `HANDLERS` under its kind, as `"recall.<module>:<function>"`.
3. Add the agent events that should fire it to `INSTALL`, for Claude Code, Codex and Cursor
   alike. `recall install` writes exactly these entries, replacing older ones of its own.
4. If the kind is new, teach `parse` to recognise it and `render` to answer it for each
   agent. Use formats confirmed in that agent's docs or source; a kind not confirmed for an
   agent stays silent there. Codex rejects replies with fields it does not know, Cursor's
   `preToolUse` must always print JSON, and Cursor runs Claude Code's hooks too (the claude
   adapter stands down when `cursor_version` is present).

Then pipe a recorded event through `recall hook --agent <agent>` for each agent and check the
reply.

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

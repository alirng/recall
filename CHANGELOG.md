# Changelog

All notable changes to recall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **recall never loses a chat it has indexed**, except one you forget and one past a retention
  period your organisation sets. Every chat is kept compressed in its own
  table, one copy per file it came from, which a rebuild never drops. When the parser changes,
  chats are re-read from their files, and those whose files are gone (Claude Code deletes
  transcripts after 30 days by default) come back from the archive, marked restored. When a
  re-read finds fewer messages than recall had (Codex compaction has rewritten session files),
  the missing ones are kept. A chat whose copies live in several files (Cursor writes one per
  project folder, Codex one in `sessions/` and one in `archived_sessions/`) now shows every
  message from all of them, where before only the file read last counted.
- **`recall forget <chat id>` and `--project NAME`** remove chats from the index, the archive,
  embeddings, extraction records and exploration reports, and keep them from being indexed
  again; the database file is rewritten afterwards so the words are not left in free pages.
  Cached model answers go only when they quote the chat (a paraphrase is not recognised).
  Notes that cite the chats are listed, not edited, with the git command that removes them
  from history. The usage log (queries and chat ids) and the short-lived notes between chats
  about a shared file, which can quote 140 characters of what a chat was asked, are left alone.
- **Retention.** `recall install` and `recall setup` offer to raise Claude Code's
  `cleanupPeriodDays` when it is under a year (backed up first; uninstall never lowers it).
  When an organisation sets that period through managed settings, recall forgets Claude Code
  chats older than it. Unverified: the format of the server-managed cache
  `~/.claude/remote-settings.json`. Claude Code's documentation confirms the path, not what
  is in the file; recall reads it as a settings object with `cleanupPeriodDays` at the top
  level, as it reads a `managed-settings.json` file or an MDM profile.
- **Chats that talk to each other.** When an edit hook finds another chat with open changes
  to the same file, recall messages that chat too: who is about to change which lines, and
  what they were asked. Agents answer through the new `message_chat` MCP tool to agree who
  goes first. An agent can only message chats recall introduced it to, with the token from
  recall's note, which also names it as the sender; what it writes arrives quoted as another
  agent's words with no authority, capped at 600 characters and ten messages an hour. Claude Code, Codex and Cursor read their messages on their next edit and at the
  end of a turn, where they keep going to answer (Cursor as a follow-up message; at most three
  times an hour per sender); Claude Code and Codex also on the next prompt. `recall install`
  adds the hooks. The menu bar shows the conversation under the file.
- **Different lines are safe.** The hooks record which lines each edit changes (Claude
  Code's replaced text, a Codex patch's hunks, the difference Cursor's whole-file write makes),
  and two live chats changing different lines of one working copy get the new `apart`
  situation (default `tell`) instead of `same_copy` (default `ask`).
- **Resolve.** In the menu bar, Resolve on a shared file messages every chat involved to
  agree who goes first, queued for each chat's next edit, turn end or prompt, never
  interrupting it (`recall resolve --repo … --file …` from the command line). The row shows
  when it was sent and clears once the overlap does; pressing it again sends nothing more.
- **Exploration reports.** Claude Code's Explore, Cursor's explore and Codex's explorer
  subagents leave a report, and recall now keeps each one with the commit it saw (the
  subagent-stop hooks; `recall explore backfill` adds the ones in your history). When
  `[explore] serve = true` is set in `config.toml`, a new exploration in that repo is handed the
  earlier report that best matches its question, as notes with the commit, age and files
  changed since, or nothing when none clears the gate. Serving is off by default: replaying 10
  past explorations found no gain in judged quality and about 6% fewer tokens, within the
  noise. Any agent can ask for the report through `project_memory(path, task)` whatever the
  setting. Cursor's preToolUse on the Task tool and Codex's capture follow their app code and
  docs and are not yet tested live; Codex cannot be served automatically, because its
  SubagentStart comes before the subagent's task exists. `recall eval explore` replays your
  past explorations.
- **Memory mid-session.** Hooks can hand an agent a past chat or note at the moment it
  applies: when the prompt closely matches an earlier chat, and when it reads a file (scoped
  rules it does not load itself, notes naming the file, the last chat that talked about it).
  A gate fitted on one developer's history abstains unless it is sure; each item comes once
  per session, 1,200 characters at most per injection; a preference or a past chat tied to
  one context (`[contexts]`) is handed only to that context's sessions, while searching chats
  is not narrowed; hooks send a prompt only to a local embedding model, never a hosted one,
  and read the index without writing to it, staying quiet while an upgrade has it to rebuild.
  Claude Code, Codex and Cursor. Failed commands and compaction are wired too but rarely fire
  until the index keeps tool errors. `recall jit` shows what would fire and why;
  `recall usage` counts what did; `recall eval jit` fits the gate to your history and `--live`
  measures its precision on real injections.

- **Several accounts and installs, connected by itself.** recall now finds every folder an agent
  keeps its settings and chats in: the defaults, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` from the
  environment and from `export` lines, statement-start assignments and aliases in your shell rc files
  (not text that only mentions them), `~/.claude-*` style folders that hold a real config (backups and
  copies of the default are skipped), and Cursor builds such as Nightly. Each gets a stable name, is
  indexed, and every chat records its folder (an added `root` column; the chat index is not rebuilt).
  A folder is written to only after the agent has used it, and never the home folder or a parent.
  `recall doctor` shows each folder as connected, trust pending (Codex), approval pending (Cursor),
  skipped or waiting, with a masked account hint and when it was last indexed. `recall setup` asks
  once, default no, whether to keep recall connected automatically (`[autopilot] connect = true`;
  `--autopilot` answers it, `recall autopilot on|off` changes it): a nightly check and the
  session-start hook then install the MCP server, hooks and skill into folders that are new or have
  lost them, with each file backed up first (the first backup is kept) and one line per change in the
  usage log. The yes covers the hook kinds of the release it was given for; later kinds wait for
  `recall install`. Per root, `[roots.NAME]` sets `mine` (a second account's chats become memory only
  when you opt in), `index` and `connect`. `recall install` and `uninstall` cover every folder found
  (`--root NAME` one), and `uninstall` turns autopilot off and also reaches folders it connected that
  are no longer found. Hooks that run another existing recall (a checkout's `uv run recall` beside the
  installed one) are left on it; `recall install` and `recall doctor` say which recall they run, and
  `recall install --repoint` moves them (autopilot never does). `docs/benchmarks/setup_eval.py`
  checks all of it in a sandbox home.
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
- **Which chat wrote this.** `recall why <file>[:line]` follows code back through git blame to
  the chats that produced it, with dates, the chat's title and what the person asked; `recall
  why --commit <sha>` does the same for a commit. Agents get it through MCP: `recent_chats`
  takes `file` or `commit`. Each chat's edits are matched with the lines later commits add,
  so worktrees, clones, rebases and squash merges are covered, and a chat that ran
  `git commit` (or revert, or cherry-pick) is matched by the sha git printed. The links stay
  in recall's index on your machine: git is only read, with lazy fetching and textconv
  off, and nothing is written to any repo. They are worked out without a model, in the
  nightly run and on each `recall why`; `[links] enabled = false` turns them off and deletes
  them. `recall links --episodes` lists commits that fixed something that had failed. It is
  experimental: a judge found 24% of 21 sampled episodes right (0 of 14 for failed checks and
  commands, 5 of 6 for corrections), so do not build on it yet.

### Changed

- **Archive copies load whatever a later version added to them.** Reading a copy ignores
  keys and message fields it does not know, so a change that adds a field to the archive can
  be reverted without breaking every index run.
- **One hook command.** Every hook recall installs, for every agent, now runs
  `recall hook --agent <agent>`, which hands the event to the features that want it and
  answers in that agent's format. `recall install` replaces the older `context --hook` and
  `status --hook` entries, which keep working until then. CONTRIBUTING.md has how to add one.
- **Session start loads the project's memory, and keeps work and personal apart.** General
  preferences used to fill the whole block before the project's notes began. Each section
  now has its own share of a 4,500-character body (down from 6,000); a project index cut to
  size keeps the best notes of every kind, by title past the first two. `[contexts]` in
  config.toml names each context's folders: the nightly run judges once whether a general
  note holds everywhere or only where it was learned (`reach:` in the note, editable), and a
  note not yet judged loads everywhere, as before. Notes from chats with no folder move to
  their project once every one of those chats resolves to it. `recall eval session` grades
  the block against labelled notes for real folders.

### Fixed

- Text that reaches the user's turn without the person writing it is no longer indexed as
  theirs: Claude Code peer messages and task notifications delivered mid-turn, compaction
  summaries and `!` command output; Cursor's session-hook context and tool catalogues; Codex
  goal continuations, skill bodies, browser state and subagent reports. Memory extraction
  reads user turns as the developer speaking. The index rebuilds once (chat schema 3).
  A chat whose file is gone comes back from the archive with the same cleaning applied to
  its user turns, and Claude Code's compaction summary is dropped from it. What the archive
  cannot undo is a peer message or task notification an older release stored as the
  person's words: Claude Code marks those only in the chat file, so once the file is gone
  they cannot be told from the person's text and stay.
- A chat file deleted while recall was listing files no longer stops the index run.
- **Codex chats Codex had compressed.** Codex turns rollouts a week old into `.jsonl.zst`, and
  recall only looked for `.jsonl`, so most of a long Codex history was neither indexed nor
  readable again. recall now reads both forms: through Python 3.14's built-in zstd, the
  `zstandard` package (new `recall-agents[zstd]` extra) or the `zstd` command. A chat keeps
  one identity whichever form is on disk, so compressing or resuming it changes nothing.
  When none of the three is available, `recall index`, `recall stats` and `recall status` say
  how many Codex chats cannot be read and how to fix it. A damaged file is skipped with an
  error, and a `zstd` that hangs is killed after a minute. Commit links and Explore's history
  read compressed Codex chats, and `recall forget` lists the `.jsonl.zst` that holds one.
  The chat index rebuilds once (chat schema 4).
- **Cursor chats get their folder and their kind.** Cursor records a chat's folder, and
  whether an agent spawned it, on the chat itself; recall now reads both, and places a
  helper chat that recorded neither in its parent's folder, up a chain of nested helpers. On
  one developer's history, Cursor chats a person typed in with no folder fell from 932 to 199,
  and about 720 helper chats (explore and the like) are marked as subagents, so memory
  extraction no longer reads them as the developer speaking. Side chats stay the person's.
  Notes already learned from helper chats stay until edited. The index rebuilds once (chat
  schema 5).

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

# Changelog

All notable changes to recall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.6.1] - 2026-11-12

### Added

- **Agents can hand work to any chat.** `message_chat` reaches any chat on this Mac, in any project.
  An agent finds the chat with `search_chats` or `recent_chats` and sends up to 4,000 characters,
  with a `who` line saying who it is. The message arrives quoted and marked as from another agent,
  and the chat that gets it treats it as a request from a colleague. A closed Claude Code chat or an
  idle Codex thread is woken to read it, resumed headlessly with anything that needs approval
  refused (`[messaging] inherit_permissions = true` keeps your own settings). An open Claude Code
  chat is woken only with `[messaging] wake_open_sessions = true`, because that turn runs with the
  chat's own permissions, and Cursor reads messages on its next turn. Nothing stays running, a
  message counts as delivered only once it is in the chat's transcript, and `wake = false` turns
  waking off. See docs/coordination.md.
- **`recall import` reads a ChatGPT account export too.** Point it at the ZIP from Settings > Data
  controls > Export data and your ChatGPT chats become searchable from every agent, labelled
  `chatgpt`. Only the final version of an edited or regenerated chat is kept. The Mac app has an
  Import chats panel for both services, and agents can do it when you tell them where the file is.
- **`recall import` reads a claude.ai account export.** Point it at the ZIP from Settings > Privacy
  > Export data and your claude.ai chats, including those from the desktop app's Chat tab, become
  searchable from every agent, labelled `claudeai`. A later export updates what changed, and `recall
  forget` removes a chat for good.
- **Claude Cowork sessions are indexed.** recall now reads the local Cowork sessions in the Claude
  desktop app, with each session's title and the folder you picked. They are searchable from every
  agent, labelled `cowork`, and stay out of the nightly notes unless `[roots.cowork] mine = true`.
- **A memory run that fails now says so.** Each run is recorded when it starts and closed however it
  ends, `recall doctor` reports a failed, crashed or overdue run, and the Mac app shows it. One
  malformed note no longer stops every note from loading; `recall doctor` names it.

### Changed

- **Forked chats show once in search.** A Claude Code, Codex or Cursor chat that copies another
  chat's history (a fork or resume) no longer takes a second result slot: the best-ranked copy
  stands for the group and lists the others as `copies`, which `read_chat` still opens.
- **Agent eval grading takes the majority of three votes.** `recall eval agent --v2` now grades each
  reply three times and the majority decides, so one judge call can't settle a borderline verdict
  alone. Re-grading 1,275 stored replies changed 2.7% of verdicts and left the same-day gaps in
  place. Results record `judge_version` as `qa-judge-v2+maj3`, so they never compare with
  single-vote scores; `--judge-votes 1` is the old judge. `longmemeval --qa` takes the same flag
  (default 1), and `eval agent --v2 --rejudge RUN_DIR --out FILE` grades the stored replies of an
  earlier run again without running any agent.

### Fixed

- **A chat left open for days can no longer undo a newer rule with something it said early on.**
  Candidate memories are dated by the message they came from, and one whose quote no message holds
  counts from the chat's start when it would change an existing rule.
- **Deleted notes stay deleted.** A rule or note you deleted is refused in code as well as in the
  prompt.
- **Hooks work when recall is installed under a path with a space.** `recall install` wrote the hook
  command unquoted, so from a folder such as `~/Library/Application Support/pipx` every hook failed
  in the agent's shell, a second install added the entries again, and `recall uninstall` left them
  behind. The path is now quoted, and the next `recall install` (or autopilot) repairs the entries
  an earlier release wrote.
- **Notes commits fail loudly, and the deletion ledger survives a crash.** A new note cannot take a
  deleted or archived note's id, the deletion ledger is written in one step and comes back from git
  if it is ever cut short, a note that stops parsing no longer counts as deleted, and a leftover git
  lock in the notes folder is cleared or reported instead of silently stopping every commit.
- **`recall remember` and edits from the app no longer wait for a memory run.** The run holds the
  notes only while it writes, not while a model answers, and it no longer writes without the lock.
  Notes written in a run are judged for where they apply in that run, before the indexes are
  written. The time budget now also stops a run that does not use a batch API.
- **Contexts no longer leak through unjudged or merged notes.** A global note the nightly run has
  not judged stays in the contexts it was learned in. A note judged to hold everywhere is judged
  again when it gains new words, and a note holding rules from several contexts shows each context
  only the rules said there.
- **An edit the hook refused no longer counts as an edit.** The chat that really has the changes is
  no longer stopped because of a chat that never touched the file.
- **`recall uninstall` also removes the nightly memory run.** It used to leave the job scheduled, so
  chats kept going to the model every night until `recall schedule disable`. `--only` and `--root`
  still leave it alone, and `recall schedule enable` brings it back.

## [0.6.0] - 2026-11-08

Most of this release is the engine work behind recall desktop, the new Mac app: `recall api` and
the changes an app may make through it. The app's own changes are in
[its changelog](https://github.com/alirng/recall/blob/main/apps/menubar/CHANGELOG.md).

### Added

- **`recall api` makes the few changes an app may make.** New endpoints edit a note (`PUT
  /v1/notes/{id}`, with `POST …/preview` to see what saving would record), forget one and put it
  back (`POST …/forget`, `POST …/restore`), set the coordination rules (`PUT /v1/coordination`),
  list the models that work here and switch to one after a test call (`GET /v1/models`, `PUT
  /v1/model`), and remove copies kept before an upgrade (`POST /v1/storage/remove`). Each runs
  recall's own code and takes the same notes lock as a memory run, answering `409 busy` while one
  runs and `409 conflict` when the note's file changed since the client read it. `/v1/info` lists
  them under `writes`, and `/v1/notes/{id}` gains `stamp`. Started by an app or launchd, `recall
  api` adds the nightly run's folders to its `PATH`, so it finds Claude Code, Codex and local model
  servers. See docs/api.md.
- **`recall api`:** an HTTP and JSON door to your chats, notes, memory runs and status, on a local
  unix socket (mode 0600, no network port), with live updates as server-sent events. It starts on
  demand and exits after five idle minutes, and an open event stream costs no CPU while nothing
  changes. Apps use it instead of opening the index. See docs/api.md.
  `/v1/status` takes `?fresh=1` to skip its 15-second cache.

### Changed

- **Benchmark figures show 0.5.2.** The README, [docs/benchmarks.md](docs/benchmarks.md) and the
  charts now quote what 0.5.2 measured on 3 and 4 November beside 0.5.1: search and the other
  model-free results are unchanged, and the agent evals sit within run noise of 0.5.1 apart from a
  dip on my own questions about past work, which the page shows as one. `store.py record` gains
  `--home`, naming the eval index a run's throwaway copy was made from.

### Fixed

- **A config.toml key with a space no longer breaks the file.** Saving settings quotes every key,
  writes nested tables properly, checks the text reads back as what was saved before replacing the
  file (and refuses otherwise), and creates a new config.toml readable only by you.
- **`recall api` no longer repeats an API key in an error, and hides crash details.** An unknown key
  reference is reported without its text, every error message goes through the credential redaction
  the indexer uses, and an unexpected failure answers with a generic message while the detail goes
  to api.log.
- **Switching from a private model to one that sends excerpts away needs your consent.** `PUT
  /v1/model` answers `409 needs_consent` with the destination unless the body repeats it as
  `accept_destination`.
- **Edits and forgets must carry the note's stamp**, and a forgotten chat no longer shows in the
  API's chat list, messages, search results or counts. One malformed note no longer breaks every
  notes endpoint: it is skipped and logged. Restore only puts back a note that was forgotten, `PUT
  /v1/coordination` answers 400 for values of the wrong type, and `recall api` refuses to replace a
  file or link at its socket path.
- **config.toml is written in one step**, a temp file then a rename, so a hook reading the settings
  while they're saved sees the old file or the new one, never half of it.
- **An API key at its spend limit now falls back.** Anthropic answers a key past its monthly spend
  limit with a 400 ("You have reached your specified API usage limits"), which recall took for a
  broken setup rather than an empty account. A nightly run with `[model] fallback` set never
  switched to it, marked every chat failed, and logged "read 19 chats · 0 candidate memories". That
  reply now counts as out of credits: the run carries on with the fallback (or stops cleanly without
  one), and a batch an earlier run left running is kept for the next run rather than marked lost.
  The read step now logs why its calls failed and counts failed chats apart from read ones (`failed`
  in the run's counts), and Anthropic errors are logged in the API's own words rather than as the
  SDK's dump of the reply.

## [0.5.2] - 2026-11-04

### Upgrading

- **Run `recall install` after upgrading.** It refreshes the shared `recall` skill, which gains the
  `remember` step (`/recall remember <rule>` in Claude Code and Cursor, `$recall remember <rule>` in
  Codex), and registers recall's MCP server with any of the newly supported agents you have. The
  first index run after the upgrade then rebuilds the chat tables once, because the chat schema
  changed: about three minutes on a 4,600-chat index with a large Cursor history. Runs after that
  take their usual fraction of a second.

### Added

- **BEAM by ability.** `recall eval beam` takes `--abilities`, a comma-separated list such as
  `event_ordering,summarization`, and answers only those questions, so a run aimed at the weak
  abilities costs a fraction of a full split. It also honours `--workers` now. The result records
  which abilities it ran.
- **Claude Code subagents are searchable.** recall indexes each subagent's transcript as its own
  chat: the brief its parent agent wrote and the report it sent back, a workflow subagent's
  structured result included. Like Codex and Cursor subagents, they are never mined for memory.
  `recall forget` on a Claude Code chat forgets its subagents too, and an index run skips the
  subagents of a chat already forgotten. In search, a subagent takes a place above another chat only
  by matching better, and each further subagent of the same parent chat counts for half, so one
  workflow's dozens of subagents do not fill the results; `recent_chats` leaves subagents out.
- **Query profiles in the search evals.** `recall eval search --v2`, `recall eval longmemeval` and
  `recall eval swe-contextbench` take `--profiles`, which labels each case P1 literal, P2 concept or
  P3 conjunction by written rules over the index (no model; see `evals_profiles.py`) and adds MRR
  and Recall@R per profile next to the usual scores, which stay as they were.
- **Grok Bot chats (opt-in).** `recall setup` asks, with a default of no, whether to read the chat
  cache Grok Bot (Cursor's personal-agent app) keeps on the Mac. Yes reads it read-only, as one chat
  per bot per day, so a bot's older messages stay in recall's archive as the app's 200-entry window
  slides; skips the bots you name, and any bot you deleted in the app; and puts the app's folder in
  your `personal` context, so sessions in other contexts are never handed these chats. `recall
  dream` leaves them out until you set `[roots.grokbot] mine = true`. `recall search`, `recent`,
  `show` and `memory search` take `--context NAME`, which limits what comes back to one context from
  any folder, for an agent such as a Grok Bot that must see only that part of your life. More in
  docs/grok-bot.md.
- **Outcome ledger, logging first.** Session-start deliveries and the `project_memory` tool
  (Cursor's session start) now log the agent's session id and the notes and rules they carried, as
  `note/rule@version`; mid-session injections log the rules of the notes they carried; and MCP tool
  calls log the agent's session id where the agent exposes one (Claude Code and, by its tool-call
  metadata, Codex), instead of recall's process number. `recall ledger` joins those deliveries to
  what the index shows next in the same session (a tool failure within 15 minutes, a linked commit
  within a day, a commit touching the file whose read triggered the delivery, the person correcting
  the agent), per channel and per note. It only reads, changes nothing an agent sees, and an older
  release reads the same `usage.jsonl`.
- **More coding agents: compatible support.** recall now indexes the chat history of OpenCode, Kilo
  Code, Antigravity, Gemini CLI, Qwen Code, Pi and its forks, GitHub Copilot CLI, Factory Droid,
  Cline, Roo Code, Goose, Crush and more (the list and each agent's support level are in
  `docs/agents.md`), so `search_chats` and `read_chat` find those chats under the agent's name and
  folder. `recall install` also registers recall's MCP server in the config file each of these
  agents documents, with a backup first and a restore on `recall uninstall`. Compatible means
  history and MCP only: no session-start hooks, briefs or notes for these agents, and their chats
  stay out of the nightly notes unless you set `mine = true` for the agent under `[roots.<name>]`.
  Adding these sources changes the chat schema, so the chat tables rebuild from the agents' files
  once after the upgrade (about three minutes on a 4,600-chat index with a large Cursor history; an
  index with nothing new takes about the same 0.3 seconds as before).
- **`recall remember "<rule>"` saves a rule you state, at once.** It goes straight into the notes as
  a note of its own, for the current project, or with `--global` for every project in the folder's
  `[contexts]` context (a work rule never loads in personal folders), and the next session starts
  with it. Agents run it through the `recall` skill when you type `/recall remember <rule>` (Claude
  Code, Cursor) or `$recall remember <rule>` (Codex); the MCP tools stay read-only. A rule is marked
  `said_by: developer` only when you typed it at your own terminal, or the agent's `--quote` is
  found in your latest message in that chat's own transcript. Anything else an agent saves is its
  candidate, which no session is shown unasked, and an agent cannot save a rule with a command, a
  URL or a destructive step at all. The nightly run keeps your words: it retires the older rules a
  remembered rule replaces and folds in duplicates, and only a later chat in which you typed
  something else changes it, never an older chat or the agent. `recall eval memory` gains a `gold`
  column for this, scored without a judge on cases that remember rules between chats.
- **all@8 in the search eval.** `recall eval search --v2` now reports the share of cases with two or
  more expected chats where every one of them is in the top 8, overall and per tag, next to hit@5
  and mrr. It is deterministic and model-free, and works with `--cases` for a custom case file.

### Changed

- **Evals: a BEAM adapter, and a judge that is not the reader.** `recall eval beam` runs BEAM's long
  conversations, checked on the 100K split (it needs pyarrow, e.g. `uv run --with pyarrow`). `recall
  eval longmemeval --qa` and `recall eval beam` now grade with a fixed Claude Opus 5.5 unless
  `--judge` names another model, refuse a judge that is the reader (before, the reader graded its
  own answers), record a question the model refuses or fails as an error row instead of ending the
  run, and put the judge's name in the ledger line. The benchmark tables carry corrected competitor
  figures (Hindsight 94.6, Zep 90.2, Mastra counted per question as 93.6), name the denominator of
  the 98.7% retrieval figure, split the tool definitions out of the tokens per task, and name the
  MemoryCode figures of record. No product behaviour changes.
- PyPI page: Homepage now points at rererecall.com, with Source, Documentation and the site's
  changelog listed beside it.
- **Lighter background use:** the session-start hook no longer re-parses your shell rc files every
  time (about 40 ms saved on every session start; they are read again only when they change), and
  the menu bar app refreshes every 15 minutes instead of every 5, at lower priority. Each refresh is
  a `recall status` run that costs about 1.5 CPU seconds.

### Fixed

- **The edit hook stops agents only for a clash that is happening now.** It used to stop or warn
  whenever another chat had open changes to the file: in the same working copy if that chat had
  written anything in the last 15 minutes, and across branches at any age, so an orchestrator's
  sub-sessions, a chat that had finished, and a branch merged days ago all raised it. Now the other
  chat must have written in the last 5 minutes and edited that file in the last 15 (30 and 60 across
  branches); anything older is quiet, and a chat whose last message is outside the hook's window is
  ignored. The four `[coordination]` rules are unchanged.
- Menu bar: the Restart rows name how many stale recall servers an agent has and when the oldest and
  newest started, instead of listing every start time. Working now is a git log again: newest chat
  at the top, main as the trunk even when no chat is on it, and each branch curving into the trunk
  below its oldest chat; before, a repo with no chat on main drew a dangling stub above every
  branch.
- **The menu bar's "Needs you" lists only clashes happening now.** It listed every file open in two
  chats, including chats that stopped a day or a week ago, leftover uncommitted changes and clean
  merges between branches. `recall status` now uses the same test as the edit hook (both chats at
  the file in the last 5 to 15 minutes, or 30 to 60 across branches) and drops chats whose last
  message is outside the window, instead of going by when the transcript file changed. The rest are
  under `quiet` in `--json`. The panel logs each open and each Resolve or Show changes press to
  `usage.jsonl` (the action and the time only; `recall menubar-use`), so a later look at whether it
  earns its place has numbers.

## [0.5.1] - 2026-10-17

### Added

- **Benchmark figures live in one store.** Every figure in the README, `docs/benchmarks.md` and on
  rererecall.com, and every other system's figure they compare against, is a row of
  `docs/benchmarks/results.jsonl`: a benchmark and metric from `docs/benchmarks/registry.json`, the
  value with its interval, repeats and date, the code it measured (release, commit and product-code
  hash), the model, judge, harness and conditions, and the run folders it came from. Rows are
  appended and never edited. `uv run python docs/benchmarks/store.py record RUN_DIR` turns a
  finished run into rows, and `check` validates the store in CI. The charts draw from the store
  through `docs/benchmarks/charts.json`, and `docs/benchmarks/figures.json` replaces `results.json`
  for anyone drawing the same figures. See `docs/benchmarks/README.md`.

### Changed

- **Benchmarks re-checked for 0.5.0.** On the v0.5.0 tag, search and retrieval give 0.4.1's figures
  at every cutoff. Run side by side with 0.4.2 on the same day, three runs each, agents do as well
  (78.7% of my questions about past work against 79.4%; MemoryCode with notes at session start 85.5%
  against 85.0%), while a call to recall's tools takes 65 ms instead of 258 ms at the median and
  none failed. The figures are in docs/benchmarks.md and docs/benchmarks/results.jsonl.
- **The README shows the release and 0.5.0's speed.** A PyPI badge gives the current version, the
  SWE-ContextBench chart carries 0.5.0's label (its re-check gave the same figures), and the results
  add that a call to recall's tools takes 65 ms at the median, against 258 ms in 0.4.2, with agents
  as accurate.

## [0.5.0] - 2026-10-16

### Upgrading

- **Codex now asks before `message_chat` runs.** `message_chat` writes a message to another chat's
  mailbox, and Codex's default `auto` approval let it through without asking, because the tool is
  marked neither destructive nor open-world. Run `recall install` to add the approval to your
  existing Codex entry (`recall doctor` says when an entry lacks it); an approval you set yourself
  is left alone, and `recall uninstall` removes it with the entry.
- **The chat index rebuilds itself once more.** The first index run after upgrading re-reads your
  chats, and Cursor's database in a background run after that. On Linux, Cursor chats that had
  failed to index because Linux keeps no file creation time come back.
- **The notes folder's git history still holds the start of every note's body.** Earlier releases
  kept it in the deletion ledger, `.recall/notes.json`, which each memory run commits. The first
  memory run on this release rewrites the ledger without bodies; once it has (no note text left in
  that file), this removes the old versions from every commit, needs git-filter-repo, and removes
  the folder's git remote, so add it back if you sync the notes: `cd ~/.agents/memory && git add -A
  && git commit -q --allow-empty -m "Before purging the ledger's history" && cp .recall/notes.json
  ../recall/notes-ledger.json && git filter-repo --force --invert-paths --path .recall/notes.json &&
  mkdir -p .recall && mv ../recall/notes-ledger.json .recall/notes.json && git add -A && git commit
  -qm "Deletion ledger without note bodies"`.

### Changed

- **recall waits for power on battery.** `recall dream` defers itself when it starts on battery
  outside a terminal, the nightly run included (launchd runs a missed night when the Mac wakes), and
  the first tool call or session start on power runs it in the background. Tool calls start no
  background indexing or embedding on battery, and the jobs recall starts in the background run at
  low priority. `recall status` and `recall doctor` say when a run was deferred. `recall dream
  --on-battery run` runs one anyway, and `on_battery = "run"` under `[power]` in config.toml lets
  everything run on battery.
- **recall's hooks and tools answer faster.** On a history of 1,200 chats, the edit hook took 80 ms,
  175 ms while other chats were working, and about 0.8 s with its cache gone, because before every
  edit it listed every agent's transcripts and read again in full each chat that had moved in the
  last 12 hours. Now it reads only what each recent transcript gained since its last look, and reads
  chats in full and asks git only when another chat touched the same file: 40 ms, and 150 ms cold. A
  chat that starts without recall's hooks (Codex before you trust them) is still seen at the next
  edit, from the folder its agent writes it to. One resumed in a transcript older than a day is seen
  within five minutes. When another chat has the file open, the git calls run side by side and skip
  macOS's `/usr/bin/git` stub (380 ms down to 150 ms). A prompt no longer waits on an Ollama model
  that is not loaded. Asking one started a load that Ollama dropped when the hook gave up, so every
  prompt waited a quarter of a second and the model never loaded. Now a background process loads it,
  at most once every 25 minutes, and a loaded model is waited for at most 100 ms rather than 250.
  Hooks that do not read the index load 15 ms less code. The MCP server reads the shell's rc files
  once per index refresh rather than three times, memory notes load eight times faster, and a tool
  call waits for an index refresh only until it is a second old. A longer one, such as the rebuild
  after an update, carries on in the background. Until it is done, `search_chats`, `recent_chats`
  and `read_chat` say that chats may be missing. Refreshes commit after each chat they read, and the
  redaction scrub every half second, so a tool call that writes no longer waits tens of seconds for
  the index. One that would wait over 3 s answers from the notes or commit links as they stand.
- **Semantic search keeps one copy of the vectors, not one per agent.** Each agent session runs its
  own recall MCP server, and with embeddings on, each built a matrix of every message vector on its
  first search and kept it: over 100 MB a server for 23k messages, more than 1 GB with ten agents
  open. The vectors now sit in one file beside the index (`recall-vectors/`) that a server maps only
  while it searches, so the operating system holds one copy for all of them. Five servers after two
  searches each take 428 MB instead of 830 MB, and search ranks as before: search_v2 and SWE-CB were
  identical on a same-day paired run. `recall forget` rewrites the file without the forgotten chats.
  `recall embed` no longer runs twice at once and stops after 20 minutes (the next run carries on),
  finding the messages to embed reads only the chats that changed, and a search waits at most 10
  seconds for the embedding model before going by words.

### Fixed

- **One chat recall cannot index no longer breaks its tools.** A chat that failed to index made
  every tool call fail and kept the chats after it out of the index, and a damaged archive copy did
  the same. Such a chat is now logged by its path, never its words, and tried again after a minute,
  ten minutes and an hour, then left until its file changes, while the others index as usual.
  `recall doctor` lists these chats and `recall stats` counts them. A damaged archive copy is
  replaced by the next read of its file, and can no longer stop recall from opening the index after
  an update.
- **Tool calls no longer rescan Cursor's database every 30 minutes.** Cursor's older chats, and its
  subagent and side chats, sit in a database of several GB. recall now reads it in full in the
  nightly run and on `recall index`, and from a tool call only on the first index or a day after the
  last full read (in the background, at low priority, stopped after 15 minutes), and skips it while
  the database is unchanged. Other chats are still read as soon as they change. A new Cursor side
  chat becomes searchable after the next nightly run or `recall index`.
- **Memory that reaches an agent unasked no longer carries commands, links or other chats'
  prompts.** A README or web page an agent read could become a note that session start injected into
  every later session. Now a rule that gives an agent something to act on (a URL or host, a command
  to run, a check switched off, private data sent somewhere) never loads at session start or
  mid-session, and neither does a note with such a title; a prohibition such as "never force-push
  main" still loads, and memory_search and read_note still find everything. A note whose rules only
  the agent stated is counted, not named, and rules a user turn stated without a typed quote are now
  marked `unverified`, so an unmarked rule means one from before speakers were recorded. Injected
  text keeps no `<` or `>` (each becomes ‹ or ›), so no note, rule, past chat or exploration report
  can open or close the block, and titles are one line of at most 120 characters. On my chats,
  session start loads 0.574 of the notes it must (was 0.576), 0.482 on held-out folders (was 0.488),
  holding back 13 of 733 rules. `recall status` redacts a chat's title and latest request before
  caching them or quoting them to other chats, and its cache rebuilds once. The deletion ledger
  (`.recall/notes.json`) kept the start of every note's body, and consolidation sent 200 characters
  of each deleted note to the model: it now keeps the title, scope and project, existing ledgers are
  rewritten without bodies, and a deleted note restored on disk leaves it. The history rewrite
  `recall forget` prints now commits the notes folder first (filter-repo's reset would discard
  uncommitted hand edits), deletes the listed notes from disk and history, also removes the ledger's
  past versions and puts the current one back, and passes `--force`, without which filter-repo
  refused to run.
- **memory_search no longer rebuilds the notes index on every call.** It used to drop and rebuild
  the notes tables and load every note twice per search, which took the index's write lock while
  other agents' servers were reading, so a search elsewhere could fail with a transient `vtable
  constructor failed` error. It now rebuilds only when a note file, the `[contexts]` in config.toml,
  or the tables' layout differs from what the index was built from, so a hand edit still shows up on
  the next search, and a rebuild is one transaction. With 733 notes and embeddings on, a search
  takes about 35 ms through the MCP server, against about 530 ms in 0.4.2. Servers still running an
  older release keep rebuilding the tables until their agent restarts.
- **A model server on another machine is no longer called private.** A local model whose `base_url`
  is not `localhost` or a loopback address used to be reported as "nothing leaves this machine".
  `recall dream` no longer says so, and setup names the host the chat excerpts go to.
- **SECURITY.md and the README say what recall does.** They said all six MCP tools only read. There
  are nine, and `message_chat` writes to recall's mailbox. SECURITY.md now also lists every file
  `recall install` writes, what the hooks can do (refuse an edit once, keep an agent going, and
  rewrite a subagent's prompt if you turn that on), what a hosted embedding endpoint receives, and
  that recall keeps its copy of a chat after an agent deletes it, even past a short
  `cleanupPeriodDays` you set yourself.
- **Time windows as agents write them.** `since` and `until` accept `2mo`, `3 months`, `2wk`, `5
  days`, `4 hours`, `1yr` and the like, not just `12h`, `3d`, `2w`, `1m` or `1y`. A window recall
  cannot read now says which forms it takes, instead of failing with a bare date error.

## [0.4.2] - 2026-10-16

recall's tools stop failing with `database is locked`. Search results and the chat index are
unchanged.

### Upgrading

- **Restart the agents that were open when you upgraded.** Their recall MCP server still runs the
  release it started with, and keeps failing until the agent restarts. `recall status` lists them.

### Changed

- **Benchmarks re-measured for 0.4.1 and 0.4.2.** Search scores on every public benchmark are
  unchanged. Notes written by Claude Haiku 5.5 hold 90.6% of MemoryCode's conventions in their
  latest form, and loaded at session start they lift an agent on long histories from 32% to 75%.
  Agents take in about as many tokens per question as with 0.3.0 run the same day, plus the
  definitions of the tools 0.4.0 added. LongMemEval accuracy is 88.4%. On my own chats, gpt-6-luna's
  notes leaned ahead of Haiku's, within noise. Agent results are compared within a day, because the
  model's behaviour shifted between 12 and 16 October. `docs/benchmarks/agent_tokens.py` now fits
  one factor over any number of releases.

### Fixed

- **recall's tools no longer fail with `database is locked`.** Since 0.4.0, on a machine with more
  than one Cursor build, every index refresh opened a write that nothing committed, and an agent's
  recall MCP server kept it open after its first tool call. Its later calls, and other agents' and
  hooks', waited 30 seconds for the index and failed. In the eval of questions about past work, a
  third of recall's tool calls failed this way. A refresh with nothing to write now writes nothing,
  a refresh commits what it wrote, and the chat and note tools close their connection to the index
  at the end of each call.

## [0.4.1] - 2026-10-16

The nightly memory run survives an upgrade, and `recall status` says which agents to restart after
one. Search results are unchanged.

### Upgrading

- **The chat index rebuilds itself once more.** The chat schema is no longer one number every parser
  change bumps (it was 7): each module that shapes the index declares its own `CHAT_SCHEMA` string,
  and the schema is a hash of them all, shown as `p-` and ten hex digits. The first index run after
  upgrading re-reads your chats; chats whose files are gone come back from the archive. Later
  rebuilds happen only when a release changes one of those strings.
- **Restart the agents that were open when you upgraded.** Their recall MCP server still runs 0.4.0,
  which rebuilds the chat index back to schema 7 whenever it is called. `recall status` lists them.

### Added

- **`recall status` names the agents to restart after an upgrade.** An agent's recall MCP server
  keeps the release it started with until the agent restarts. `recall status` now lists each server
  that started before recall was last installed into its environment, with the agent that started it
  (and, for Claude Code, the chat), and `--json` carries them as `outlived`. The menu bar shows them
  under Needs you.

### Changed

- **Contributors:** a PR adds a `changes/<topic>.<section>.md` fragment instead of editing
  CHANGELOG.md, and `tools/changelog.py --release` compiles them at release. No change for users.

### Fixed

- **The nightly memory run no longer crashes when an older recall rebuilds the chat index under
  it.** An agent's MCP server keeps the release it started with until the agent restarts, so after
  an upgrade it rebuilt the chat tables back to its own schema, and a run reading them stopped with
  `no such column: s.root`. Now a recall process that an upgrade has outlived leaves the index alone
  and asks for the agent to be restarted, and a nightly run that finds the chat tables changed under
  it leaves the chats for the next run and records why. After upgrading, restart agents that were
  already open.

## [0.4.0] - 2026-10-15

recall can now tell you which chat wrote a line of code, keeps every chat it has indexed, tells
agents about each other's edits, and writes notes as rules it checks one by one. Search results
are unchanged. Numbers: docs/benchmarks.md.

### Upgrading

- **The chat index rebuilds itself once** (chat schema 7), and so does the redaction of stored
  text: the first index run after upgrading re-reads your chats and rewrites the text recall
  cannot rebuild from your agents' files (cached answers, extracted candidates, archived
  chats) with the current rules. Chats whose files are gone come back from the archive.
- **Memory notes gain a `rules:` block** in their front matter the next time recall writes
  them. An older recall that rewrites such a note drops that block, so upgrade every machine
  that shares the notes folder.
- **The Claude provider's `haiku`, `sonnet` and `opus` now run the 5.5 models.** Each call uses
  more of your plan than Haiku 4.5 did.
- **With an API key, the nightly run sends batched calls** at half the price and stops at a
  default budget of $1 a run (`[dream] max_usd`). With a ChatGPT or Claude subscription or a
  local model, nothing changes in cost: batches and the dollar budget do not apply.

### Added

- **Batch APIs for the nightly run.** With an Anthropic or OpenAI key, `recall dream` sends its
  calls through the provider's batch API, at half the price: extraction as one batch, then
  consolidation in rounds, every project side by side. A batch that overruns 20 minutes is
  cancelled and the night carries on synchronously; one still running at the time budget is
  collected by the next run. `batch = "off"` in `[model]`, or `recall dream --no-batch`, turns
  it off. Plans (ChatGPT, Claude) and local models keep their usual path.
- **A dollar budget.** `[dream] max_usd` (default $1.00, or `--max-usd`) caps what a run may
  spend on an API key, in place of the call cap. `recall dream` reports each night's cost, and
  `llm_calls` records each call's.
- **When API credits run out**, a run carries on with `[model] fallback` (e.g. `"claude:haiku"`,
  your plan through Claude Code) or stops cleanly and continues the next night. `recall setup`
  mentions that Claude Max and Team plans include monthly API credits.
- **A second model for consolidation**: `[model] consolidate = "anthropic:claude-sonnet-5-5"`,
  or `--model extract-spec+consolidate-spec` for one run. A `--model` spec can name an effort
  after `@` for ChatGPT and Anthropic models.
- **recall never loses a chat it has indexed**, except one you forget and one past a retention
  period your organisation sets. Every chat is kept compressed in its own
  table, one copy per file it came from, which a rebuild never drops. When the parser changes,
  chats are re-read from their files, and those whose files are gone (Claude Code deletes
  transcripts after 30 days by default) come back from the archive, marked restored. When a
  re-read finds fewer messages than recall had (Codex compaction has rewritten session files),
  the missing ones are kept. A chat whose copies live in several files (Cursor writes one per
  project folder, Codex one in `sessions/` and one in `archived_sessions/`) now shows every
  message from all of them, where before only the file read last counted.
- **Past tool failures are searchable.** When a tool call fails (Claude Code's `is_error`
  results, Codex commands that exit non-zero, Cursor tool calls marked failed or terminal
  commands that exit non-zero), recall keeps one redacted line saying what went wrong, with
  the tool and the head of its command, in a `tool_errors` table with its own full-text
  index. Each line also records the message it came after, whether a later identical call in
  that chat succeeded (a retry, not proof of a fix) and the start of what the agent said next.
  Text is redacted whole before it is cut, with extra rules for commands (`-u user:pass`,
  `--password x`, `DB_PASS=x`, webhook URLs). A call the person declined is not a failure.
  It never enters the chat messages, so search ranking and memory extraction are unchanged.
  `store.tool_errors(con, text)` finds the chats where an error came up before.
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
  A gate fitted on one real history abstains unless it is sure; each item comes once
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

- **The Claude provider's `haiku`, `sonnet` and `opus` now run the 5.5 models.** Claude Code's
  own aliases had resolved `haiku` to Haiku 4.5, so `claude:haiku` (the setup default for the
  Claude plan) ran a model a generation older than its name. recall now passes
  `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`, so an existing `name = "haiku"`
  needs no edit, and a full id still passes through unchanged. The label and the response
  cache follow the pinned id. When Claude Code serves another model than the one asked for
  (an old install, or a plan that serves something else), recall says so once on stderr.
  Haiku 5.5 thinks before it answers, so each call uses more of the plan than Haiku 4.5 did.
  On MemoryCode, notes written through Claude Code held 97.6% of the facts with the full model
  id (one run), against 80.9% with the old alias.
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
- **The MCP server reports recall's version** when an agent connects; it reported none.
- **A shorter README.** It now covers what recall does, how to install it and the headline
  results; the detail moved into pages under `docs/` (benchmarks, how it works, projects and
  rules, coordination, commit links, exploration reports, accounts, reference). Figures were
  rechecked against their runs: the MemoryCode agent result now comes from the runs that load
  recall's tools as Claude Code does by default, and search times from the dedicated timing run.
- **Memory notes hold rules, and consolidation changes them one at a time.** A note is a
  title and a list of rules, each with an id, the dates and chats that stated it, and a quote.
  The model answers with operations on named rules (confirm, refine, retire, add, create), and
  code checks each one: a rule leaves a note only through a retire that names it, a chat older
  than a rule's last sighting cannot retire or refine it (or one older than its retirement add
  it back), a note you edited mid-run is not overwritten, and nothing is written until every
  operation in the batch is checked. Consolidation prompt v3. The note's body is rendered from
  its active rules, and a hand edit of the body is read back into them: a rule whose words
  still stand keeps its id and sources, and a rule you delete is remembered as removed by hand
  even when its words sit inside a longer rule. A rule carries `said_by: developer` only when
  the developer typed a quote that supports that rule outside any paste and the rule asks for
  no command, URL, network, destructive or exfiltrating action, or to switch off a sandbox,
  permission or review; otherwise it is unset. It is a hint, not authentication, and no reader
  shows it yet. Notes written this way still read as plain notes in an older release, which
  drops the rule fields if it rewrites the note. On a replay of the same extracted facts
  (MemoryCode, 40 held-out histories), facts lost when notes merge fell from 12.5% to 8.1%,
  recall rose from 70.5% to 75.3% and stale notes fell from 9.3% to 4.8%.

### Fixed

- The PyPI page shows the logo, the charts and working links: the build points the README's
  relative paths at GitHub.
- Text that reaches the user's turn without the person writing it is no longer indexed as
  theirs: Claude Code peer messages and task notifications delivered mid-turn, compaction
  summaries and `!` command output; Cursor's session-hook context and tool catalogues; Codex
  goal continuations, skill bodies, browser state and subagent reports. Memory extraction
  reads user turns as the developer speaking.
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
- **Cursor chats get their folder and their kind.** Cursor records a chat's folder, and
  whether an agent spawned it, on the chat itself; recall now reads both, and places a
  helper chat that recorded neither in its parent's folder, up a chain of nested helpers. On
  one real history, Cursor chats a person typed in with no folder fell from 932 to 199,
  and about 720 helper chats (explore and the like) are marked as subagents, so memory
  extraction no longer reads them as the developer speaking. Side chats stay the person's.
  Notes already learned from helper chats stay until edited.
- **Secrets the redactor missed.** A key written as `"apiKey": "..."`, `**Client Secret**: ...`,
  `API Key: ...` or `use this api key ...`, in a URL query (`?apiKey=...`, `%3D`, `&amp;`), as
  `SOME_NAME_KEY=...`, or in a shape of its own (Azure AD client secrets, `sk_` keys, `fc-` keys,
  `Bearer` and `Basic` tokens, `user:password@`, `:password@` and `token@` URLs, Slack and
  Discord webhook URLs, `Cookie:` values, GitLab deploy, runner and trigger tokens, PyPI,
  Docker Hub and Google OAuth client secrets, private keys cut off before their END line) went
  into the index as written. Each is now redacted, and so are session titles and a key that
  straddled the cut at 20,000 characters. A key pasted again further down the same chat, with
  no word beside it, goes too, in the chat's opening prompt and in the lines kept for failed
  tool calls as well (they use these rules, plus the command rules that were written for them,
  now in the same file and covered by the same version hash). A hash, UUID or commit id is
  still left alone unless a word like "api key" sits next to it, and so is a path
  (`SSH_KEY=~/.ssh/id_ed25519`). Text recall
  cannot rebuild from your agents' files (extracted candidates, cached model answers, error
  messages, failed tool calls, exploration reports, archived chats) is rewritten with the
  current rules at the start of an index run (`chats/scrub.py`), once per version of the rules
  (a hash of them) and again after every rebuild; a restored archive copy, and the failed tool
  calls it carries, are redacted again as they are read. Your notes are not rewritten: the same
  run names the notes, and the entries of `.recall/notes.json`, that the rules match, on stderr
  and in `logs/redact.log`. The chat

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

[Unreleased]: https://github.com/alirng/recall/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/alirng/recall/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/alirng/recall/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/alirng/recall/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/alirng/recall/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/alirng/recall/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/alirng/recall/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/alirng/recall/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/alirng/recall/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/alirng/recall/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/alirng/recall/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/alirng/recall/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/alirng/recall/releases/tag/v0.1.0

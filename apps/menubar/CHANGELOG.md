# Recall changelog

Recall is the Mac app. It has its own version and runs on recall, the engine, which keeps
[its own changelog](https://github.com/alirng/recall/blob/main/CHANGELOG.md).

## [0.1.0] - Unreleased

Powered by recall 0.6.0.

### Added

- **The recall window.** Open recall (⌘O) in the menu bar app opens one window with two views.
  Memory has the notes list with category tags, filters, a project picker with pins and sort, and a
  reader with each note's rules, the chats it was learned from, related notes and its history. This
  Mac has counts, sources, the last memory run as a waterfall, storage, model and schedule,
  coordination rules and access. Chats (messages shown as light markdown, with find in chat),
  Projects and Now are plain lists, and ⌘K opens a palette that searches notes and chats and goes to
  any view. Sections switch on the next frame, lists move with the arrow keys, and menus are themed
  with number keys. The window reads everything through `recall api`, starts it when it isn't
  running, and updates live from its event stream, with no polling. Its own file is
  `~/.agents/recall/app.json`; the one other thing is Sparkle's own update settings, in macOS preferences. The menu bar's Show changes diffs move from `~/Library/Caches/recall`
  to `~/.agents/recall/cache/diffs`, and the plain-menu setting moves from `defaults` to `"ui":
  "menu"` in that file. The app has its own icon.
- **Edit and forget notes in the recall window.** Edit note (⌘E) turns the reader into one text box
  holding the note's rules, one per line, and shows what saving records as you type: each rule kept,
  reworded, removed or new. A removed rule isn't learned again from the same chats, and a new one is
  marked as said by you. Save (⌘S) commits the note, rewrites the MEMORY.md indexes and refreshes
  search, so agents see it from their next session; History shows "Edited by you". Forget… (or ⌫ in
  the list) deletes the note after a sheet that says what happens, and Undo (⌘Z) puts it back for 10
  seconds. While a memory run is writing notes, Save and Forget wait for it and then go ahead; a
  note whose file changed since you opened it is never overwritten. Against a `recall api` from
  before these writes the buttons show read-only with "Update recall to change this here".
- **Change settings on This Mac.** The model that writes notes is a menu of what already works on
  this Mac (the current model, a signed-in Claude or ChatGPT plan, a running local model, an API
  provider whose key recall stored); picking one makes one short test call and saves only if it
  answers. Adding a key or a new provider stays `recall model` in Terminal, so the app never handles
  a key. Coordination rules save as you change them, with a preset menu and Back to Recommended.
  Remove old copies lists each copy recall kept before an upgrade with its size and removes only
  those. The schedule stays read-only, and against a `recall api` from before these writes so do the
  rest.
- **First run.** Open the app on a Mac that isn't set up and a small window does it: it finds your agents
  and their chat counts, you pick who writes notes (Claude plan, ChatGPT plan or a local model, with where
  excerpts go said under the rows), and it installs and connects. It installs uv (after asking) and
  `recall-agents` if they're missing, reads your chats, runs `recall install --only mcp,skill,hook`, sets
  the nightly run for 03:30 and starts the first memory run straight away. Then two switches, both on:
  tell me if a night's run fails, and open when I log in. A Mac where recall already has an agent
  connected skips all of it. A recall older than the app needs is updated after asking. A failed install
  says why, with Try again and Copy details. The app asks to move itself into Applications when it runs
  from a disk image, because updates need it there. The disk image itself opens to the usual drag to Applications
  window, in recall's own look.
- **Updates.** Recall updates through Sparkle, with no Sparkle windows: This Mac has a Versions
  panel (`Recall 0.1.0 → 0.1.1 [Update]`, `recall-agents 0.6.0 → 0.6.1 [Update]`, What's new) and the menu
  bar icon gets a small mark. Nothing installs without Update. recall's latest version comes from PyPI at
  most once a day and is upgraded with the tool that installed it (uv or pipx; otherwise the command is
  shown), after which the app's `recall api` restarts on the new code. Agents still running the old recall
  are listed. A build without `RECALL_SPARKLE_PUBLIC_KEY` has app updates off.
- **A failed night is noticed.** When the nightly run's log changes, the app reads the last run, and it costs
  nothing otherwise. A run that learned nothing, at least half of five or more
  chats failing, a run that died, or no run for two nights marks the menu bar icon, notifies once per
  problem, and opens This Mac's run panel with the fix that fits: Use Claude plan and Raise limit for a
  spend limit, Use Claude plan for a refused key, Copy: claude /login, Open Ollama, Run now. Run again now
  starts a memory run with the nightly limits. A few failed chats stay quiet: they retry by themselves.
- **This Mac** has Open when I log in and Set up again.
- **Run now, and one rhythm through the window.** Run now on This Mac starts a memory run with your
  usual limits and shows its progress. Opening the app by hand opens the window; at login it stays in
  the menu bar. Storage is three rows that open to show their files. New notes get a yellow asterisk
  with a count above the list, and a note's date is never cut. Rows come in two heights, type in
  five sizes and spacing on a 4 pt grid, with one-pixel dividers.

### Changed

- **Menu bar:** the panel now reads `recall api` when it opens and holds its event stream only while
  it is open, so it updates live, and a server it started exits 30 seconds after the panel closes.
  The icon's 15-minute check uses the API only when a server is already running and otherwise runs
  `recall status --json`, because starting a server for it costs more. With a recall that has no
  `recall api` (0.5.2 and earlier), the menu bar uses `recall status --json` for everything, as
  before.

- **App build.** The app is `Recall.app` (same bundle id), with a version and
  an integer build number at the top of build.sh, Sparkle 2.10.1 pinned by sha256 and downloaded into
  `build/vendor`, signed from the inside out, and `RECALL_UNIVERSAL=1` for arm64 and x86_64.

- **Smoother scrolling and typing.** Answers from busy agents wait until a scroll stops and land
  together, so This Mac and the lists don't stutter while chats change. A chat's messages are drawn by
  AppKit's text view, which lays out only what's showing and stays selectable, and their markdown is
  read off the main thread. Find in a long chat no longer stops the window, the arrow keys fetch a
  note or chat once they pause, closing a menu no longer reloads everything, and hidden sections
  don't follow a window resize. On a 120 Hz display a long chat now drops about 4 ms of frames a
  second instead of 14 to 15, find's longest stall is under 0.1 s instead of 3.5 s, and the window
  uses no CPU while idle.

- **Easier to read and to use.** Memory with no notes says so and when the first come, instead of
  showing an empty list. A chat's prose wraps at about 70 characters, while code and tables keep the
  full width, and a chat's title uses the width beside the find box. A memory run shorter than a
  minute shows its time once. Small controls (the find box's arrows, filter tags, the ▾ menus) take a
  click a few points outside what's drawn. This Mac's Access no longer shows "Serve this memory to
  other machines", a placeholder that did nothing; it comes back with server mode.

### Fixed

- **Menus stay on screen.** A menu whose button is near the bottom of the screen opens above it.

- **The Mac app is built for the installed recall by default.** The checkout is baked in only with
  `RECALL_DEV_PROJECT=/path`. It also refuses malformed or oversized answers from `recall api`,
  compares real paths in the scratch-copy guard of its write probes, runs Show Changes without a
  repo's diff drivers and keeps the diff private, and ⌫ and ⌘Z in Memory act on the highlighted note
  and only in that section. The Forget sheet says when an unsaved edit goes with the note. build.sh
  pins its font tools and fails if signing fails.
- **Moving from a private model to one that sends excerpts away asks first.** recall 0.6.0 refuses
  that switch without consent. This Mac now shows "Send chat excerpts to Anthropic?" with Cancel and
  Test and switch, and Test and switch sends the destination back as accepted.

### In recall 0.6.0, for this app

- **`recall api`:** an HTTP and JSON door to your chats, notes, memory runs and status, on a local
  unix socket (mode 0600, no network port), with live updates as server-sent events. It starts on
  demand and exits after five idle minutes, and an open event stream costs no CPU while nothing
  changes. Apps use it instead of opening the index. See docs/api.md.
  `/v1/status` takes `?fresh=1` to skip its 15-second cache.
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

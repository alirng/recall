# recall desktop changelog

recall desktop is the Mac app. It has its own version and runs on recall, the engine, which keeps
[its own changelog](https://github.com/alirng/recall/blob/main/CHANGELOG.md).

## [0.1.0] - Unreleased

Powered by recall 0.6.0.

### Added

- **The recall window.** Open recall (⌘O) in the menu bar app opens one window with two views.
  Memory has the notes list with category tags, filters, a project picker with pins and sort, and a
  reader with each note's rules, the chats it was learned from, related notes and its history. This
  Mac has counts, sources, last night's memory run as a waterfall, storage, model and schedule,
  coordination rules and access. Chats (messages shown as light markdown, with find in chat),
  Projects and Now are plain lists, and ⌘K opens a palette that searches notes and chats and goes to
  any view. Sections switch on the next frame, lists move with the arrow keys, and menus are themed
  with number keys. The window reads everything through `recall api`, starts it when it isn't
  running, and updates live from its event stream, with no polling. Its only file is
  `~/.agents/recall/app.json`. The menu bar's Show changes diffs move from `~/Library/Caches/recall`
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

### Changed

- **Menu bar:** the panel now reads `recall api` when it opens and holds its event stream only while
  it is open, so it updates live, and a server it started exits 30 seconds after the panel closes.
  The icon's 15-minute check uses the API only when a server is already running and otherwise runs
  `recall status --json`, because starting a server for it costs more. With a recall that has no
  `recall api` (0.5.2 and earlier), the menu bar uses `recall status --json` for everything, as
  before.

### Fixed

- **The Mac app is built for the installed recall by default.** The checkout is baked in only with
  `RECALL_DEV_PROJECT=/path`. It also refuses malformed or oversized answers from `recall api`,
  compares real paths in the scratch-copy guard of its write probes, runs Show Changes without a
  repo's diff drivers and keeps the diff private, and ⌫ and ⌘Z in Memory act on the highlighted note
  and only in that section. The Forget sheet says when an unsaved edit goes with the note. build.sh
  pins its font tools and fails if signing fails.
- **Moving from a private model to one that sends excerpts away is refused without consent.** recall
  0.6.0 asks for it through the API. The window has no confirm for this yet, so it shows that refusal;
  `recall model` in Terminal still makes the switch.

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

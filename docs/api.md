# The recall API

`recall api` serves your recall data (chats, notes, memory runs, status, settings) to apps over a
local socket, and makes the few changes an app may make. The Mac app and the menu bar use it, so
they never open the index or write a note themselves.
It is the same door a self-hosted recall would open later: going from this Mac to a server means
changing an address, not rewriting a client.

```bash
recall api            # serve until 5 minutes pass with no client
recall api --idle 0   # serve until stopped
```

## What it reads and what it changes

Reading changes nothing of yours. It may refresh recall's own search cache (the notes search index,
as the MCP server does) and log its use to `usage.jsonl`. Searches made through it are logged with
the client name from the `X-Recall-Client` header (`app` if absent), so you can see whether an app
earns its place.

The writes are few, and each runs recall's own code, the same the command line runs:

- **Notes:** edit a note's text, forget a note, put a forgotten note back. These take the notes lock
  a memory run takes, so the two take turns, and each is one commit in the notes folder.
- **Coordination rules:** a preset, or one situation's action. Only `[coordination]` in config.toml
  changes.
- **The model:** one of the models that already work on this machine, after a test call. The API never
  takes or returns a key; adding a key or a new provider stays `recall model`.
- **Storage:** removing the copies recall kept before an upgrade, and nothing else.
- **Imports:** adding the chats in a claude.ai or ChatGPT account export, the same as `recall import`.

Beyond an import adding chats, chats and the chat archive are never changed through the API, and the schedule never is. `/v1/info` lists what a
server can change under `writes`; a server without it (from before these writes) is read-only, and an app
should show its controls that way.

## The socket

- `running/api.sock` in recall's data folder (`~/.agents/recall/running/api.sock`, or under `RECALL_HOME`). Files that exist only while recall runs go in that one `running/` folder; its lock is `running/api.lock`. Errors and warnings go to `logs/api.log`.
- macOS allows a socket path of at most 103 bytes. If `RECALL_HOME` makes the path longer, `recall api` says so and stops; give it a shorter home or `--socket` with a short path.
- Mode 0600: only your user can connect. There is no TCP port.
- HTTP/1.1 and JSON, every path under `/v1`. Reads are `GET`; the writes below are `PUT` or `POST`
  with a JSON body.

```bash
curl --unix-socket ~/.agents/recall/running/api.sock http://x/v1/info
```

Any host name works after `http://`; the socket is the address.

## Lifecycle

Nothing runs at rest. A client that finds no answer on the socket starts `recall api`, and the
server exits by itself after `--idle` seconds (default 300) with no request in flight and no open
event stream.

- A stale socket file from a crashed run is cleared by the next start. A second `recall api` while
  one is running exits quietly.
- The server waits on the kernel, not on timers: the only timer it owns is the idle exit. With a
  stream open and nothing changing it uses no CPU.
- It never rebuilds the chat index. If the index was built by another release it answers
  `503 index_needs_rebuild` and leaves the rebuild to `recall index`.

### Socket activation (launchd)

`recall api --launchd` takes its socket from launchd instead of binding one. A job whose plist has a
`Sockets` entry (`SockPathName` the path above, `SockPathMode` 384) keeps the socket open while no
process runs, starts `recall api --launchd` on the first connection, and holds any connection that
arrives while the server is exiting for the next start. A client then needs only the socket path,
not the path to `recall`. Nothing installs this job yet.

Development note: the installed release has no `api` command until a release ships it. While it is
in development an app points at the repo copy (`uv run recall api` from a checkout).

## Conventions

- Every time is ISO 8601 with an offset. Notes keep dates without a time of day, so those read as
  midnight local time.
- `project` is a project's name everywhere. `folder` is a path, where there is one.
- Lists take `limit` and `offset` and answer `total`.
- Errors are `{"error": {"code", "message"}}` with a 4xx or 5xx status. A write refused because a
  memory run holds the notes is `409 busy`; one refused because the note's file changed since the
  client read it is `409 conflict`.
- A project filter matches the name exactly: `project=recall` does not return `rererecall`.
- API keys are never returned. The config says where a key lives ("macOS Keychain"), not what it is.

## Endpoints

| Path | Answers |
| --- | --- |
| `/v1/info` | recall version, API version, instance name (`This Mac`), whether live updates work, and `writes`: what this server can change (`notes`, `coordination`, `model`, `storage`) |
| `/v1/status` | what `recall status --json` gives: live sessions, overlaps, outlived servers, power, roots (`?since=12h`; kept 15 s, `?fresh=1` skips that and recomputes) |
| `/v1/stats` | chats and messages by agent, with each agent's latest chat; notes count |
| `/v1/storage` | every place recall keeps things, inside and outside `~/.agents`: `items` with a plain label, `path`, `bytes`, `kind` and `exists`, plus `totals` by kind. A copy kept before an upgrade also has `version` and `date` where its name gives them. Kinds: `data` (lost if deleted), `rebuildable`, `running`, `settings`, `backup`. Each file is marked on its own, never a whole folder, because `cache/` holds rebuildable files next to mail and edit records that are not. Outside `~/.agents` it lists the installed program, the background job plists, the menu bar app, leftovers of earlier builds, and whether a Keychain item exists (never its contents) |
| `/v1/runs` | memory runs, newest first, with counts (created, added, refined, confirmed, retired), calls, tokens and cost (`?limit=10`) |
| `/v1/runs/latest` | the last run with `steps` (its log lines, dated) and `phases` (commit links, read, consolidate, commit notes, each with start, finish and result) |
| `/v1/config` | model, embeddings, run limits, schedule, power; a key's whereabouts, never the key |
| `/v1/coordination` | what happens when two chats edit the same file: each situation, its action and default, the presets, and `preset`: the one the rules match now (`null` for custom) |
| `/v1/models` | the models that can write memories on this machine now, found the way `recall model` finds them: the one set now, a signed-in Claude or ChatGPT plan, each model of a running local server, and an API provider whose key recall stored. Each `option` has an `id`, a `label` and a `detail`; none carries a key. To list the models of an API provider whose key recall stored, it sends that key to the vendor to ask for its model list (no chat data goes with it). Kept 30 s |
| `/v1/doctor` | each agent folder: connected or not, and what is missing |
| `/v1/projects` | project names with chat and note counts, agents and latest activity |
| `/v1/notes` | notes with `type`, `project`, `status`, `scope`, `sort`, `limit`, `offset`; also `facets` for the filters |
| `/v1/notes/{id}` | one note in full: rules (all, with status and evidence), source chats (title, agent, date, or `found: false` if gone), related notes, `history`, and `stamp`: the file as read, which an edit or a forget sends back |
| `/v1/chats` | chats with `agent`, `project`, `since`, `until`, `subagents=1`, `limit`, `offset` |
| `/v1/chats/{id}/messages` | one chat's messages, paged with `offset`, `limit`, `max_chars` |
| `/v1/search` | chats and notes for `q`, with `kind` (all, chats, notes), `agent`, `project`, `since`, `until`, `limit`; echoes the filters used and `took_ms` |
| `/v1/events` | server-sent events |

## Writes

| Method and path | Body | Does |
| --- | --- | --- |
| `POST /v1/notes/{id}/preview` | `{"text"}` | What saving `text` as the note's text would record, writing nothing: `changes`, each a rule with `kind` (`kept`, `reworded`, `removed`, `new`, `merged`, `restored`), its `text`, and `said_by` (`developer` for a rule in your words; `unverified` for one holding a command or a link). Also `counts`, and `empty` when the text is blank |
| `PUT /v1/notes/{id}` | `{"text", "stamp"}` (`stamp` is required: `400 bad_parameter` without it) | Saves `text` as the note's text: the rules one per line, as in the note's file. It is read the way a hand edit of the file is: a line that stays is kept, a changed line is that rule reworded, a line gone is removed and not learned again from the same chats, and a new line is a rule in your words. Rewrites the MEMORY.md indexes, commits "edited by hand: <title>" and refreshes the search index. `409 busy` while a memory run holds the notes, `409 conflict` if the file changed since `stamp`, `400 empty` for blank text (forget the note instead). Answers `changed`, `commit`, `changes` and `counts` |
| `POST /v1/notes/{id}/forget` | `{"stamp"}` (required: `400 bad_parameter` without it) | Deletes an active note's file and records it as rejected (its title, scope and project only), so no memory run learns it again from the same chats. Its old text stays in the notes folder's git history. Commits "forgotten: <title>"; answers that `commit` |
| `POST /v1/notes/{id}/restore` | `{"commit"}` | Puts back the note that `commit` (a forget's) deleted, from the notes folder's history; recall stops treating it as rejected. `409 conflict` if the note is back already, `400 not_forgotten` for a note that was not forgotten (the deletion ledger does not list it as rejected and the commit is not a "forgotten:" one) |
| `PUT /v1/coordination` | `{"preset"}` or `{"rules": {"situation": "action"}}` | Sets the coordination rules. Agents read them on their next edit. Answers `/v1/coordination` plus `was`, the rules before |
| `PUT /v1/model` | `{"option", "accept_destination"?}` | Switches the model that writes memories to one of `/v1/models`' options, after one short test call with an invented chat; saves `[model]` only if it answers, otherwise `422 test_failed` with the reason and nothing changes. The same provider keeps its other settings; another provider starts fresh, with a signed-in plan to fall back on when an API key's credits run out. The test call can take a minute on a plan or a local model. Consent: when the model now set is private (nothing leaves this Mac) and the new one is not, the body must carry `"accept_destination"`, the new model's `destination`, exactly as the error below gives it; without it the answer is `409 needs_consent` with `destination` in the `error`, and nothing is sent or changed |
| `POST /v1/storage/remove` | `{"paths": [...]}` | Deletes copies recall kept before an upgrade, given as `/v1/storage` paths. Every path must be an item tagged `backup` now, inside recall's data folder, or nothing is removed. Answers `removed` and `bytes` |
| `POST /v1/import` | `{"path"}` (an absolute path to the export's ZIP, folder or `conversations.json`) | Indexes the export, as `recall import` does, and answers `service`, `chats`, `indexed` and `unchanged`. A path that is not an export answers `400 refused`, and nothing is indexed. The export can be large, so allow minutes. |

Every write answers `took`: `write_ms`, and for notes `index_ms`, the search index refresh. A
notes write sends `changed` with topic `notes` at once; a settings write sends `config`.

`recall api` started by an app or by launchd gets a bare `PATH`. It adds the folders the nightly run's
schedule gets (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`), so the model menu finds Claude
Code, Codex and local servers where the nightly run will.

## History

A note's `history` lists the commits that touched it in the notes repo. Each entry has `changes`:
what that commit did to this note, found by comparing the file with its parent: `created`, `added`,
`refined`, `retired`, `confirmed` or `relinked`, with the rule's id and text.

Memory-run `phases` and `steps` are read from the nightly run's text log, which has times of day
only. The API dates them from the run's record. A log line worded differently by a later release
shows up under kind `other`.

## Live updates

`GET /v1/events` sends `hello`, then `changed` events:

```text
event: changed
data: {"topics": ["chats", "notes"], "at": "2026-01-01T10:00:00+00:00"}
```

Topics: `chats` (the index changed), `notes` (the notes repo has a new commit), `runs` (a memory run
wrote its log or finished), `config` (config.toml changed), and `status`, sent only when the
"Needs you" result actually changes. `status` is recomputed at most once every five minutes, and only while a
stream is open and something else changed, because computing it costs more than anything else here.

Events come from kqueue on the index, its journal, config.toml, the run log and the notes repo's
branch. A burst is coalesced over two seconds. Edits you make by hand to a note file are not
watched; they show once the notes repo next commits, so an app should also refetch on focus.

Live updates work on macOS only for now. On other systems `hello` says `live: false` and nothing
else is sent.

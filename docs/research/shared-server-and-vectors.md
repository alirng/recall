# One recall for every agent session: process model and vector storage

Snapshot: 2026-10-16. Question: every agent session (Claude Code, Codex, Cursor) starts its own `recall serve` over MCP stdio, and each one paid for a private copy of every embedding vector. What is the ideal pattern for keeping memory flat however many agents are open? Method: measurements on one Apple Silicon Mac against a scratch copy of the real index, a throwaway prototype, and primary sources (source at pinned commits, official docs, the MCP spec, project issue trackers). Inline citations follow the other notes here: `[src: owner/repo@sha:path#L]`, `[doc: url]`, `[issue: owner/repo#N]`, `[observed]`, `[inferred]`.

## Answer

Two layers, and the first matters more.

1. **Process: one shared recall daemon behind a thin stdio forwarder, in the gopls shape.** Agents keep launching `recall serve` over stdio, so no client config changes. `recall serve` becomes a standard-library forwarder (about 9 MB, no MCP SDK) that pipes stdin and stdout to a per-user, per-release unix socket, starting the daemon if nothing answers. The daemon runs recall's existing `MCPServer`, one MCP session per connection, and exits a few minutes after its last client leaves. Measured with real MCP sessions: 24 agents cost 288 MB in total, against 1,425 MB for the 24 servers 0.4.2 was running on the test machine and about 2.1 GB for 24 servers with the shared vector file alone.
2. **Vectors: keep the shared memory-mapped file.** No alternative store beats it: sqlite-vec would remove the second copy but has no float16 and cannot load on the python.org macOS build, and every ANN library is either a private copy per process or an immutable file that needs the same sync. Inside a daemon the file still earns its place: the daemon's footprint stays flat as history grows, and one-shot processes (`recall search`, evals) search without building a matrix.

Embeddings already come from an external server (Ollama), which is the choice that the projects with the worst per-process memory did not make.

## Measurements

All on a scratch copy of the real index (22,792 vectors of 768 dimensions, 22,565 messages), Ollama `nomic-embed-text`, Python 3.13.16 from uv. Footprint is macOS `footprint` (what Activity Monitor calls Memory: dirty plus compressed pages, not clean file-backed ones). RSS misleads on macOS: idle servers are compressed or swapped, so `ps` showed 4 MB each [observed]; claude-mem saw 13 MB RSS against a 23 GB footprint [issue: thedotmack/claude-mem#3191].

**What ran before this change.** 24 `recall serve` processes (release 0.4.2): 18 under Claude Code, one per `claude` process; 5 under one Codex app-server; 1 under Cursor's `mcp-process` helper. Their footprint summed to 1,425 MB. An idle one is 54 to 64 MB, almost all of it compressed or swapped (`footprint --swapped`: 57 MB dirty, 56.7 MB swapped); the one that had searched was 150 MB. None was orphaned: each exits when its agent closes stdin [observed].

**What one server is made of** (a fresh interpreter after each import):

| Process | Footprint | RSS | Cold start |
|---|---|---|---|
| bare `python` | 7.9 MB | 17.2 MB | 20 ms |
| + `socket`, `json`, `select` (a forwarder's imports) | 8.9 MB | 18.5 MB | 37 ms |
| + `mcp.server.mcpserver` (the SDK) | 58.5 MB | 73.5 MB | 555 ms |
| `recall.server` (what `recall serve` imports) | 62.0 MB | 78.1 MB | 617 ms |
| `recall.server` + numpy | 68.6 MB | 86.6 MB | 708 ms |

The MCP SDK is about 50 MB and half a second of every server; recall's own modules add 3.5 MB and numpy 6.6 MB. `-X importtime` puts `mcp.client` (238 ms) and `mcp_types._types` (156 ms) on top [observed]. The vectors were the second problem, not the first.

**Three designs, measured.** Each agent ran `initialize` and two `search_chats` calls; footprint is the sum over every recall process involved.

| Design | 1 agent | 5 agents | 24 agents | 2nd search |
|---|---|---|---|---|
| `main` (`acab9a8`): a server per agent, each with its own float32 matrix | 166 MB | 830 MB | about 4.0 GB (166 x 24, not run) | 63-79 ms |
| shared vector file (`vectorfile.py`): a server per agent, vectors mapped from one file | 86 MB | 428 MB | about 2.1 GB (86 x 24, not run) | 60-69 ms |
| prototype: forwarder per agent + one daemon | 89 MB | 125 MB (5 x 9 + 80) | 288 MB (24 x 9 + 73) | 62-74 ms, median 68 |

In the prototype only the search that starts the daemon pays its import (811-927 ms); every other agent's first search took 69-77 ms, where a fresh server's took 220-298 ms. The daemon exited 20 s after its last connection closed, as configured [observed]. The prototype is recall's own `MCPServer`, unchanged, served once per socket connection through the SDK's `stdio_server(stdin, stdout)`, which takes explicit streams [src: modelcontextprotocol/python-sdk@2118f14 (v2.3.0):src/mcp/server/stdio.py#L162-L169]; the forwarder is 50 lines of standard library.

## How other projects handle many sessions

Every MCP memory or search server read defaults to one stdio process per session; a shared process is opt-in and user-started (qmd, Serena, basic-memory, mcp-memory-service) or the whole design (claude-mem's worker) [src and issues in the table].

| Project | Process model | Vectors | Embeddings | What went wrong |
|---|---|---|---|---|
| claude-mem | one HTTP worker on `37700 + uid % 100`, spawned on demand under a lock; thin stdio MCP per session [src: thedotmack/claude-mem@26e839a:src/servers/mcp-server.ts#L11-L18; src/services/worker-spawner.ts#L8-L27] | Chroma via one `chroma-mcp` sidecar | ONNX MiniLM in the sidecar | version-mismatch recycle loop, 808 orphaned sidecar pairs, 74 GB swap [issue: thedotmack/claude-mem#3905]; port ownership [issue: thedotmack/claude-mem#2926]; idle exit defaults to never [src: thedotmack/claude-mem@26e839a:src/shared/SettingsDefaultsManager.ts#L183] |
| mcp-memory-service | stdio per client, each opening SQLite; optional HTTP server [src: doobidoo/mcp-memory-service@f572cee:docs/integration/multi-client.md#L5-L37] | sqlite-vec `vec0` | ONNX in-process; optional external server, "a single embedding service for multiple MCP instances" [src: doobidoo/mcp-memory-service@f572cee:docs/deployment/external-embeddings.md#L3-L24] | "Total per process: 685 MB - 2.3 GB" [src: doobidoo/mcp-memory-service@f572cee:docs/troubleshooting/memory-management.md#L9-L18]; its stdio-to-HTTP thin-client mode was dead code for months, then removed [issue: doobidoo/mcp-memory-service#1155] |
| basic-memory | stdio per client; optional user-run HTTP, no auth [src: basicmachines-co/basic-memory@a72751c:src/basic_memory/cli/commands/mcp.py#L39-L59, L138-L165] | sqlite-vec | FastEmbed ONNX in-process | 26 GB footprint from repeated model loads [issue: basicmachines-co/basic-memory#872]; concurrent starts miss client timeouts [issue: basicmachines-co/basic-memory#1731] |
| qmd | stdio per client; opt-in HTTP daemon "that avoids repeated model loading" [src: tobi/qmd@86d2365:README.md#L133-L147] | sqlite-vec `vec0` | GGUF model in-process | about 4.5 GB per invocation [issue: tobi/qmd#942]; daemon `initialize` blocked behind a running query [issue: tobi/qmd#762]; stdio server did not exit on stdin EOF [issue: tobi/qmd#751]; an embed lock file after concurrent runs raced [src: tobi/qmd@86d2365:src/cli/embed-lock.ts#L1-L23] |
| Serena | stdio per client; HTTP only if "you control the server lifecycle yourself" [src: oraios/serena@d9e4c79:docs/02-usage/020_running.md#L55-L77] | none | none | an orphaned process at 36 GB [issue: oraios/serena#1367] and 90 orphaned JVMs [issue: oraios/serena#1490]; uncoordinated instances corrupting one workspace [issue: oraios/serena#1944] |
| chroma-mcp | stdio only [src: chroma-core/chroma-mcp@98ff675:src/chroma_mcp/server.py#L30, L667] | Chroma, HNSW in RAM per process | Chroma default | 11 orphans, about 8.9 GB [issue: chroma-core/chroma-mcp#65] |
| reference `memory` server | stdio per client | none (JSONL) | none | cross-process lost writes, 1 of 20 survived, fixed with a lock file [issue: modelcontextprotocol/servers#4797] |
| claude-context | stdio per session, each with its own 5-minute sync [src: zilliztech/claude-context@6fc318b:packages/mcp/src/sync.ts#L10-L12] | Milvus server | remote API or Ollama | sync CPU churn until a cross-process lock [src: zilliztech/claude-context@6fc318b:packages/mcp/src/sync.ts#L72-L99] |

What recurs: vectors live in a server or in SQLite read through the page cache, never in a matrix per process; the in-process embedding model, not the vectors, is the big per-process cost; SQLite WAL plus a non-zero `busy_timeout` is the multi-process answer (a zero default recurs as a bug: [issue: thedotmack/claude-mem#2821], [issue: tobi/qmd#710], [issue: mem0ai/mem0#7196]); single-writer jobs take a lock file with stale-lock recovery; and shared daemons fail in their own ways (recycle loops, ports, head-of-line blocking, idle-disposal races).

## How developer daemons share one heavy server

Outside MCP the pattern is mature. gopls exists for exactly this reason: many editors mean many caches, and short sessions pay start-up each time [src: golang/tools@82ec52b:gopls/doc/daemon.md#L18-L30].

- **Found by a deterministic address keyed on what must match.** gopls names a unix socket after a hash of its build id and the user, so "a daemon reached that way always has the same version as its client" and an old daemon just idles out [src: golang/tools@82ec52b:gopls/internal/lsprpc/autostart_posix.go#L34-L69; gopls/internal/cmd/cmd.go#L423-L437]. Bazel stops a wrong-version server and starts a new one [doc: https://bazel.build/run/client-server]; Gradle reuses only same-version daemons [doc: https://docs.gradle.org/current/userguide/gradle_daemon.html].
- **Started by the client re-executing itself, detached.** gopls dials, and on failure deletes a stale socket, re-execs as `serve -listen unix;<path> -listen.timeout 1m` with `Setsid`, then retries the dial with backoff for about 13 s [src: golang/tools@82ec52b:gopls/internal/lsprpc/dialer.go#L70-L113]. gopls leaves the start race as a TODO [src: same#L82-L84]; sccache treats the bound address as the lock, so a loser sees `AddrInUse` and just connects [src: mozilla/sccache@7bdb9bd:src/commands.rs#L336-L338].
- **Exits on an idle timer** armed when the client count reaches zero: gopls 1 minute [src: golang/tools@82ec52b:internal/jsonrpc2/serve.go#L105-L141], sccache 10 minutes [src: mozilla/sccache@7bdb9bd:README.md#L170-L179], Gradle, Bazel and Nx 3 hours.
- **Per-client state stays per connection, and the forwarder carries the client's context.** gopls gives each connection its own session over a shared cache, and the forwarder adds the editor's environment to `initialize` because the daemon's own environment is not the client's [src: golang/tools@82ec52b:gopls/internal/lsprpc/lsprpc.go#L88-L138, L354-L389]. It also refuses a socket owned by another user [src: golang/tools@82ec52b:gopls/internal/lsprpc/autostart_posix.go#L71-L95].

Among MCP bridges only mcp-proxy truly shares one backend; supergateway starts a child per session and mcp-remote is a per-session shim [src: sparfenyuk/mcp-proxy@701a045:src/mcp_proxy/mcp_server.py#L245-L263; supercorp-ai/supergateway@a0be844:src/gateways/statefulSession.ts#L32-L84; geelen/mcp-remote@b529ea1:README.md#L15-L31].

## Why stdio forwarding, not a shared HTTP server

All three clients accept a URL entry with headers: Claude Code `"type": "http"` [doc: https://code.claude.com/docs/en/mcp.md], Codex `url` with `bearer_token_env_var` and no experimental flag [doc: https://developers.openai.com/codex/mcp.md; src: openai/codex@0e67502:codex-rs/config/src/mcp_types.rs#L514-L541], Cursor `url` and `headers` [doc: https://cursor.com/docs/mcp.md]. Direct HTTP still loses on every other count:

- **Nothing would start it.** No client starts an HTTP server, so it needs launchd or systemd and stays resident, against recall's near-zero idle cost. Codex gives a server `startup_timeout_sec`, 10 s by default, retrying `initialize` within it [doc: https://developers.openai.com/codex/mcp.md; src: openai/codex@0e67502:codex-rs/rmcp-client/src/streamable_http_retry.rs#L24, L36-L50]; Claude Code retries a first connection three times, then marks the server failed [doc: https://code.claude.com/docs/en/mcp.md].
- **It needs network security.** Localhost TCP means validating `Origin`, binding to 127.0.0.1 and authenticating every connection, as the spec requires of local servers [src: modelcontextprotocol/modelcontextprotocol@86f6e0c:docs/specification/2025-11-25/basic/transports.mdx#L74-L84]; none of the three clients offers a unix-socket transport.
- **It loses per-session context.** Claude Code gives `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR` to stdio servers only [doc: https://code.claude.com/docs/en/env-vars.md].
- **It changes every client config** and must serve two spec eras at once: Codex asks for 2025-06-18 [src: openai/codex@0e67502:codex-rs/codex-mcp/src/rmcp_client.rs#L1167-L1171], while the 2026-07-28 revision drops sessions, `Mcp-Session-Id` and the `initialize` handshake [src: modelcontextprotocol/modelcontextprotocol@86f6e0c:docs/specification/2026-07-28/changelog.mdx#L12-L28].

Claude Code's newer client runtime asks stdio servers too whether they speak the 2026-07-28 revision and uses it when they do [doc: https://code.claude.com/docs/en/mcp.md, "MCP client runtimes"]. So the forwarder must stay a byte pipe and leave the protocol to the SDK in the daemon, which already negotiates both eras for `recall serve` today.

A stdio forwarder keeps today's config, auto-starts like gopls, and carries the session context. The daemon could add HTTP later for clients that want it.

## Vector store options

| Option | One copy for all processes | Blocker for recall |
|---|---|---|
| shared fp16 file, `mmap`, exact numpy scan (`vectorfile.py`) | yes, the page cache | a second copy beside the SQLite table, kept in step by triggers |
| sqlite-vec `vec0` | yes, a query streams chunks through a 3 MiB buffer [src: asg017/sqlite-vec@04d28bd:sqlite-vec.c#L7300-L7426] | no float16 (open since 2024), so float32 at twice the size; needs `enable_load_extension`, which the python.org macOS installer and default pyenv lack [src: python/cpython@75ce53d:Mac/BuildScript/build-installer.py#L1160-L1177; issue: pyenv/pyenv#1702] (uv and Homebrew builds have it [observed]); thin maintenance and poor space reuse after deletes |
| usearch `view()` | pages shared, but immutable | updates mean rebuild and swap: the same sync problem |
| hnswlib | no, a private copy per process | ships only as source |
| FAISS read-only mmap | yes | 5-19 MB wheel for what numpy does in 4 ms; its maintainers answered this exact use case with `faiss.knn()` over memory-mapped data [issue: facebookresearch/faiss#3165] |
| LanceDB | data once, caches per process | about 100 MB with Arrow |
| Chroma `PersistentClient` | no | "not process safe" per its maintainers [issue: chroma-core/chroma#3792] |

ANN buys nothing at this size: an exact scan of 23k x 768 takes about 4 ms. The file's rules, which `vectorfile.py` follows and which the research confirms as the ones that matter: append only; publish the row count outside the file after the bytes are flushed; readers map only that prefix; never shrink a file another process may have mapped (SIGBUS) [doc: https://man7.org/linux/man-pages/man2/mmap.2.html]; rewrite into a new file and swap the header on `forget`. `vectorfile.py` truncates only an unpublished tail left by a crashed append, which no reader maps. macOS `fsync` does not flush the drive cache (`F_FULLFSYNC` does); the file is derived and every candidate is checked against the table, so a torn row after a power loss costs a rebuild, not a wrong answer [inferred].

## The pattern for recall

**Forwarder** (`recall serve`): imports only the standard library and never parses MCP. Records its working directory, its pid and the session variables agents set (`CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR` from Claude Code; Codex sets `CODEX_THREAD_ID` for tool subprocesses, unverified for MCP children), then connects to the socket and sends them as one JSON line before piping bytes both ways. If nothing answers it takes `serve.lock`, checks again, starts the daemon detached (`start_new_session`), and retries the connection for up to about 15 s. It exits when stdin closes (agents close it when a session ends, which is how recall's servers avoid orphans today) or when the socket closes.

**Address**: a socket in `RECALL_HOME` named for the release and a hash of the interpreter path, so a dev checkout and the installed tool never share a daemon and an upgrade never talks to an old one. Mode 0600, and the owner's uid checked before use. macOS caps socket paths at 104 bytes [observed: `OSError: AF_UNIX path too long` with a long `RECALL_HOME`], so bind and connect relative to the folder.

**Daemon**: recall's `MCPServer` served per connection through `stdio_server(stdin, stdout)`. Each connection is its own MCP session, so `clientInfo` per agent keeps working. The session context replaces the two process-wide values the tools read today: `os.getpid()` in usage logging (`server.py`) and `os.getcwd()` as the base for relative paths (`commits.py` L850, L939). It exits about 5 minutes after the last connection closes (between gopls's 1 minute and sccache's 10). The SDK runs synchronous tools on worker threads [src: modelcontextprotocol/python-sdk@2118f14:src/mcp/server/mcpserver/resolve.py#L556], so one slow call should not block another session's `initialize` (qmd's failure); a test should confirm it.

**Failure and upgrade**: if the daemon dies, every session drops at once, and Claude Code never reconnects a stdio server by itself [doc: https://code.claude.com/docs/en/mcp.md]. A second step can make the forwarder reconnect: restart the daemon, and for a legacy-era session replay the client's `initialize` and drop the duplicate reply (ra-multiplex answers later clients' `initialize` from the first one's result [src: pr2502/ra-multiplex@d01f84d:src/instance.rs#L124-L153]); a 2026-07-28 session has no handshake to replay. Reconnecting to the newest installed release also ends today's "restart your agents after upgrading".

**Kept as they are**: WAL with `timeout=30`; the index lock and embed lock (hooks and `recall search` still index, and `recall embed` still runs apart); Ollama as the embedder; the shared vector file.

**Cost of the change**: a new module of a few hundred lines (forwarder, daemon, address and lock handling), a session-context hook in two places, and end-to-end runs with all three agents, including many sessions starting at once (a desktop app restoring its sessions) and an upgrade while sessions are open.

## What this means for the shared vector file

Keep it. The research confirms the mmap file as the right store and its file handling as the part that matters. The changes that came with it are independent of the process model: incremental `pending()`, the embed lock and deadline (qmd added the same lock for the same race), and the shorter interactive timeouts. The file alone halves memory per agent (166 to 86 MB); the daemon is what makes it flat in the number of agents.

## Open questions

1. Whether Claude Code re-initialises an HTTP session after a 404 (undocumented); only matters if HTTP is added.
2. Whether Cursor starts one stdio server per window or per app (one helper seen once).
3. The best idle timeout for recall: daemon start costs about 0.6-0.9 s on the next first call.
4. Whether hooks should also talk to the daemon instead of importing recall cold on every prompt (the hook-speed work's area).

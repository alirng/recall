# LLM backends for recall's memory extraction

Research notes for the "Memory notes + dreaming" roadmap item in `README.md`: a batch job that extracts structured JSON memories from coding-agent transcripts, then consolidates them. First run is about 8M tokens of chat text; afterwards small nightly increments. Goal: no API keys and no extra setup.

Written 2026-10-06. Measurements come from one Apple-silicon Mac with 24 GiB of unified memory (macOS 27.0). All sources were fetched that day.

**How to read this.** Every factual claim ends with a source key in square brackets, resolved under "Sources". Tags: **[M]** measured on one Mac during this research (commands in Appendix A), **[D]** derived from cited numbers (the arithmetic is shown), **[U]** not verified, needs a calibration run. Vendor terms and docs are paraphrased rather than quoted; follow the links for exact wording. Nothing here is legal advice.

> **Decision log, 2026-10-06**
>
> - A first round of local benchmarks was run on one Mac (Ollama `qwen3:8b`, and Apple's on-device model). Afterwards the machine was reported to crash while the local model was in use. The cause was not diagnosed, and our own benchmark requests used that same resident model, so treat every local number below as measured under contention. All local benchmarking stopped at that point.
> - Local backends are therefore **deprioritised and opt-in** (Part 2).
> - recall's **default backend is `codex exec` with `gpt-6-luna` at low reasoning effort** (section 2.1). Claude Code `claude -p` is the first fallback.
> - The Codex, Claude, Cursor and Antigravity sections were written from docs, source code and read-only local checks only. No model call was made through any subscription CLI.

## 0. Summary

1. **Default: Codex.** `codex exec` on the user's ChatGPT plan, model `gpt-6-luna`, low reasoning effort, `--ephemeral`, `--output-schema`, run from a scratch directory [cx-exec][cx-src-exec][oai-luna]. Luna is OpenAI's cheapest and fastest tier in the Codex model lineup [cx-models]: API price $0.10 in and $0.50 out per million tokens, about 135 output tok/s and 2.4 s time to first token at low effort [oai-luna][aa-luna]. For about 8M input tokens packed into about 200 calls expect roughly 24 Codex credits (about $1 API-equivalent), about an hour sequentially or 15 to 20 minutes with four workers [D]. 200 calls fit under the lowest published Plus allowance for Luna, 350 local messages per 5-hour window [cx-pricing].
2. **First fallback: Claude Code `claude -p --model haiku`.** It has `--json-schema`, `--tools ""`, `--system-prompt`, `--no-session-persistence` [cc-cli]. Two traps: `ANTHROPIC_API_KEY` can be set in a developer's shell, and `-p` always prefers it over the subscription, so recall must scrub it [cc-env][M]; and `--bare` cannot be used because it ignores subscription login [cc-headless].
3. **Do not let recall index its own runs.** Codex `--ephemeral` skips the rollout file and the state DB [cx-src-session]. Claude `--no-session-persistence` (or `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1`) skips transcripts [cc-cli][cc-env]. Cursor has no switch, and headless runs do write transcripts [cur-changelog]. So also filter at index time on the scratch cwd; all three readers in `src/recall/sources/` already capture cwd or project [recall-src].
4. **Gemini CLI is gone for consumers.** Since 2026-06-18 it no longer serves free, AI Pro or AI Ultra individual accounts [gem-disc][gem-dep]. Its successor `agy` has `-p` and `--json-schema`, but the free quota is unpublished and Google collects interaction data by default [agy-headless][agy-plans][agy-readme].
5. **Cursor `agent -p` is last resort.** No schema flag, no system-prompt flag, no tool-disable flag, no persistence flag in the documented parameter list [cur-params].
6. **Local models: opt-in only.** On one Mac, Qwen3-8B Q4_K_M through Ollama measured 21 to 35 tok/s generation and 200 to 620 tok/s prompt processing [M]; Apple's on-device model measured about 29 tok/s generation and 590 to 995 tok/s prompt processing with an 8,192-token window on macOS 27 (Apple documents 4,096) [M][afm-ctx]. A full local first run is hours to most of a day [D].
7. **Embeddings stay local and light.** model2vec `potion-base-8M` is 30 MB with numpy-class dependencies; `fastembed` with `bge-small-en-v1.5` is a 67 MB quantised ONNX model and scores far higher on retrieval [m2v][fe][hf-static].
8. **Privacy and terms need a consent gate.** On consumer plans, both OpenAI and Anthropic train on content unless the user opts out, and this includes Codex and Claude Code use [oai-data][cc-data]. Both vendors' terms restrict scripted access in general wording while documenting their CLIs for scripts; the position is gray, not clear (section 2.6) [oai-terms][anth-terms][cc-legal][cx-exec].

## 1. Environment observed (read-only checks)

| Item | Observation | Src |
| --- | --- | --- |
| OS / chip | macOS 27.0, Apple M4 Pro, 16 GPU cores, 24 GiB. This matches the M4 Pro configuration used in the BaseRT paper (16 GPU cores, 24 GB, 273 GB/s), so its numbers transfer directly | [M][baseRT] |
| Toolchain | Command Line Tools only (`xcode-select -p` is `/Library/Developer/CommandLineTools`, no Xcode.app); Swift 6.4; MacOSX27.0 SDK contains FoundationModels; `uv` 0.12.23; Python 3.14.8 | [M] |
| Claude Code | 2.1.287 on PATH; `claude auth status` reports a claude.ai subscription login (`authMethod` claude.ai, first-party provider) | [M] |
| Codex | Not on PATH, but the OpenAI IDE extension in Cursor bundles a binary: `~/.cursor/extensions/openai.chatgpt-*/bin/macos-aarch64/codex` (`codex-cli 0.154.0-alpha.6.2`); `codex login status` prints `Logged in using ChatGPT`. Its `exec --help` lists `--ephemeral`, `--output-schema`, `--ignore-user-config`, `--ignore-rules`, `--skip-git-repo-check`, `-C`, `-s`, `-m`, `--json`, `-o`, `--disable` | [M] |
| Codex model cache | `~/.codex/models_cache.json` (fetched 2026-10-06) lists `gpt-6-luna` (context 272,000, max 872,000, efforts low/medium/high/xhigh/max, default medium), `gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-astra`, `gpt-5.6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra` | [M] |
| Cursor CLI | `agent` / `cursor-agent` 2026.01.23, `agent status` says not logged in. The docs changelog is at an August 26, 2026 release, so this install is about 8 months behind | [M][cur-changelog] |
| Not installed | `gemini`, `agy` (a dangling `~/.antigravity/antigravity/bin/agy` symlink points at an Antigravity app that is no longer present), `lms`, `llama-server`, `llama-cli`, `mlx_lm.*`, `apfel`; Homebrew has `mlx`, `ollama`, `whisper-cpp` | [M] |
| Ollama | 0.35.0 running on 127.0.0.1:11434 with one model, `qwen3:8b` (5.2 GB on disk, Q4_K_M, 40,960 native context) | [M] |
| Env vars | `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are both set in the shell (presence only was checked, values were not read). `claude -p` would therefore bill the API, not the subscription | [M][cc-env] |
| History size | `~/.claude/projects` 1.1 GB and `~/.codex/sessions` 1.9 GB of JSONL (includes tool output; recall drops that). README baseline is about 2,700 chats | [M][recall-src] |
| recall side effects | `~/.codex/config.toml.recall-backup` exists: recall's installer registered its MCP server in Codex's config. A headless child that loads the user config would start recall's own MCP server | [M][recall-src] |

## 2. Part 1: subscription CLIs (primary path)

recall's users already run Claude Code, Codex or Cursor [README], so at least one of these CLIs is almost always installed and signed in. That is the only backend family with zero setup.

### 2.0 Rules that apply to every CLI backend

1. **Scratch cwd.** Run every child in `~/.agents/recall/scratch` (empty, not a git repo, no `CLAUDE.md` or `AGENTS.md` in any parent). Project-level config and instruction files are discovered from the cwd and its parents [sdk-features][cx-config-basic].
2. **No persistence, plus an index-time filter.** Use each tool's no-persist flag (below). Also make recall's indexer skip any session whose `project`/cwd is under the scratch dir, and any whose first user turn starts with a sentinel such as `<recall-internal:v1>`. recall's own rule is never to write to another tool's data, so deleting stray transcripts is not an option [recall-src].
3. **Scrub the environment.** Remove `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`, `CODEX_API_KEY`, `OPENAI_API_KEY`, `CURSOR_API_KEY`, `GEMINI_API_KEY` from the child's env unless the user chose API-key mode. Reason: API keys outrank subscription login in `claude -p` and `codex exec` [cc-env][cx-src-auth].
4. **Do not load the user's MCP servers, hooks or custom instructions.** The child would otherwise start recall's own MCP server (recursion, lock contention) and run user hooks. Flags are in each section.
5. **No tools, or read-only.** Transcripts contain pasted web pages and other prompt-injection surface (recall's README says so). Extraction needs no tools.
6. **Never touch credentials.** Do not read `~/.codex/auth.json`, the macOS keychain item, or run `claude setup-token` on the user's behalf. Spawn the user's own unmodified binary and let it authenticate; this is the posture the vendors' texts leave room for (section 2.6) [cc-legal].
7. **Redact first.** Send only text that has passed recall's redaction (`src/recall/redact.py`), and tell the user what leaves the machine.
8. **Throttle.** Two to four workers, exponential backoff, stop on the first limit message and resume after the window resets.

### 2.1 Codex `codex exec` with `gpt-6-luna` (default)

**Auth and detection.**
- Two sign-in modes: ChatGPT (subscription) or API key. `codex exec` reuses saved CLI auth by default; the docs recommend API keys as the default for automation and treat ChatGPT-managed auth in automation as an advanced path [cx-auth][cx-exec].
- Credentials live in `~/.codex/auth.json` or the OS credential store (`cli_auth_credentials_store`) [cx-auth].
- Detect with `codex login status`; it exits 0 when credentials are present [cx-cli]. Example output: `Logged in using ChatGPT` [M].
- Source order: a `CODEX_API_KEY` env var beats every other credential in `exec` [cx-src-auth]. The docs show `OPENAI_API_KEY` being piped into `codex login --with-api-key` rather than read directly [cx-auth]. Scrub both anyway.
- Binary discovery for zero setup: PATH first, then `openai.chatgpt-*/bin/<platform>/codex` inside VS Code-family extension folders [M].

**Reference invocation.**

```bash
# prompt (instructions + sentinel + batched transcripts) arrives on stdin
env -u CODEX_API_KEY -u OPENAI_API_KEY \
codex exec - \
  --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules \
  -C ~/.agents/recall/scratch -s read-only \
  -m gpt-6-luna -c model_reasoning_effort=low \
  -c web_search=disabled --disable shell_tool \
  --output-schema schema.json -o out.json --json --color never < prompt.txt
```

| Flag | Why | Src |
| --- | --- | --- |
| `-` / stdin | `codex exec -` reads the whole prompt from stdin; avoids argv limits for large batches | [cx-exec][cx-src-exec] |
| `--ephemeral` | No session files. Source: in an ephemeral session no thread persistence object is created and no state DB is opened, and Codex's own memories pipeline is skipped | [cx-src-session][cx-src-memories] |
| `--skip-git-repo-check` | Codex refuses to run outside a git repo otherwise | [cx-exec] |
| `--ignore-user-config` | Skips `$CODEX_HOME/config.toml` (user MCP servers including recall's, custom instructions, profiles); auth still comes from `CODEX_HOME`. Pass `-m` explicitly because the user default model is also skipped | [cx-src-exec] |
| `--ignore-rules` | Skips user and project execpolicy rules | [cx-src-exec] |
| `-C DIR`, `-s read-only` | Working root and sandbox; read-only is already the `exec` default | [cx-src-shared][cx-exec] |
| `-m gpt-6-luna` | Cheap, fast tier for high-volume extraction (see "Model" below) | [oai-luna][cx-models] |
| `-c model_reasoning_effort=low` | `-c` values are parsed as TOML, falling back to a literal string, so an unquoted `low` works. Luna supports low to max in the Codex client | [M][cx-config] |
| `-c web_search=disabled`, `--disable shell_tool` | Remove tool definitions to shrink the prompt and the attack surface. `--disable X` equals `-c features.X=false` | [cx-config][M] |
| `--output-schema FILE` | Final answer constrained to a JSON Schema | [cx-exec] |
| `-o FILE` | Writes the final message to a file | [cx-src-exec] |
| `--json` | stdout becomes JSONL events; `turn.completed` carries `usage` (input, cached input, output, reasoning tokens) | [cx-exec] |

Gotchas:
- `codex exec -p` is `--profile`, not print mode [cx-src-shared][M].
- `--disable shell_tool` and `web_search=disabled` should shrink the tool block, but their effect on `usage.input_tokens` is [U]; check it in the first calibration call. A global `~/.codex/AGENTS.md`, if present, may still be injected under `--ignore-user-config`; whether `-c project_doc_max_bytes=0` suppresses it is [U] [cx-config][M].

**Structured output.** `--output-schema` uses OpenAI Structured Outputs semantics. Schema rules: the root must be an object (not `anyOf`); every property must be listed in `required` (emulate optional fields with `["string","null"]`); `additionalProperties: false` on every object; `enum`, `minItems`, `maxItems`, `pattern` are supported; at most 5,000 properties and 10 nesting levels [oai-so][cx-exec]. Luna lists structured outputs as a supported feature [oai-luna]. Still parse and validate, and retry once on failure.

**Model.**
- API facts for `gpt-6-luna`: described as the most efficient model for focused, high-volume tasks; reasoning effort none, low, medium (default), high, xhigh, max; 1,050,000 context window; $0.10 input, $0.01 cached input, $0.50 output per million tokens [oai-luna]. The Codex client itself caps the context at 272,000 (max 872,000) [M].
- Codex docs recommend Luna for extraction, classification and structured summaries, and suggest starting at High effort [cx-models]. We choose Low for speed and cost; the quality delta on recall's task is unmeasured [U].
- Measured by a third party on OpenAI's API: Luna at low effort is 135.6 output tok/s with a 2.40 s time to first token, and scores 22 on the Artificial Analysis Intelligence Index, against 15 for Claude Haiku 4.5 (non-reasoning). Index versions may differ between pages [aa-luna][aa-haiku].
- **Model churn is fast.** GPT-5.5 retires from Codex on 2026-10-14; `gpt-5.4`/`gpt-5.4-mini` retired 2026-08-31; `gpt-5.3-codex-spark` retired 2026-09-14 [cx-models]. Keep the model id configurable and use a fallback chain: `gpt-6-luna`, then `gpt-5.6-luna`, then no `-m` (account default). Read `~/.codex/models_cache.json` to see what the account actually exposes [M].
- Plan availability: Plus and above list Luna; Free and Go list it only for the desktop app, subject to rollout, so the CLI may not offer it there [cx-pricing].

**Per-call overhead.**
- The base instruction file in the Codex repo is 20,903 bytes, roughly 5K tokens, before tool definitions and any AGENTS.md [cx-src-prompt][D].
- The docs' sample `turn.completed` shows 24,763 input tokens, 24,448 of them cached, for a small repo-summary turn [cx-exec].
- Cached input is billed at 10% of the uncached rate, with no separate cache-write charge [cx-pricing]. So overhead is cheap after the first call; whether the cache hits across separate processes is [U]. Keep the instruction text and schema as a byte-identical prefix and put transcripts last.
- Process startup is unmeasured [U]; plan for 1 to 2 s per call until calibrated.

**Persistence and history.** Non-ephemeral runs write `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and a thread row that recall reads (`sessions/**`, `archived_sessions/**`, the `local_thread_catalog` table) [recall-src]. `--ephemeral` avoids both [cx-src-session]. Config `history.persistence = "none"` controls `history.jsonl` and can be added with `-c` as belt and braces [cx-config].

**Limits.**
- Plus and Standard Business: GPT-6 Luna 350 to 3,000 local messages per 5-hour window (estimates, not fixed); GPT-6.1 Sol 15 to 160; Astra 5 to 45. Pro has no 5-hour limit; weekly limits may apply; local messages and cloud tasks share the allowance [cx-pricing].
- Credits for overage: Luna 2.5 credits per M input, 0.25 cached, 12.5 output; Sol 6.1: 50 / 2.5 / 250. Rate parity with the API price implies about $0.04 per credit [cx-pricing][oai-luna][D].
- The docs say prompt length alone is a poor predictor of usage, so one large `exec` may count as more than one message [U].
- Limit errors have no documented format; recall should treat any non-zero exit or `turn.failed` / `error` event as retryable, back off, and pause until the next window [cx-exec].

**Terms and data.** See section 2.6. Short version: OpenAI's general terms forbid automatic or programmatic extraction of output and circumventing rate limits, while Codex docs explicitly support `codex exec` for scripts with saved ChatGPT auth. On personal plans, the `Improve the model for everyone` setting also applies to Codex tasks; opting out is in ChatGPT data controls or the Privacy Portal [oai-terms][cx-exec][oai-data].

**Failure modes to handle.** Not signed in (exit non-zero); model unavailable on the plan (fall back along the chain); schema rejected (strict-mode rules above); quota exhausted (pause); network errors (retry); invalid JSON despite the schema (one retry, then bisect the batch).

### 2.2 Claude Code `claude -p` (first fallback) and the Agent SDK

**Auth and detection.**
- `claude auth status` prints JSON and exits 0 when logged in [cc-cli]. Observed: logged in via claude.ai [M].
- Subscription credentials sit in the macOS Keychain, falling back to `~/.claude/.credentials.json` with mode 0600 [cc-auth]. recall must never read either.
- **API key hazard.** If `ANTHROPIC_API_KEY` is set, `-p` always uses it, and the call is billed to the API instead of the plan [cc-env][anth-cc-plan]. Scrub it from the child env (and `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_*`).
- **`--bare` is unusable on a subscription.** Bare mode never reads OAuth credentials or the keychain, so it needs an API key or `apiKeyHelper`. The docs also say bare mode is planned to become the default for `-p` in a future release; if that happens, subscription `-p` runs will need a way to opt out, so watch the release notes [cc-headless][cc-env].
- So isolate with explicit flags instead (recipe below).

**Reference invocation.**

```bash
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
    -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX -u CLAUDE_CODE_USE_FOUNDRY \
    CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
    CLAUDE_CODE_SKIP_PROMPT_HISTORY=1 CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 \
claude -p --model haiku --output-format json \
  --json-schema "$(cat schema.json)" --system-prompt-file system.txt \
  --tools "" --disable-slash-commands --strict-mcp-config --setting-sources= \
  --no-session-persistence --max-turns 3 < prompt.txt
```

| Flag / setting | Why | Src |
| --- | --- | --- |
| `--model haiku` | Alias for the current Haiku, which is Claude Haiku 4.5 at $1 in / $5 out per M tokens, 200K context. Aliases move; pin with the full id if needed | [cc-models][anth-pricing][aa-haiku] |
| `--output-format json` | Result envelope with `result`, `session_id`, `usage` and `total_cost_usd` (a client-side estimate) | [cc-headless] |
| `--json-schema` | Print mode only; validated JSON lands in `structured_output`. Schema `format` is treated as an annotation, not enforced | [cc-cli][cc-headless] |
| `--system-prompt-file` | Replaces the whole default prompt (the SDK passes `--system-prompt ""` by default) | [cc-cli][sdk-src] |
| `--tools ""` | Disables all built-in tools; MCP tools are separate | [cc-cli] |
| `--strict-mcp-config` | Ignores every MCP source except `--mcp-config` (so recall's own MCP is not started) | [cc-cli] |
| `--setting-sources=` (empty) | Loads no user, project or local settings; this is what the SDK sends for `setting_sources=[]`. Managed policy and `~/.claude.json` still load | [cc-cli][sdk-src][sdk-features] |
| `--disable-slash-commands` | No skills or commands | [cc-cli] |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `..._DISABLE_CLAUDE_MDS=1` | Auto memory and CLAUDE.md files are read regardless of setting sources unless disabled | [sdk-features][cc-env] |
| `--no-session-persistence` and `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` | No transcript on disk (flag is print-mode only; the variable works in any mode) | [cc-cli][cc-env] |
| `--max-turns 3` | Exits with an error at the limit. Structured output is delivered through a tool call and can be re-prompted, so 1 may be too low [U] | [cc-cli][sdk-structured] |

**Structured output behaviour.** Validation failures are re-prompted. When retries run out the result subtype is `error_max_structured_output_retries`; a `success` result with no `structured_output` must also be treated as a failure [sdk-structured].

**Per-call overhead.** Anthropic's own illustrative context breakdown lists a 4.2K-token system prompt, plus about 3.5K of auto memory, CLAUDE.md files, skill descriptions and environment text. The isolation above removes everything except our own prompt; built-in tool schemas are not published and `--tools ""` removes them [cc-ctx][D]. Expect roughly 0.5 to 2K tokens per call [U]. Calibrate from `usage` in the JSON result. Prompt caching is automatic; on a subscription the main conversation requests a 1-hour cache TTL, and `usage.cache_creation` shows which TTL was used [cc-cache]. Process start time is unmeasured; `claude --version` returns in 10 to 20 ms but never loads the app, so it is not representative [M][U].

**Persistence.** `-p` sessions are hidden from the session picker but are still written to `~/.claude/projects/<cwd-slug>/<id>.jsonl` unless suppressed [cc-sessions]; recall's reader globs `~/.claude/projects/*/*.jsonl` [recall-src]. Use the flag and the filter.

**Agent SDK (Python).** `claude-agent-sdk` wraps the same CLI and offers `system_prompt`, `tools=[]`, `setting_sources=[]`, `strict_mcp_config=True`, `output_format={"type": "json_schema", ...}`, `max_turns`, `model`, `env`, `extra_args` [sdk-python][sdk-src]. In Python, suppress transcripts through `env={"CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1"}` (the `persistSession` option exists only in TypeScript) [sdk-sessions]. It exposes `RateLimitEvent` with five-hour, seven-day and per-model utilisation, which is ideal for self-throttling [sdk-python]. Whether `claude -p --output-format stream-json` emits the same events is [U].
Recommendation: spawn the user's own `claude` binary directly rather than depending on the SDK. Anthropic's SDK overview says third-party developers may not offer claude.ai login or its rate limits in their products, and that includes agents built on the Agent SDK [sdk-overview][cc-legal]. A subprocess keeps recall out of the credential path.

**Limits.** Pro and Max limits are shared with claude.ai and use 5-hour and weekly windows [anth-cc-plan][sdk-python]. No token or message numbers are published. **Policy risk:** Anthropic announced that `claude -p` and Agent SDK usage would move to a separate monthly credit (Pro $20, Max 5x $100, Max 20x $200) on 2026-06-15, then paused it the same day; today `claude -p` still draws from subscription limits, and a future update will be announced first [anth-sdk-plan].

**Terms and data.** Section 2.6. Consumer accounts: training on content unless the user opts out, retention 5 years if allowed and 30 days if not [cc-data].

### 2.3 Cursor `agent -p`

- Binary `agent` (alias `cursor-agent`). Auth: `agent login`, or `CURSOR_API_KEY` for scripts; `agent status` shows state [cur-auth].
- Flags: `-p`, `--output-format text|json|stream-json`, `--model` (list with `agent models`), `--mode ask|plan` (read-only), `--sandbox enabled|disabled`, `--trust`, `--workspace`, `--resume`, `--force` [cur-params].
- Output: `json` gives `result`, `duration_ms`, `session_id`, `request_id` but no token counts; `stream-json` added per-turn input, output and cache token totals in February 2026 [cur-output][cur-changelog].
- **Missing controls.** The documented parameter list has no JSON-schema flag, no system-prompt flag, no tool-disable flag and no no-persist flag [cur-params]. The docs state print mode has access to all tools, including write and shell; edits only apply with `--force` [cur-params][cur-headless]. Mitigation: `--mode ask`, `--sandbox enabled`, `--trust --workspace <scratch>`, plus prompt-level JSON and a parse-and-retry loop.
- **Persistence.** Transcripts persist to disk, and headless transcripts are written as Claude Code-compatible JSONL [cur-changelog]. recall's Cursor reader indexes `~/.cursor/projects/*/agent-transcripts/*/*.jsonl` and derives the project from the folder slug [recall-src], so scratch-dir filtering is mandatory and cannot be replaced by a flag.
- **Cost.** Plans are dollar pools: Pro $20, Pro Plus $60, Ultra $200 per month, with a Cursor Models pool (Composer 2.5 at $0.50 in, $2.50 out per M; cache read $0.20) and an Other Models pool at API prices [cur-pricing]. 8M tokens on Composer 2.5 would be about $4.8 of pool [D].
- **Startup.** `agent --version` takes 1.0 to 1.6 s here (Node-based) [M].
- **Terms.** Cursor's terms (updated 2026-09-03) forbid harvesting, scraping or extracting data from the service and reverse engineering; they do not mention scripted CLI use, which the CLI docs encourage [cur-terms][cur-headless].
- Verdict: last resort. If used, treat the model as unconstrained and validate everything.

### 2.4 Gemini CLI, and its successor Antigravity (`agy`)

**Gemini CLI.**
- On 2026-06-18 the Gemini CLI team stopped serving Google AI Pro, Google AI Ultra and free individual accounts; enterprise Code Assist licences and API-key auth are unaffected [gem-disc][gem-dep].
- The repo's quota page still lists 1,000 requests a day for individual accounts, which is stale. The remaining route is an API key: free tier 250 requests a day, Flash only [gem-quota]. A key breaks the no-API-key goal.
- Flags for the remaining users: `-p`, `--output-format json|stream-json`, `-m`; exit codes 0, 1, 42 (input error), 53 (turn limit); JSON carries `response` and `stats` [gem-headless][gem-cli]. There is no schema flag in the flag table [gem-cli]. System prompt override via `GEMINI_SYSTEM_MD` [gem-sysmd]. Sessions are saved under `~/.gemini/tmp/<hash>/chats/` [gem-session].
- Google's terms for the CLI call third-party tools that drive the OAuth-backed service a violation [gem-tos].

**Antigravity CLI (`agy`).**
- Closed-source Go binary installed by script; signs in through the system keyring with Google sign-in as fallback; headless runs use cached credentials and fail with an authentication error rather than prompting [agy-readme][agy-headless].
- Headless: `agy -p`, `--output-format text|json|stream-json`, `--json-schema` (schema string, file, or primitive type; result in `structured_output`), `--model`, `--continue`; JSON includes a `usage` object [agy-headless].
- Overhead: the docs' one-sentence example used 10,415 input tokens, 8,113 of them cached; a stream example used 30,384 [agy-headless]. Model errors exit with code 3 and print an `AGY_ERROR` JSON line on stderr [agy-changelog].
- Quota: free users get a weekly-refreshed allowance described only as meaningful, with no numbers; Pro and Ultra get five-hour windows plus weekly caps; Gemini 3.8 Flash is available on the free tier [agy-plans][agy-models].
- Data: using the CLI means agreeing that Google may collect interaction data to improve the product; opt-out is in settings [agy-readme].
- recall does not index Antigravity data today, so its runs cannot pollute the index [recall-src]; where `agy` stores headless conversations is [U].
- Verdict: optional third-tier backend when already installed and signed in; never a default.

### 2.5 Comparison

| | Codex (default) | Claude Code | Cursor | Antigravity |
| --- | --- | --- | --- | --- |
| Zero-setup here | Yes: bundled binary, signed in [M] | Yes: PATH, signed in [M] | No: not signed in [M] | No: not installed [M] |
| Cheap model | `gpt-6-luna` | `haiku` (4.5) | Composer 2.5 or other pool model | Gemini 3.8 Flash |
| JSON schema flag | `--output-schema` | `--json-schema` | none | `--json-schema` |
| No-persist | `--ephemeral` | `--no-session-persistence` | none | unknown |
| Tools off | `--disable shell_tool`, `web_search=disabled`, `-s read-only` | `--tools ""` | `--mode ask` only | unknown |
| Ignore user config / MCP | `--ignore-user-config` | `--strict-mcp-config`, `--setting-sources=` | none documented | unknown |
| Token usage in output | `--json` `turn.completed.usage` | JSON `usage`, `total_cost_usd` | `stream-json` only | JSON `usage` |
| Published quota | Luna 350 to 3,000 msgs / 5 h (Plus) | none (opaque) | dollar pool | none for free |
| Env key trap | `CODEX_API_KEY` | `ANTHROPIC_API_KEY` (set here) | `CURSOR_API_KEY` | `GEMINI_API_KEY` |
| Main risk | model churn; gray ToS; training default | gray ToS; possible credit change; training default | no controls | data collection; unpublished quota |

All rows: [cx-exec][cx-cli][cx-pricing][cc-cli][cc-env][cur-params][cur-output][agy-headless][agy-plans][M].

### 2.6 Terms of service and data handling for scripted use of consumer plans

Not legal advice; wording paraphrased.

| Vendor | What the texts say | Reading for recall | Src |
| --- | --- | --- | --- |
| Anthropic | Consumer Terms (effective 2025-10-08) section 3 bars accessing the services by bot, script or other automated means, except through an API key or where Anthropic explicitly permits it. Claude Code docs say OAuth login is for ordinary use of Claude Code and other native apps, say Pro and Max limits assume "ordinary, individual usage", and say developers building products on the Agent SDK should use API keys; third parties may not offer claude.ai login or route requests through users' plan credentials. The same page says none of this stops an end user from signing in to the unmodified Claude Code binary with their own plan | `claude -p` is a documented mode of the official binary, run by the user on their own machine. A bulk 8M-token job is not ordinary use, and the explicit-permission carve-out for batch use is not spelled out. Safest posture: opt-in, throttled, spawn the user's own binary, never touch tokens, never offer it as a service | [anth-terms][cc-legal][sdk-overview] |
| OpenAI | Terms of Use (effective 2026-01-01) forbid automatically or programmatically extracting data or output and circumventing rate limits. Codex docs, however, document `codex exec` for scripts, reusing saved ChatGPT auth, and say API keys are the better default for automation; the ChatGPT-token-in-CI path is marked advanced and not for public repos | Product docs endorse the use; the general clause is broad. Same posture as above | [oai-terms][cx-exec] |
| Cursor | Terms (updated 2026-09-03) bar harvesting or scraping data from the service, benchmarking disclosure and model extraction; silent on scripted CLI use | CLI docs promote scripting. Low risk, but usage burns the plan's dollar pool | [cur-terms][cur-headless] |
| Google | Gemini CLI consumer access ended; third-party tools using the OAuth-backed service are called a violation. Antigravity terms (JS-rendered page, not read) derive from Google's ToS; README says interaction data is collected | Do not use consumer Gemini paths from recall | [gem-tos][agy-readme] |

**Training and retention on personal plans.**
- OpenAI: the `Improve the model for everyone` setting also applies to Codex tasks on personal ChatGPT plans; turn it off in ChatGPT or via the Privacy Portal [oai-data].
- Anthropic: consumer data is used for training when the setting is on, including Claude Code use; retention is 5 years if allowed, 30 days if not [cc-data].
- **Cross-vendor disclosure.** README promises nothing leaves the machine. Sending a Claude-origin chat to OpenAI, or the reverse, discloses it to a vendor that has not seen it. Options: (a) vendor-affine routing, sending each chat to the CLI of the tool that produced it; (b) one vendor for all, with explicit consent naming the vendor. Cursor chats already live with Cursor and its model providers. This is a product decision for the maintainers.

## 3. Part 2: local backends (deprioritised, opt-in)

### 3.0 Status and rules if a user opts in

**Why they are deprioritised.**
- After our benchmarks the user reported the local model crashing their machine (cause undiagnosed; see the decision log).
- What we measured was slow and noisy. Qwen3-8B Q4_K_M through Ollama generated 21 to 35 tok/s, and prompt processing varied between 199 and 617 tok/s run to run [M]. The Apple on-device model generated about 29 tok/s [M].
- The resident Ollama model held 6.3 to 6.6 GB at 16,384 context, 100% on GPU, on a 24 GiB machine that was also running other apps [M].
- A full first run is hours to most of a day (section 5.3), against about an hour on a subscription CLI.

**Opt-in guard rails** (engineering judgment, not from a source): require an explicit setting such as `backend = "ollama"`; never auto-start at login or from the nightly schedule unless the machine is idle and on AC power; check free memory against model size plus KV cache plus about 8 GB before loading; cap context at 8K; one request at a time; time-box runs into resumable 15 to 20 minute slices; unload the model afterwards (Ollama `keep_alive: 0`); show the user the expected duration first.

**Platform reach.** Ollama, LM Studio and llama.cpp run on macOS, Linux and Windows; MLX is macOS plus Linux CPU or CUDA extras (no Windows extra exists); Apple's model is macOS only [ollama-lib][lms-headless][llcpp-install][mlx-pypi][afm-model].

### 3.1 Ollama

- **Detect.** `GET /` returns `Ollama is running`; `GET /api/version` returns the version; `GET /api/tags` lists installed models; `GET /api/ps` lists loaded ones [ollama-routes]. Default bind is 127.0.0.1:11434, changed by `OLLAMA_HOST` [ollama-faq]. Verified here [M].
- **API.** `POST /api/chat` takes `format` (the string `json` or a JSON Schema), `options` (including `num_ctx`), `think`, and `keep_alive`; OpenAI-compatible routes are `/v1/chat/completions` and `/v1/models` [ollama-api][ollama-so][ollama-routes]. Cloud models and cloud structured outputs are out of scope; the docs note structured outputs are not supported on Ollama Cloud [ollama-so].
- **Structured output.** Pass the schema in `format`; the docs advise also putting the schema text in the prompt [ollama-so]. On Qwen3-8B, 13 of 13 schema-constrained calls returned valid JSON [M]. Use `think: false` for non-reasoning extraction [ollama-api].
- **Context trap.** The FAQ says the default window is 4,096 tokens; a newer page says defaults depend on memory (under 24 GiB: 4k, 24 to 48 GiB: 32k, 48 GiB or more: 256k). Set `options.num_ctx` on every request. RAM needed scales with parallel slots times context, and `OLLAMA_NUM_PARALLEL` defaults to 1 [ollama-faq][ollama-ctx].
- **MLX runner.** Structured-output support in the MLX runner landed in v0.33.1 (2026-08-26). Release candidate v0.40.0-rc5 (pre-release, 2026-09-25) makes MLX the default engine on Apple Silicon for supported architectures, naming qwen3.5, qwen3.6 and gemma4. On stable 0.35.x, `qwen3.5:4b` runs on llama.cpp unless an `-mlx` tag is pulled [ollama-rel][ollama-lib]. The engine will change under recall; do not assume one.
- **Prompt cache.** Re-sending an identical prompt reported its token count with near-zero prefill time, so the KV prefix is reused [M].
- **Library sizes.** `qwen3.5:4b` 3.3 to 4.0 GB; `qwen3.5:9b` 6.6 to 7.6 GB; `gemma4:e4b` 6.6 to 9.5 GB; `gemma4:12b` 7.7 to 8.0 GB; `granite4.2:3b` 2.2 GB [ollama-lib].
- **Measured on one Mac** (Ollama 0.35.0, `qwen3:8b` Q4_K_M, synthetic 2,000 to 6,600-token prompts, other apps running) [M]: prompt processing 199 to 269 tok/s over 9 schema-constrained calls with unique prompts, and 430 to 617 tok/s over 4 earlier unconstrained calls; generation 21 to 35 tok/s, median 28.9 over the 9 calls. The 2x spread between those two prompt-processing sets is unexplained; contention with the other client using the resident model is the leading suspect [U].

### 3.2 llama.cpp and llama-cpp-python

- **Install.** README offers a unified installer (`curl -LsSf https://llama.app/install.sh | sh`, giving `llama cli` and `llama serve`), Docker, prebuilt archives, and source builds; `install.md` lists Homebrew, conda-forge, winget, MacPorts and Nix [llcpp-readme][llcpp-install]. Release b11438 (2026-10-06) ships `macos-arm64` 11 MiB, `ubuntu-x64` 16 MiB, `win-cpu-x64` 18 MiB, `win-vulkan-x64` 31 MiB, plus CUDA 12.8/13.4, ROCm, SYCL and OpenVINO builds [llcpp-rel]. Metal is on by default on macOS [llcpp-build].
- **Server.** `llama-server` binds 127.0.0.1:8080, offers `GET /health`, `-hf user/model[:quant]` to download from Hugging Face (default quant Q4_K_M), `-np` parallel slots, and has continuous batching and prompt caching enabled by default [llcpp-server].
- **Constrained decoding.** GBNF via `--grammar`/`--grammar-file`; JSON Schema via `-j`/`-jf`, a per-request `json_schema` field, or OpenAI-style `response_format` (`json_object` with a `schema`, or `json_schema`) [llcpp-server][llcpp-gbnf]. The schema is not shown to the model, so describe the structure in the prompt. The supported subset has gaps (no `uniqueItems`, `not` or conditionals; `pattern` must be anchored; `properties` cannot be mixed with `anyOf`) and unsupported keywords are skipped silently [llcpp-gbnf].
- **llama-cpp-python: not recommended.** PyPI 0.3.36 (2026-10-01) is a 76.6 MB source distribution only, so `pip install` compiles C/C++ [llcpppy]. Prebuilt wheels exist only on extra indexes; the Metal index stops at 0.3.19 with CPython 3.9 to 3.12 wheels (none for 3.13 or 3.14), and the CPU index also stops at 0.3.19 [llcpppy][llcpppy-idx]. recall needs Python 3.11 or newer, and the wheels above do not cover 3.13 or 3.14, so talk to `llama-server` over HTTP instead.

### 3.3 MLX and mlx-lm

- **Install and models.** `pip install mlx-lm` (or conda-forge). Version 0.32.0 (2026-10-01), MIT, Python 3.11 or newer; on macOS it depends on `mlx>=0.32.2`, Linux needs the `cpu`, `cuda12` or `cuda13` extra [mlx-pypi][mlx-readme]. Models download from the Hugging Face Hub on first use; `mlx-community` hosts pre-quantised repos [mlx-readme][mlx-server].
- **Server.** `mlx_lm.server` (default port 8080) is OpenAI-style, documented as not production-grade; `max_tokens` defaults to 512; the request fields list has no `response_format` or schema, and a quantised KV cache turns batching off [mlx-server]. A search of `mlx_lm/server.py` on the main branch found no `response_format`, `json_schema` or grammar handling [M][mlx-server].
- **Structured output options.** Custom `logits_processors` [mlx-readme]; Outlines (`outlines.from_mlxlm`) [outlines-mlx]; XGrammar's `contrib/mlxlm.py` with a Metal token-mask kernel [xgrammar-mlx]; LM Studio's `mlx-engine` (Outlines) [mlx-engine]. Outlines compiles schemas in 3.5 to 8 s and some keyword types took 40 s to 10 min in JSONSchemaBench [jsb]. The dependency-free route is JSON in the prompt, validate, one retry.
- **Hardware note.** M5 GPUs get roughly 3 to 4x faster prefill from MLX on macOS 26.2 or newer; M4 does not [apple-m5].

### 3.4 LM Studio

- OpenAI-compatible server at `http://localhost:1234/v1` (`GET /v1/models` lists models); `lms` CLI ships with the app and needs the app run once; commands `lms server start`, `lms get`, `lms ls`, `lms ps`, `lms load --context-length N`; the CLI is MIT-licensed [lms-so][lms-cli][lms-headless].
- Headless: the standalone `llmster` daemon (install script, then `lms daemon up`) with just-in-time model loading [lms-headless].
- Structured output via `response_format` with `json_schema`; GGUF models use llama.cpp grammars, MLX models use Outlines. The docs caution that models under 7B may not handle structured output well [lms-so].
- Free for home and work use since 2025-07-08 [lms-free].

### 3.5 Apple Foundation Models

- **Availability.** `SystemLanguageModel.default.availability` is `.available` or `.unavailable` with `.deviceNotEligible`, `.appleIntelligenceNotEnabled` or `.modelNotReady` [afm-model][afm-iface]. Measured: available [M].
- **Context window.** Apple documents 4,096 tokens per session, input plus output [afm-ctx]. `contextSize` (since 26.4) returns the real value and `tokenCount(for:)` counts tokens [afm-updates][afm-iface]. Measured on macOS 27.0: `contextSize` is **8,192**, variant `AFM 3 Core Advanced` [M]. apfel documents 8,192 only on macOS 27 with an M3 or newer and 12 GB or more, and 4,096 on macOS 26 and on M1/M2 [apfel]. Read it at runtime; never hard-code. The Private Cloud Compute variant has 32K but needs a managed entitlement, a network and has daily limits, so it is not usable from a CLI [afm-pcc].
- **Guided generation without Xcode.** The `@Generable` and `@Guide` macros need a macro plugin that ships with Xcode: with Command Line Tools only, compilation fails with a missing `FoundationModelsMacros` plugin [M]. The runtime path works: `DynamicGenerationSchema`, `GenerationSchema(root:dependencies:)`, then `session.respond(to:schema:)`, whose `GeneratedContent` has `jsonString`. It compiled with CLT `swiftc` into a 66 KB binary (about 49 s for the first compile) and completed 6 of 6 guided calls (one warm-up, five timed) without error [M][afm-iface]. On macOS 27 each response exposes input and output token usage [afm-iface].
- **Python SDK.** `apple-fm-sdk` (Apache-2.0, v0.2.1, 2026-06-29, sdist only) compiles Swift bindings at install time and refuses to build unless a full Xcode 26 or newer is active; Command Line Tools are rejected [afm-py]. A Mac with only Command Line Tools would fail the build [M].
- **Community CLI.** `apfel` (MIT, `brew install apfel`, builds with CLT): one-shot CLI with `--schema` guided JSON and `--count-tokens`, plus an OpenAI-compatible server on port 11434, which collides with Ollama's port [apfel]. A tiny Swift helper is the third route (Appendix B).
- **Limits.** Safety guardrails may block benign prompts; `Guardrails.permissiveContentTransformations` exists for text-transformation tasks [afm-safety][afm-iface][apfel]. No embeddings, about 3B parameters, one fixed model [apfel]. Errors to handle: `contextSizeExceeded`, `guardrailViolation`, `refusal`, `rateLimited` [afm-iface].
- **Measured** [M]: prompt processing 586 tok/s (456-token prompt), 879 (1,366), 995 (2,731), 788 (4,096); generation 28.5 to 29.6 tok/s; guided-generation calls took 6.3 to 9.0 s for 365 to 3,250-token prompts with 115 to 189 output tokens. The schema and instructions cost 230 tokens of the window (163 + 67). Extraction quality was not evaluated; the one sample, on synthetic text, filed a redaction fact under `lesson`.

### 3.6 Constrained decoding across the backends

| Backend | Mechanism | Takes JSON Schema | Notes | Src |
| --- | --- | --- | --- | --- |
| Ollama | `format` | Yes | llama.cpp grammar on GGUF; XGrammar in the MLX runner | [ollama-so][ollama-rel] |
| llama.cpp server | GBNF; `response_format`; `json_schema` | Yes (subset) | Silent skips for unsupported keywords | [llcpp-gbnf][llcpp-server] |
| LM Studio | `response_format.json_schema` | Yes | llama.cpp for GGUF, Outlines for MLX | [lms-so] |
| mlx-lm | none native | No | Outlines or XGrammar add-ons | [mlx-server][outlines-mlx][xgrammar-mlx] |
| Apple model | guided generation | Via `DynamicGenerationSchema` (apfel `--schema`) | CLT-only builds cannot use the macros | [afm-iface][apfel][M] |

Evidence from JSONSchemaBench (10K real-world schemas, Llama-3.2-1B-Instruct, llama.cpp as the inference engine) [jsb]:
- Share of schemas where the output validated: simple function-call schemas (GlaiveAI) 0.90 unconstrained vs 0.95 llama.cpp, 0.96 Guidance, 0.95 Outlines, 0.93 XGrammar; hard schemas (JsonSchemaStore) 0.21 unconstrained vs 0.38 llama.cpp, 0.30 Guidance, 0.33 XGrammar, 0.09 Outlines (timeouts).
- Overhead: llama.cpp grammar compile about 0.05 s and about 27 to 30 ms per output token against 15 to 17 ms unconstrained in their setup; Outlines compile 3.5 to 8 s per schema.
- Constraining did not hurt quality; it moved three reasoning tasks by +0 to +2.3 points for llama.cpp.
- For recall's flat schema (array of `kind`, `text`, `evidence`) compliance should be near-perfect. Keep arrays bounded (`maxItems`) and still validate.

### 3.7 Small-model candidates and throughput

**Candidates, newest first.** The models named in the original brief (Qwen3 2507, Phi-4-mini, Llama 3.2, SmolLM3, Gemma 3n) are now one to two generations old [hf-api].

| Model | Params | License | Context | 4-bit download | Notes | Src |
| --- | --- | --- | --- | --- | --- | --- |
| Qwen3.5-4B | 4.66B (hybrid Gated DeltaNet and attention, multimodal) | Apache-2.0 | 262,144 | MLX 3.03 GB; Ollama 3.3 to 4.0 GB | Thinking is on by default; disable with `enable_thinking=false` (Ollama `think:false`); vendor non-thinking sampling temp 0.7, top_p 0.8, top_k 20, presence 1.5. Vendor scores (thinking mode): IFEval 89.8, IFBench 59.2, LongBench v2 50.0 | [hf-qwen35][hf-api][ollama-lib] |
| Qwen3.5-9B | 9.65B | Apache-2.0 | 262,144 | MLX 5.95 GB; Ollama 6.6 to 7.6 GB | IFEval 91.5, IFBench 64.5, LongBench v2 55.2 | [hf-qwen35][hf-api][ollama-lib] |
| Gemma 4 E4B-it | 4.5B effective (8B with embeddings) | Apache-2.0 | 128K | MLX 5.15 GB; official QAT Q4_0 GGUF 5.15 GB plus 0.99 GB mmproj | Different family and failure modes; native system role | [hf-gemma4][hf-api] |
| Gemma 4 12B-it | 11.95B | Apache-2.0 | 256K | MLX 6.74 GB; QAT Q4_0 GGUF 6.98 GB | Heavier than Qwen3.5-9B for no clear gain | [hf-gemma4][hf-api] |
| Granite 4.2 3B | 3.66B | Apache-2.0 | 131,072 | IBM MLX q4 2.06 GB; Ollama 2.2 GB | Text-only; the low-RAM option | [hf-api][ollama-lib] |
| Qwen3-4B-Instruct-2507 | 4.02B | Apache-2.0 | 262,144 | MLX 2.26 GB | Non-thinking by default; superseded | [hf-api] |
| Qwen3-8B | 8.19B | Apache-2.0 | 40,960 | MLX 4.61 GB; Ollama 5.2 GB | Hybrid thinking; benchmarked in this research | [hf-api][ollama-lib][M] |
| Phi-4-mini-instruct | 3.84B | MIT | 131,072 | MLX 2.16 GB | 2025 model | [hf-api] |
| SmolLM3-3B | 3.08B | Apache-2.0 | 65,536 in config | MLX 1.73 GB | Behind Qwen3.5-4B | [hf-api] |
| Llama-3.2-3B-Instruct | 3.21B | Llama 3.2 community license, gated | not in API metadata | MLX 1.81 GB | Gated weights need Hugging Face approval | [hf-api] |
| Gemma 3n E4B | 7.85B raw | Gemma license, gated | not in API metadata | MLX 3.86 GB | Superseded by Gemma 4 | [hf-api] |

**Local picks if a user opts in.** Extraction: Qwen3.5-4B, non-thinking, schema-constrained. Consolidation on 24 GB or more: Qwen3.5-9B. Alternative family: Gemma 4 E4B. 8 GB Macs: Granite 4.2 3B or Qwen3.5-2B (MLX 1.72 GB). No benchmark exists for memory extraction on any of these; build a small hand-labelled eval from the user's own chats, in the style of `evals/cases.jsonl`.

**Throughput on an M4 Pro with 16 GPU cores and 24 GB (the research Mac's class).**

| Model, quant, runtime | Prompt processing tok/s | Generation tok/s | Src |
| --- | --- | --- | --- |
| Llama-3.2-3B Q4, llama.cpp b9630 | 864 to 919 (prompt 128 to 2,048) | 102 | [baseRT] |
| Llama-3.2-3B Q4, MLX 0.31.2 | 879 to 942 | 112 | [baseRT] |
| Gemma-4-E2B Q4, llama.cpp | 1,209 to 1,292 | 107 | [baseRT] |
| Gemma-4-26B-A4B Q4, llama.cpp / MLX | 558 to 609 / 639 to 664 (prompt 512 to 2,048) | 58.0 / 69.3 | [baseRT] |
| Qwen3-30B-A3B Q4, llama.cpp / MLX | 601 to 668 / 664 to 726 (prompt 512 to 2,048) | 80.7 / 83.1 | [baseRT] |
| Llama-2-7B Q4_0, llama.cpp (older commit 8e672ef) | 364 | 49.6 | [llcpp-4167] |
| Qwen3-8B Q4_K_M, Ollama 0.35.0, contended | 199 to 269 and 430 to 617 | 21 to 35, median 28.9 | [M] |
| Apple on-device model (AFM 3 Core Advanced) | 586 to 995 | 28.5 to 29.6 | [M] |
| For scale, M4 Max 128 GB, 4-bit, mlx-lm / llama.cpp | not reported | Qwen3-4B 128.9 / 118.2; Qwen3-8B 79.9 / 76.9 | [vllm-mlx] |

- BaseRT is a runtime vendor, but its baselines are stock llama.cpp and mlx-lm with default settings on the same chip class as the research Mac [baseRT].
- **Derived, Qwen3.5-4B and 9B on this chip** [D]: scale Llama-3.2-3B by parameter ratio. Best case on an idle machine: 4B about 600 to 700 prompt and 75 to 85 generation tok/s; 9B about 280 to 330 prompt and 40 to 50 generation tok/s. The contended Qwen3-8B result above is the realistic floor.
- Chip scaling from the llama.cpp thread (7B Q4_0): M4 base 10-core 221 prompt / 24.1 generation; M4 Max 40-core 886 / 83.1; M5 Pro 20-core 1,621 / 66.3 [llcpp-4167].

## 4. Embeddings for dedupe and semantic search

Embeddings stay local in every recommendation: no embedding is sent to a vendor. They are cheap on CPU, so they do not carry the memory pressure of an LLM.

| Option | Size | Dependencies | CPU speed (i7-13700K, relative) | Quality | License | Src |
| --- | --- | --- | --- | --- | --- | --- |
| model2vec `potion-base-8M` | 30 MB (7.6M params) | numpy, tokenizers, safetensors, jinja2, joblib, tqdm; 60 KB pure-Python wheel | about 124K sentences/s | MTEB avg 51.1, retrieval 31.1 | MIT | [m2v][m2v-results][hf-api][hf-static] |
| model2vec `potion-base-32M` | 129 MB | same | similar | 52.1 / 32.7 | MIT | [m2v-results][hf-api] |
| model2vec `potion-retrieval-32M` | 129 MB | same | similar | retrieval 35.1, which is 81.7% of MiniLM | MIT | [m2v-results][hf-api] |
| `all-MiniLM-L6-v2` | 91 MB (22.7M params) | PyTorch via sentence-transformers, or ONNX | about 1.7K sentences/s | MTEB avg 55.9, retrieval 42.9; NanoBEIR NDCG@10 0.562 | Apache-2.0 | [m2v-results][hf-static][hf-api] |
| `bge-small-en-v1.5` via fastembed | 67 MB quantised ONNX (133 MB fp32); 33.4M params | fastembed 0.8.1: onnxruntime, numpy, tokenizers, huggingface-hub, pillow, requests, loguru, mmh3, py-rust-stemmers, tqdm | about 0.9K sentences/s (fp32) | NanoBEIR NDCG@10 0.627 | MIT | [fe][fe-src][hf-static][hf-api] |
| EmbeddingGemma-300M | 1.2 GB | n/a | n/a | n/a | Gemma terms; gated, manual approval | [hf-api] |
| Qwen3-Embedding-0.6B | 1.19 GB bf16 | n/a | n/a | n/a | Apache-2.0 | [hf-api] |

- Speeds are from the sentence-transformers static-embeddings post, run on an x86 desktop CPU, not Apple silicon; use them for ordering, not absolutes [hf-static]. `potion-base-8M` scored 0.442 on NanoBEIR in the same post, against 0.627 for bge-small and 0.452 for plain BM25 [hf-static].
- fastembed's own pitch is ONNX Runtime instead of gigabytes of PyTorch [fe]. sentence-transformers therefore is the heavy option; avoid it.
- **Recommendation.** Ship `potion-retrieval-32M` through model2vec as the default: numpy-class dependencies, 129 MB, fast enough to embed every memory sentence in seconds. Use it for near-duplicate detection and clustering of memories, and for fuzzy search fused with the existing FTS5 BM25 ranking (reciprocal rank fusion). Static embeddings are weaker than transformers on long chat chunks, so embed short memory strings, not whole chats. Offer `fastembed` plus `bge-small-en-v1.5` as the quality tier. Brute-force cosine in numpy is enough: 100K vectors at an assumed 512 dimensions in float16 is about 100 MB [D]. EmbeddingGemma needs gated access and is not zero-setup.

## 5. Recommendation for recall

### 5.1 Backend selection order (auto-detect)

Detection is read-only and makes no model call. Anything that spends quota or sends data needs a one-time consent screen that names the vendor, the token volume, the quota or credit cost, and the vendor's training setting (sections 2.0 and 2.6).

| Order | Backend | Eligible when | How recall detects it |
| --- | --- | --- | --- |
| 0 | Explicit setting | `backend = ...` is set; `none` parks chats in a pending queue | config |
| 1 | **Codex `exec`, `gpt-6-luna`, low effort (default)** | A `codex` binary exists and `codex login status` exits 0 in ChatGPT mode | `codex` on PATH, else `~/.cursor/extensions/openai.chatgpt-*/bin/<os-arch>/codex` or the same under `~/.vscode/extensions`; then `codex login status`; model choice from `~/.codex/models_cache.json` if present (first of `gpt-6-luna`, `gpt-5.6-luna`), else omit `-m`. The cache file is an internal Codex format [U] [M][cx-cli] |
| 2 | Claude Code `claude -p --model haiku` | `claude auth status` exits 0 and reports `authMethod` claude.ai | `claude` on PATH; scrub API-key variables; stronger consent text because of the terms position [M][cc-cli][cc-legal] |
| 3 | Antigravity `agy -p` | Only by explicit setting | Binary present and signed in; there is no side-effect-free auth probe documented [U] [agy-headless] |
| 4 | Cursor `agent -p` | Only by explicit setting | `agent status` shows signed in; last resort [cur-auth] |
| 5 | Local (opt-in): Ollama, LM Studio, llama-server, Apple model, mlx-lm | Only by explicit setting, after the guard rails in 3.0 | Ollama: `GET /` says `Ollama is running`, then `GET /api/tags`; LM Studio: `GET :1234/v1/models`; llama-server: `GET :8080/health`; Apple: macOS 26 or newer and `availability == .available`, read `contextSize` at runtime; mlx-lm last because it needs a pip install and has no schema support [ollama-routes][lms-headless][llcpp-server][afm-model][mlx-server] |
| 6 | Nothing available | Always the fallback | Queue chats as pending and print the one-line way to enable a backend; never skip silently |

- **Why Codex first.** Cheapest per token by a wide margin (about $1 API-equivalent for the whole first run against about $9.5 on Haiku), higher output speed than Haiku (135.6 against 91.4 tok/s, though a slower first token: 2.4 s against 0.7 s), a published per-window message allowance, a schema flag, a no-persistence flag verified in source, and the Codex binary ships inside the VS Code-family extension (see binary discovery above) [aa-luna][aa-haiku][cx-pricing][cx-src-session][M].
- **Why Claude second.** Best-controlled CLI (schema, tools off, system prompt replace, settings isolation), but the terms position and the paused credit change are more exposed, and the API-key trap applies whenever `ANTHROPIC_API_KEY` is set [cc-legal][anth-sdk-plan][M].
- **Vendor-affine option** for the privacy promise: route each chat to the CLI of the tool that produced it (Codex chats to Codex, Claude chats to Claude), so no vendor sees a transcript it has not already seen. Cursor chats need a separate decision. If maintainers prefer one vendor for everything, say so in the consent text.
- **Never prefer a local model silently** just because it is installed. Ollama running with a model resident was the configuration in use when the crash was reported (no causal link has been established).

### 5.2 Routing: extraction versus consolidation

1. **Extraction (many calls, per batch of chats): cheapest fast model.** Luna at low effort; Haiku 4.5 as fallback; a 4B-class local model only by opt-in. Output is schema-constrained, bounded (`maxItems`), and carries a short verbatim `evidence` quote that recall checks by substring against the source chat. Reject memories whose evidence is not found; this is a cheap guard against invented memories.
2. **Local, no LLM: embed, dedupe, cluster.** Use `potion-retrieval-32M` on every candidate memory (section 4). Merge near-duplicates above a similarity threshold and cluster the rest, so the next stage sees clusters, not thousands of items.
3. **Consolidation (few calls, nightly): stronger model.** `gpt-6.1-sol` at low or medium effort (its cached default is low [M]) or Claude Sonnet 5.5 via `--model sonnet` ($2 in, $10 out per M) [anth-pricing]. Only clusters that changed, plus the current `MEMORY.md`, go in. A call with 100K input and 20K output costs about 10 Codex credits on Sol 6.1 (100K at 50 plus 20K at 250 credits per M) or about $0.40 on Sonnet 5.5 [cx-pricing][anth-pricing][D]. Plus allows 15 to 160 Sol messages per window, so keep it to about ten calls a night [cx-pricing].
4. **Escalation.** If a batch fails schema validation twice, bisect it; if a single chat still fails, send it once to the stronger model; cap escalations per night.
5. **Provenance.** Store `chat_id`, evidence quote, `prompt_version`, `schema_version`, backend and model with every memory so a prompt or model change can re-run only what it affects.

Illustrative extraction schema (inside OpenAI's strict subset, so it also works with Claude's `--json-schema` and llama.cpp grammars) [oai-so]:

```json
{"type":"object","additionalProperties":false,"required":["chats"],"properties":{"chats":{"type":"array","items":{
 "type":"object","additionalProperties":false,"required":["chat_id","memories"],"properties":{
  "chat_id":{"type":"string"},
  "memories":{"type":"array","maxItems":8,"items":{"type":"object","additionalProperties":false,
   "required":["kind","text","evidence","confidence"],"properties":{
    "kind":{"type":"string","enum":["decision","preference","fact","lesson"]},
    "text":{"type":"string"},"evidence":{"type":"string"},
    "confidence":{"type":"string","enum":["low","medium","high"]}}}}}}}}}
```

### 5.3 Wall-clock and quota estimates for the first run

**Assumptions.** 8.0M input tokens of redacted text; about 2,700 chats, so about 3K tokens per chat on average (README baseline count against 8M); cloud calls packed to about 40K tokens, so about 200 calls; extraction output about 1.5K tokens per call (0.3M total); harness overhead per call about 8K tokens for Codex, 2K for Claude and 10K for Antigravity, mostly cached [U]; token speeds and time to first token from the Artificial Analysis pages [aa-luna][aa-haiku]; plan limits and prices from the vendor pages cited in section 2. All [D] unless a [M] tag is shown.

| Backend | Calls | Billed tokens (new in / cached in / out) | Cost basis | Wall-clock, one worker | Four workers | Quota position |
| --- | --- | --- | --- | --- | --- | --- |
| **Codex `gpt-6-luna`, low** | 200 | 8.0M / 1.6M / 0.3M | about 24 credits (20.0 + 0.4 + 3.75), about $0.97 API-equivalent | 50 to 67 min (15 to 20 s per call: 2.4 s first token, 1.5K output at 135 tok/s, plus startup and prefill) | 13 to 17 min | 200 messages against 350 to 3,000 per 5-hour window on Plus; fits one window even at the low end [cx-pricing] |
| Claude `haiku` (4.5) | 200 | 8.0M / 0.4M / 0.3M | about $9.5 API-equivalent (8.0 at $1, 0.4 at $0.10, 0.3 at $5) | 67 to 77 min (20 to 23 s per call) | 17 to 20 min | No published numbers. $9.5 is about 0.7 of Anthropic's quoted $13 average API-equivalent developer day [cc-costs]; if `claude -p` moves to the paused credit model it would use about half of Pro's $20 credit [anth-sdk-plan] |
| Cursor (Composer 2.5) | 200 | 8.0M in / 0.3M out | about $4.8 of the Cursor Models pool | unmeasured; guess 85 to 135 min | unmeasured | Dollar pool of the plan (Pro $20 a month) [cur-pricing] |
| Antigravity free tier | 200 | 8.0M / 2.0M / 0.3M | free | unmeasured; guess 65 to 100 min | unmeasured | Weekly allowance unpublished; may stop midway [agy-plans] |
| Local, Apple model (macOS 27, 8K window) | about 3,200 (2.5K chunks) | 8.0M in plus 0.74M schema and instruction overhead / 0.56M out | none | about 8 h (about 9 s per call [M]) | do not parallelise | n/a |
| Local, Qwen3-8B Q4_K_M via Ollama (measured, contended) | about 1,330 (6K chunks) | 8.0M in / 0.4M out | none | 7 to 13 h | do not parallelise | n/a |
| Local, Qwen3.5-4B class (derived) | about 1,330 | same | none | about 4 to 8 h | do not parallelise | n/a |

- **Why batching matters.** One call per chat would be about 2,700 Codex messages (against 350 to 3,000 per window), about 35 credits and about 5 hours, because the roughly 8K-token harness overhead is paid 2,700 times. Packing 13 to 20 chats per call cuts messages by about 13 times and time by about 5 times [D].
- **Nightly increments.** Assume 30K to 60K new tokens a day (about 10 to 20 chats; an assumption). That is one or two Codex calls, about 0.2 credits and under a minute, plus up to ten consolidation calls. A local model would need about 1 to 6 minutes of extraction per night (0.4% to 0.75% of the 4 to 13 hour first run) [D].
- **Rate-limit realism.** Window sizes in messages are estimates the vendor says may vary with task size. Treat the first 10 calls as calibration, then show the user the projected total before continuing (5.5).

### 5.4 Batching and caching tips

1. **Pack by size, not by chat.** Build batches of about 40K to 60K tokens. Put each chat in a labelled block (`<chat id="...">`) and require the output to be keyed by `chat_id`. Split chats larger than the batch with about 10% overlap and merge results afterwards. Keep Codex batches well under its 272K client context window [M].
2. **Stable prefix first.** Put instructions, schema and few-shot examples first, byte-identical across calls, and the transcripts last. OpenAI prefix caching bills cached input at 10% [cx-pricing][oai-luna]; Anthropic cache reads are 0.1x and a subscription's main conversation requests a 1-hour TTL [anth-pricing][cc-cache]; llama.cpp and Ollama reuse the KV prefix (observed [M]) [llcpp-server]. No timestamps or per-run IDs in the prefix.
3. **Pre-filter before spending anything.** Skip chats with fewer than two user turns, collapse repeated boilerplate, cap very long assistant turns to head and tail, and skip chats whose fingerprint, prompt version and model are unchanged. This plausibly removes a third or more of the 8M tokens; measure it on the real corpus [U].
4. **Idempotent queue in SQLite.** Key results by (chat fingerprint, prompt_version, schema_version, model). Resume after interruption. On failure, bisect the batch to isolate bad inputs.
5. **Concurrency and limits.** Two to four workers. Stop on the first limit signal and sleep until the window resets. For Claude, the Python SDK's `RateLimitEvent` reports utilisation of the five-hour and seven-day windows; use it if recall ever embeds the SDK [sdk-python].
6. **Defence in depth against self-indexing.** Run in the scratch dir; use `--ephemeral` or `--no-session-persistence`; prefix every prompt with a sentinel; skip such sessions in the indexer. After the first calibration run, check that no new file appeared under `~/.codex/sessions` or `~/.claude/projects` for the scratch path.
7. **Validate everything.** Parse and schema-validate; confirm the evidence quote occurs in the source chat; treat a Claude `success` result without `structured_output` as failure [sdk-structured].
8. **Treat transcripts as data.** Wrap them in tags and instruct the model to ignore instructions inside them; keep tools off or the sandbox read-only.
9. **Sample first.** Offer `--estimate` (counts tokens, calls, expected credits, time) and a 2% dry-run that shows example memories and measured cost before the full job; require an explicit yes.
10. **Log per call:** backend, model, tokens (new, cached, output), seconds, exit status. This feeds the ETA and the next calibration.

### 5.5 Calibration checklist and open items

1. Make one tiny `codex exec --json` call per backend at consent time and record `turn.completed.usage`, to learn the real overhead and whether `--disable shell_tool`, `-c web_search=disabled` and `-c project_doc_max_bytes=0` shrink the prompt [U]. The same for `claude -p` (JSON `usage`).
2. Confirm Luna works in the CLI on Free and Go plans, and that parallel `codex exec` processes do not race on credential refresh [U].
3. Confirm `claude -p --json-schema` with `--tools ""` succeeds at `--max-turns 1` or needs 3, measure startup time, and check whether stream-json emits rate-limit events [U].
4. Build a 30-chat hand-labelled set from the user's own history in the style of `evals/cases.jsonl` and compare Luna (low), Haiku, and a local 4B on precision of memories and evidence validity. No public benchmark covers this task.
5. Watch for policy changes: Anthropic's paused credit move for `claude -p`; OpenAI model retirements (GPT-5.5 on 2026-10-14); Cursor CLI upgrades; Antigravity quota publication [anth-sdk-plan][cx-models].
6. Before re-enabling any local backend, find the crash cause (Console and `~/Library/Logs/DiagnosticReports`), then retest with one client only and a smaller context.
7. Decisions for the maintainers: cross-vendor disclosure policy (5.1); consent wording and whether to remind users to switch off training; whether to depend on Codex's internal `models_cache.json`; whether to ship the optional Swift helper or only support `apfel`; default stop thresholds for quota.

## 6. Sources

Fetched 2026-10-06 unless noted. "Archive" means read through the Internet Archive because the vendor site refuses automated fetches.

**Anthropic and Claude Code**

| Key | URL |
| --- | --- |
| cc-headless | https://code.claude.com/docs/en/headless.md |
| cc-cli | https://code.claude.com/docs/en/cli-reference.md |
| cc-env | https://code.claude.com/docs/en/env-vars.md |
| cc-sessions | https://code.claude.com/docs/en/sessions.md |
| cc-auth | https://code.claude.com/docs/en/authentication.md |
| cc-legal | https://code.claude.com/docs/en/legal-and-compliance.md |
| cc-models | https://code.claude.com/docs/en/model-config.md |
| cc-cache | https://code.claude.com/docs/en/prompt-caching.md |
| cc-ctx | https://code.claude.com/docs/en/context-window.md |
| cc-data | https://code.claude.com/docs/en/data-usage.md |
| cc-costs | https://code.claude.com/docs/en/costs.md |
| sdk-overview | https://code.claude.com/docs/en/agent-sdk/overview.md |
| sdk-python | https://code.claude.com/docs/en/agent-sdk/python.md |
| sdk-structured | https://code.claude.com/docs/en/agent-sdk/structured-outputs.md |
| sdk-sessions | https://code.claude.com/docs/en/agent-sdk/sessions.md |
| sdk-features | https://code.claude.com/docs/en/agent-sdk/claude-code-features.md |
| sdk-src | https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py |
| anth-terms | https://www.anthropic.com/legal/consumer-terms (effective 2025-10-08) |
| anth-sdk-plan | https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan (updated 2026-06-15/16) |
| anth-cc-plan | https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan (dated 2026-08-19) |
| anth-pricing | https://platform.claude.com/docs/en/about-claude/pricing |
| aa-haiku | https://artificialanalysis.ai/models/claude-4-5-haiku |

**OpenAI and Codex**

| Key | URL |
| --- | --- |
| cx-exec | https://learn.chatgpt.com/docs/non-interactive-mode.md |
| cx-cli | https://learn.chatgpt.com/docs/developer-commands.md?surface=cli |
| cx-auth | https://learn.chatgpt.com/docs/auth.md |
| cx-models | https://learn.chatgpt.com/docs/models.md |
| cx-pricing | https://learn.chatgpt.com/docs/pricing.md |
| cx-config | https://learn.chatgpt.com/docs/config-file/config-reference.md |
| cx-config-basic | https://learn.chatgpt.com/docs/config-file/config-basic.md |
| cx-src-exec | https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs |
| cx-src-shared | https://github.com/openai/codex/blob/main/codex-rs/utils/cli/src/shared_options.rs |
| cx-src-session | https://github.com/openai/codex/blob/main/codex-rs/core/src/session/session.rs |
| cx-src-memories | https://github.com/openai/codex/blob/main/codex-rs/memories/write/src/start.rs |
| cx-src-auth | https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs |
| cx-src-prompt | https://github.com/openai/codex/blob/main/codex-rs/protocol/src/prompts/base_instructions/default.md |
| oai-luna | https://developers.openai.com/api/docs/models/gpt-6-luna.md |
| oai-so | https://developers.openai.com/api/docs/guides/structured-outputs.md |
| oai-terms | https://openai.com/policies/terms-of-use/ (Archive snapshot 2026-10-02) |
| oai-data | https://help.openai.com/en/articles/7730893-data-controls-faq (Archive snapshot 2026-09-25) |
| aa-luna | https://artificialanalysis.ai/models/gpt-6-luna-low |

**Cursor**

| Key | URL |
| --- | --- |
| cur-headless | https://cursor.com/docs/cli/headless.md |
| cur-params | https://cursor.com/docs/cli/reference/parameters.md |
| cur-output | https://cursor.com/docs/cli/reference/output-format.md |
| cur-auth | https://cursor.com/docs/cli/reference/authentication.md |
| cur-changelog | https://cursor.com/docs/cli/changelog.md |
| cur-pricing | https://cursor.com/docs/models-and-pricing.md |
| cur-terms | https://cursor.com/terms-of-service (updated 2026-09-03) |

**Google**

| Key | URL |
| --- | --- |
| gem-disc | https://github.com/google-gemini/gemini-cli/discussions/28017 |
| gem-dep | https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals |
| gem-quota | https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/quota-and-pricing.md |
| gem-headless | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md |
| gem-cli | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/cli-reference.md |
| gem-session | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md |
| gem-sysmd | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/system-prompt.md |
| gem-tos | https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/tos-privacy.md |
| agy-readme | https://github.com/google-antigravity/antigravity-cli/blob/main/README.md |
| agy-headless | https://antigravity.google/docs/cli/headless.md |
| agy-plans | https://antigravity.google/docs/plans.md |
| agy-models | https://antigravity.google/docs/models.md |
| agy-changelog | https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md |

**Local runtimes and models**

| Key | URL |
| --- | --- |
| mlx-readme | https://github.com/ml-explore/mlx-lm/blob/main/README.md |
| mlx-server | https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md (and `mlx_lm/server.py`) |
| mlx-pypi | https://pypi.org/project/mlx-lm/ (JSON API: https://pypi.org/pypi/mlx-lm/json) |
| mlx-engine | https://github.com/lmstudio-ai/mlx-engine |
| outlines-mlx | https://github.com/dottxt-ai/outlines/blob/main/docs/features/models/mlxlm.md |
| xgrammar-mlx | https://github.com/mlc-ai/xgrammar/blob/main/python/xgrammar/contrib/mlxlm.py |
| llcpp-readme | https://github.com/ggml-org/llama.cpp/blob/master/README.md |
| llcpp-install | https://github.com/ggml-org/llama.cpp/blob/master/docs/install.md |
| llcpp-build | https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md |
| llcpp-server | https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md |
| llcpp-gbnf | https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md |
| llcpp-rel | https://github.com/ggml-org/llama.cpp/releases/tag/b11438 |
| llcpp-4167 | https://github.com/ggml-org/llama.cpp/discussions/4167 |
| llcpppy | https://github.com/abetlen/llama-cpp-python (README) and https://pypi.org/project/llama-cpp-python/ |
| llcpppy-idx | https://abetlen.github.io/llama-cpp-python/whl/metal/llama-cpp-python/ and .../whl/cpu/llama-cpp-python/ |
| ollama-faq | https://docs.ollama.com/faq.md |
| ollama-ctx | https://docs.ollama.com/context-length.md |
| ollama-so | https://docs.ollama.com/capabilities/structured-outputs.md |
| ollama-api | https://docs.ollama.com/api/chat.md |
| ollama-routes | https://github.com/ollama/ollama/blob/main/server/routes.go |
| ollama-rel | https://github.com/ollama/ollama/releases (v0.33.1, v0.34.4, v0.40.0-rc5) |
| ollama-lib | https://ollama.com/library/qwen3.5/tags (also /gemma4, /granite4.2, /qwen3) |
| lms-so | https://lmstudio.ai/docs/developer/openai-compat/structured-output.md |
| lms-cli | https://lmstudio.ai/docs/cli.md |
| lms-headless | https://lmstudio.ai/docs/developer/core/headless.md |
| lms-free | https://lmstudio.ai/blog/free-for-work (2025-07-08) |
| afm-ctx | https://developer.apple.com/documentation/foundationmodels/managing-the-context-window |
| afm-model | https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel |
| afm-updates | https://developer.apple.com/documentation/updates/foundationmodels |
| afm-pcc | https://developer.apple.com/documentation/foundationmodels/adding-server-side-intelligence-with-private-cloud-compute |
| afm-safety | https://developer.apple.com/documentation/foundationmodels/improving-the-safety-of-generative-model-output |
| afm-iface | `/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/FoundationModels.framework/Modules/FoundationModels.swiftmodule/arm64e-apple-macos.swiftinterface` (Command Line Tools SDK) |
| afm-py | https://github.com/apple/python-apple-fm-sdk (README, `build_backend.py`) and https://pypi.org/project/apple-fm-sdk/ |
| apfel | https://github.com/Arthur-Ficial/apfel |
| hf-api | https://huggingface.co/api/models/<repo> for each model named (license, parameter counts, file sizes) |
| hf-qwen35 | https://huggingface.co/Qwen/Qwen3.5-4B and https://huggingface.co/Qwen/Qwen3.5-9B (model cards) |
| hf-gemma4 | https://huggingface.co/google/gemma-4-E4B-it and https://huggingface.co/google/gemma-4-12B-it |
| baseRT | https://arxiv.org/abs/2607.00501 (M4 Pro 16-core, 24 GB; llama.cpp b9630 and MLX 0.31.2 baselines) |
| vllm-mlx | https://arxiv.org/abs/2601.19139 (M4 Max 128 GB) |
| apple-m5 | https://machinelearning.apple.com/research/exploring-llms-mlx-m5 |
| jsb | https://arxiv.org/abs/2501.10868 (JSONSchemaBench) |
| m2v | https://github.com/MinishLab/model2vec |
| m2v-results | https://github.com/MinishLab/model2vec/blob/main/results/README.md |
| fe | https://github.com/qdrant/fastembed (README) and https://pypi.org/project/fastembed/ |
| fe-src | https://github.com/qdrant/fastembed/blob/main/fastembed/text/onnx_embedding.py |
| hf-static | https://huggingface.co/blog/static-embeddings |

**This repository**

| Key | Path |
| --- | --- |
| README | `README.md` (roadmap item, baseline of about 2,700 chats, the local-only privacy promise) |
| recall-src | `src/recall/sources/claude.py`, `codex.py`, `cursor.py`, `AGENTS.md` (read-only rule for other tools' data) |

## Appendix A: measurement log

All measurements used synthetic text only; no user chat content was sent anywhere. Local runs stopped after the crash report, and the compiled helpers and the Python benchmark script were deleted afterwards. Only the Swift sources remain, in the session's scratch directory outside the repo; do not rebuild or re-run them until the crash is diagnosed.

**A.1 Read-only commands used** (no model calls): `sw_vers`, `uname -a`, `sysctl -n machdep.cpu.brand_string hw.memsize hw.ncpu`, `system_profiler SPDisplaysDataType`, `which` for each tool, `ollama --version`, `ollama list`, `ollama ps`, Ollama `GET /`, `GET /api/version` and `POST /api/show` (metadata only), `claude --version`, `claude auth status` (output filtered to non-identifying fields), `claude --help`, `agent --version`, `agent status`, `agent --help`, bundled `codex --version`, `codex login status` (email redacted), `codex exec --help`, `swift --version`, `xcode-select -p`, reading `~/.codex/models_cache.json` (model slugs and limits only), presence checks for `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (values never read), directory sizes of the chat stores.

**A.2 Apple on-device model** (macOS 27.0, AFM 3 Core Advanced, built with CLT `swiftc`).

| Test | Result |
| --- | --- |
| Availability probe | `isAvailable` true; `contextSize` 8,192; 24 supported languages; `en_US` supported |
| Prefill, 2 output tokens max | 456 tokens in 0.78 s (586 tok/s); 1,366 in 1.55 s (879); 2,731 in 2.74 s (995); 4,096 in 5.20 s (788) |
| Decode, 300-word request | 323 tokens in 11.33 s (28.5 tok/s); 400 tokens in 13.49 s (29.6 tok/s) |
| Guided generation, dynamic schema, instructions 67 tokens, schema 163 tokens | prompt 365 tokens: 6.27 s, 189 out; 717: 7.42 s, 175; 1,397: 7.23 s, 171; 2,088: 8.99 s, 175; 3,250: 7.43 s, 115. First call after load: 2.62 s |
| `@Generable` macro build with CLT only | fails: macro plugin `FoundationModelsMacros` not found |

**A.3 Ollama 0.35.0, `qwen3:8b` Q4_K_M, 100% GPU, context 16,384 (already resident), `think:false`, schema-constrained unless noted.**

| Set | Prompt tokens | Prompt processing tok/s | Generation tok/s | Notes |
| --- | --- | --- | --- | --- |
| 1, unconstrained, unique text | 711 / 2,001 / 3,961 / 6,597 | 430 / 437 / 544 / 617 | 21.0 / 24.3 / 33.7 / 27.8 | 200 output tokens |
| 1, schema, same text as the preceding call | same | invalid (prefix cache hit, near-zero prefill) | 24.4 / 34.9 / 31.8 / 29.3 | 4 of 4 valid JSON |
| 2, schema, unique prefix per call (n = 9) | 2,040 to 6,634 | 199 to 269, median 247 | 24.8 to 31.5, median 28.9 | 9 of 9 valid JSON; load time 5.9 s on the first call |

The model was already loaded and in use by another client during both sets, so these are contended numbers.

## Appendix B: tiny Swift helper for Apple's on-device model

The benchmark program (runtime schema, `respond(to:schema:)`, token counting) compiled with Command Line Tools only into a 66 KB binary. The trimmed helper below is adapted from it and was not itself compiled [U]. It reads the chat batch from stdin and prints schema-shaped JSON; recall would build it once with `swiftc -O recall-fm.swift -o recall-fm`, cache the binary, and generate the schema from its own JSON Schema (that converter is not written; `apfel --schema` already does the equivalent).

```swift
import FoundationModels
import Foundation

let item = DynamicGenerationSchema(name: "Memory", properties: [
    .init(name: "kind", schema: DynamicGenerationSchema(name: "Kind",
          anyOf: ["decision", "preference", "fact", "lesson"])),
    .init(name: "text", schema: DynamicGenerationSchema(type: String.self)),
])
let root = DynamicGenerationSchema(name: "Memories", properties: [
    .init(name: "items", schema: DynamicGenerationSchema(arrayOf: item, minimumElements: 0, maximumElements: 6)),
])
let schema = try GenerationSchema(root: root, dependencies: [])

let model = SystemLanguageModel.default
guard model.isAvailable else { print("unavailable: \(model.availability)"); exit(2) }
let input = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8) ?? ""
if try await model.tokenCount(for: input) > model.contextSize - 600 { exit(3) }   // leave room for schema and output
let session = LanguageModelSession(model: model, instructions: "Extract durable memories. Return JSON only.")
let reply = try await session.respond(to: input, schema: schema)
print(reply.content.jsonString)
```

# Grok Bot

Grok Bot is Cursor's personal-agent app (bundle `com.anysphere.sand`; not xAI's Grok). Each bot is a long-running agent on a hosted computer. recall can read the chat cache the app keeps on your Mac, so what you told a bot is searchable from your coding agents, and a bot can ask recall for what you told them.

Both directions are opt-in, and both stay inside one context (see [projects](projects.md) and `[contexts]` in config.toml): bots are personal agents, so their chats are never handed to your work sessions.

## Reading the bots' chats

`recall setup` asks once, with a default of no: "Index them, as personal?". Yes turns on `[grokbot] enabled = true`, asks which bots to leave out (one that posts publicly, say), and puts the app's folder in the context you name (`personal` by default) in `[contexts]`. Nothing here is read until you say yes, and an upgrade never turns it on.

```toml
[grokbot]
enabled = true
skip = ["a bot that posts publicly"]   # bot names or ids, never read

[contexts]
personal = ["~/Documents/Projects/Personal", "~/Library/Application Support/Grok Bot"]
```

A bot you deleted in the app (one the app's roster no longer lists) is never read, whatever you answer: its chats can hold what you set it up to watch, such as a calendar. Setup says how many it skipped.

What is read: only the transcript cache in `~/Library/Application Support/Grok Bot/sand-client-persistence/`, read-only, and nothing else in the app's folder (never `sand-secrets.json`). What is kept: what you sent, what the bot said, the answers you picked, other bots' briefs, file names. Not kept: widget payloads, email drafts, connector, secret-request and permission payloads. The usual redaction runs on all of it.

One chat is one bot on one UTC day (`grokbot:<bot id>:<day>`). The app keeps at most 200 entries per bot; recall's archive keeps the older messages as that window slides, so history is complete from your first index. The format is undocumented: a blob in a shape recall does not know is skipped with a warning.

The nightly notes do not read these chats unless you set `[roots.grokbot] mine = true`.

## A bot asking recall

A bot runs commands on your Mac under the app's local execution policy, with your approval per command. Give it recall's read commands with `--context`, which limits what comes back to one context whatever folder the command runs from. What a bot is told goes to its model provider, so keep the context to the one you are happy to share with it:

```bash
recall search --context personal "trip planning notes"
recall show <chat id> --context personal --query "venue"
recall memory search --context personal "coffee recipe"
recall recent --context personal
```

- Chats outside the context are not returned, and `show` says "no chat" for one.
- A note is returned only when every chat it was distilled from ran in the context.
- Add a line like this to a bot's profile to tell it:
  "To recall what Ali told other agents, run `recall search --context personal "<topic>"` on his Mac and ask before running it."

Sending messages from a bot to your coding agents is not built.

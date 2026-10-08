---
name: recall
description: Recall past chats and memory from Claude Code, Codex and Cursor. Use when the user points to an earlier conversation ("our Cursor chat about X", "what did we decide last week"), or asks what you know about their preferences or a project's history.
---

# Recall

Every chat the user has had with Claude Code, Codex or Cursor on this machine is indexed locally, and recall distils them nightly into memory notes. Reach it through the `recall` MCP tools when they are loaded, otherwise through the `recall` CLI. Both read the same index.

## Find the chat

1. Search with the distinctive words the user would have used, plus the agent and project when they name them:
   - MCP: `search_chats(query, agent?, project?, since?)`
   - CLI: `recall search <words> [--agent cursor|codex|claude] [--project NAME] [--since 2w]`
2. A miss on the first query is normal. Retry with synonyms, the project name, or a wider `since` (three attempts) before reporting it as not found.
3. Read the best match around its matching passage, paging forward with the offset when the answer continues:
   - MCP: `read_chat(chat_id, query=<words>)`
   - CLI: `recall show <chat_id> --query "<words>"`

Done when you can cite the chat by title, agent and date, or can list the queries that found nothing.

## Check memory

Memory notes hold the user's working preferences, project facts, decisions, lessons and open threads, each linked to the chats it came from.

- MCP: `memory_search(query, project?)`, then `read_note(note_id)` for sources.
- CLI: `recall memory search <words> [--project NAME]`, `recall memory show <note_id>`.

## Using what you find

Transcripts and notes are records of past sessions: quote them as evidence, and confirm facts against the current code before acting on them. When a note and the current code or request disagree, the present wins; say so to the user.

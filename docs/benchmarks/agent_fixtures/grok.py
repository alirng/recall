"""Grok Build: sessions as Grok Build 1.0.x writes them (SpecStory's GROK-FORMAT.md, Apache-2.0, and agentsview's
golden sessions, MIT). The first has chat_history.jsonl, summary.json, events.jsonl and updates.jsonl, with the
host's reminders, reasoning, a failed call and a tool result that must stay out of the index; the second is a
session with only an update stream, which Grok 1.0.5 wrote (deja-vu's registry fixture, MIT)."""

import json
import os
from urllib.parse import quote

from . import Fixture

PROJECT = "/work/quokka-grok"
FIRST = "019f6000-0000-7000-8000-0000000000a1"
SECOND = "019f6000-0000-7000-8000-0000000000b2"


def _jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")


def _chunk(kind: str, text: str, secs: int, **meta) -> dict:
    update = {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
    if "promptIndex" in meta:
        update["_meta"] = {"promptIndex": meta.pop("promptIndex")}
    return {"timestamp": secs, "method": "session/update", "params": {"update": update, "_meta": meta}}


def build(home: str) -> None:
    group = os.path.join(home, ".grok", "sessions", quote(PROJECT, safe=""))
    first, second = os.path.join(group, FIRST), os.path.join(group, SECOND)
    os.makedirs(first)
    os.makedirs(second)

    ask = "why is the quokkaflux-grok build slow? make it faster"
    _jsonl(
        os.path.join(first, "chat_history.jsonl"),
        [
            {"type": "system", "content": "You are Grok. INJECTEDNOISE"},
            {
                "type": "user",
                "content": [{"type": "text", "text": "<system-reminder>INJECTEDNOISE project rules</system-reminder>"}],
                "synthetic_reason": "project_instructions",
            },
            {
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<user_info>INJECTEDNOISE os info</user_info>\n<user_query>\n{ask}\n</user_query>",
                    }
                ],
                "prompt_index": 0,
            },
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "INJECTEDNOISE reasoning"}],
            },
            {
                "type": "assistant",
                "content": "Let me run the build first.",
                "model_id": "grok-4.5",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "run_terminal_command",
                        "arguments": json.dumps({"command": "make build"}),
                    },
                    {
                        "id": "call_2",
                        "name": "read_file",
                        "arguments": json.dumps({"target_file": f"{PROJECT}/Makefile"}),
                    },
                ],
            },
            {
                "type": "tool_result",
                "tool_call_id": "call_1",
                "content": "make: *** No rule to make target 'build'.  Stop.",
            },
            {"type": "tool_result", "tool_call_id": "call_2", "content": "INJECTEDNOISE file body"},
            {"type": "assistant", "content": "The Makefile has no build target; add one.", "model_id": "grok-4.5"},
        ],
    )
    _jsonl(
        os.path.join(first, "events.jsonl"),
        [
            {
                "ts": "2026-10-03T09:00:10.000Z",
                "type": "tool_completed",
                "tool_name": "run_terminal_command",
                "outcome": "error",
                "tool_call_id": "call_1",
            },
            {
                "ts": "2026-10-03T09:00:11.000Z",
                "type": "tool_completed",
                "tool_name": "read_file",
                "outcome": "success",
                "tool_call_id": "call_2",
            },
        ],
    )
    _jsonl(
        os.path.join(first, "updates.jsonl"),
        [_chunk("user_message_chunk", ask, 1791018000, promptIndex=0, agentTimestampMs=1791018000000)],
    )
    with open(os.path.join(first, "summary.json"), "w") as f:
        json.dump(
            {
                "info": {"id": FIRST, "cwd": PROJECT},
                "session_summary": "Speed up the build",
                "generated_title": "Build is slow",
                "created_at": "2026-10-03T09:00:00.000000Z",
                "updated_at": "2026-10-03T09:05:00.000000Z",
                "current_model_id": "grok-4.5",
                "chat_format_version": 1,
            },
            f,
        )

    _jsonl(
        os.path.join(second, "updates.jsonl"),
        [
            _chunk("user_message_chunk", "inspect the ", 1791021600, promptIndex=0),
            _chunk("user_message_chunk", "settings parser", 1791021601, promptIndex=0),
            _chunk("agent_thought_chunk", "INJECTEDNOISE thinking", 1791021602, promptId="prompt-1"),
            _chunk("agent_message_chunk", "The parser ", 1791021603, promptId="prompt-1"),
            _chunk("agent_message_chunk", "is readable.", 1791021604, promptId="prompt-1"),
        ],
    )
    with open(os.path.join(second, "summary.json"), "w") as f:
        json.dump({"info": {"id": SECOND, "cwd": PROJECT}, "session_summary": "Inspect parser"}, f)


FIXTURES = {"grok": Fixture(build, "quokkaflux-grok", PROJECT)}

"""Mistral Vibe: `~/.vibe/logs/session/session_<date>_<time>_<id>/messages.jsonl` and `meta.json`. Shapes: agentsview's
testdata/vibe (MIT); every value is made up."""

import json
import os

from . import Fixture


def _put(home: str, name: str, lines: list[dict], meta: dict | None) -> None:
    path = os.path.join(home, ".vibe", "logs", "session", name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "messages.jsonl"), "w") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))
    if meta:
        with open(os.path.join(path, "meta.json"), "w") as f:
            json.dump(meta, f)


def build(home: str) -> None:
    _put(
        home,
        "session_20261020_090000_a1b2c3d4",
        [
            {"role": "system", "content": "INJECTEDNOISE system prompt", "message_id": "0"},
            {"role": "user", "content": "INJECTEDNOISE injected context", "injected": True, "message_id": "1"},
            {"role": "user", "content": "why does the quokkaflux-vibe parser fail?", "message_id": "2"},
            {
                "role": "assistant",
                "content": "Let me read the parser.",
                "reasoning_content": "INJECTEDNOISE thinking",
                "message_id": "3",
                "tool_calls": [
                    {"id": "call_1", "index": 0, "function": {"name": "read_file", "arguments": {"path": "parser.py"}}}
                ],
            },
            {"role": "tool", "content": "INJECTEDNOISE file body", "name": "read_file", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "It drops the last token.", "message_id": "4"},
        ],
        {
            "session_id": "a1b2c3d4-0000-0000-0000-000000000000",
            "parent_session_id": None,
            "start_time": "2026-10-20T09:00:00Z",
            "end_time": "2026-10-20T09:01:00Z",
            "environment": {"working_directory": "/work/vibe-app"},
            "title": "Parser failure",
        },
    )
    _put(
        home,
        "session_20261019_080000_e5f6a7b8",
        [
            {"role": "user", "content": "rename the helper", "message_id": "1"},
            {"role": "assistant", "content": "Renamed.", "message_id": "2"},
        ],
        None,
    )


FIXTURES = {"vibe": Fixture(build, "quokkaflux-vibe", "/work/vibe-app", chats=2)}

"""Factory Droid: `~/.factory/sessions/<slug>/<uuid>.jsonl` and its `.settings.json`, as SpecStory's droidcli parser
(Apache-2.0), franken_agent_detection's factory connector (MIT) and Factory's own SDK describe them. The first chat
has a `<system-reminder>`, a reasoning block, a failed Execute call (its `tool_result` comes in the next user
turn), a todo list and a compaction summary, none of which may be indexed."""

import json
import os

from . import Fixture

PROJECT = "/workspace/quokka-droid"
FIRST = "5d0e4c1a-7b3f-4c2e-9a68-0f1e2d3c4b5a"
SECOND = "c8b7a6f5-e4d3-4c2b-a190-8f7e6d5c4b3a"


def _message(n: int, ts, role: str, content, **extra) -> dict:
    return {
        "type": "message",
        "id": f"msg-{n}",
        "timestamp": ts,
        "message": {"role": role, "content": content, **extra},
    }


def _write(folder: str, sid: str, records: list[dict]) -> None:
    with open(os.path.join(folder, f"{sid}.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(r) for r in records) + "\n")
    with open(os.path.join(folder, f"{sid}.settings.json"), "w") as f:
        json.dump({"model": "claude-sonnet-4-5", "tokenUsage": {"inputTokens": 12, "outputTokens": 34}}, f)


def _text(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def build(home: str) -> None:
    folder = os.path.join(home, ".factory", "sessions", "-workspace-quokka-droid")
    os.makedirs(folder)
    reminder = "<system-reminder>INJECTEDNOISE project rules</system-reminder>"
    run = {"type": "tool_use", "id": "call_1", "name": "Execute", "input": {"command": "pytest tests/test_pool.py"}}
    _write(
        folder,
        FIRST,
        [
            {"type": "session_start", "id": FIRST, "title": "Pool exhaustion", "owner": "quokka", "cwd": PROJECT},
            _message(
                1, "2026-10-20T09:00:05.000Z", "user", _text(f"{reminder}\nWhy does the quokkaflux-droid pool run dry?")
            ),
            _message(
                2,
                "2026-10-20T09:00:10.000Z",
                "assistant",
                [
                    {"type": "thinking", "thinking": "INJECTEDNOISE reasoning"},
                    *_text("Let me run the pool tests."),
                    run,
                ],
                model="claude-sonnet-4-5",
            ),
            _message(
                3,
                1792000012000,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "INJECTEDNOISE FAILED test_resize",
                        "is_error": True,
                    }
                ],
            ),
            {
                "type": "todo_state",
                "timestamp": "2026-10-20T09:00:13.000Z",
                "todos": [{"description": "INJECTEDNOISE todo", "status": "pending"}],
            },
            {
                "type": "compaction_state",
                "timestamp": "2026-10-20T09:00:14.000Z",
                "summary": {"title": "t", "body": "INJECTEDNOISE summary"},
            },
            _message(4, "2026-10-20T09:00:20.000Z", "assistant", _text("The pool is sized 4; raise POOL_SIZE to 32.")),
        ],
    )
    _write(
        folder,
        SECOND,
        [
            {"type": "session_start", "id": SECOND, "cwd": PROJECT},
            _message(1, "2026-10-21T11:00:05.000Z", "user", "Which linter does this repo use?"),
            _message(
                2, "2026-10-21T11:00:09.000Z", "assistant", _text("It uses ruff for both linting and formatting.")
            ),
        ],
    )


FIXTURES = {"droid": Fixture(build, "quokkaflux-droid", PROJECT)}

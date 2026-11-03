"""Amp's legacy local threads: `~/.local/share/amp/threads/T-<uuid>.json`, one JSON object per thread. Shape:
deja-vu docs/registry/amp.md and fixtures/registry/amp (MIT); every value is made up."""

import json
import os

from . import Fixture

T0 = 1_761_000_000_000


def _put(home: str, name: str, thread: dict) -> None:
    path = os.path.join(home, ".local", "share", "amp", "threads", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(thread, f)


def build(home: str) -> None:
    _put(
        home,
        "T-0a1b2c3d-0000-4000-8000-000000000001.json",
        {
            "v": 12,
            "id": "T-0a1b2c3d-0000-4000-8000-000000000001",
            "title": "Fix the flaky retry",
            "created": T0,
            "env": {"initial": {"trees": [{"displayName": "amp-app", "uri": "file:///work/amp-app"}]}},
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "why does the quokkaflux-amp retry loop spin?"}],
                    "meta": {"sentAt": T0 + 1000},
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "INJECTEDNOISE reasoning"},
                        {"type": "text", "text": "Let me run the tests."},
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "pytest -x"}},
                    ],
                    "usage": {"model": "claude-sonnet-5", "timestamp": "2026-10-20T09:00:03.000Z"},
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "toolUseID": "toolu_1",
                            "run": {
                                "status": "done",
                                "result": {"output": "FAILED test_retry INJECTEDNOISE", "exitCode": 1},
                            },
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The loop never backs off; add a delay."}],
                    "usage": {"timestamp": "2026-10-20T09:00:08.000Z"},
                },
            ],
        },
    )
    _put(
        home,
        "T-0a1b2c3d-0000-4000-8000-000000000002.json",
        {
            "v": 12,
            "id": "T-0a1b2c3d-0000-4000-8000-000000000002",
            "created": T0 + 5000,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "rename the helper module"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Renamed it."}]},
            ],
        },
    )


FIXTURES = {"amp": Fixture(build, "quokkaflux-amp", "/work/amp-app", chats=2)}

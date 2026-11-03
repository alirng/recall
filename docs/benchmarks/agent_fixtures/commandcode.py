"""Command Code: `~/.commandcode/projects/<encoded-cwd>/<session>.jsonl`, in the v3 shape (a `session` header, then
`message` envelopes with Claude's blocks) and the older flat one, with a checkpoints stream beside them. Shapes:
deja-vu docs/registry/commandcode.md and fixtures/registry/commandcode (MIT); every value is made up."""

import json
import os

from . import Fixture


def _put(home: str, project: str, name: str, lines: list[dict]) -> None:
    path = os.path.join(home, ".commandcode", "projects", project, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))


def _msg(n: int, role: str, content) -> dict:
    return {
        "type": "message",
        "timestamp": f"2026-10-20T09:00:{n:02d}Z",
        "message": {"role": role, "content": content},
    }


def build(home: str) -> None:
    project = "-work-commandcode-app"
    _put(
        home,
        project,
        "cc-sess-1.jsonl",
        [
            {
                "type": "session",
                "version": 3,
                "id": "cc-sess-1",
                "timestamp": "2026-10-20T09:00:00Z",
                "cwd": "/work/commandcode-app",
            },
            _msg(
                1,
                "user",
                [
                    {"type": "text", "text": "why does the quokkaflux-commandcode build fail?"},
                    {"type": "text", "text": "<system-reminder>INJECTEDNOISE</system-reminder>"},
                ],
            ),
            _msg(
                2,
                "assistant",
                [
                    {"type": "text", "text": "Let me run it."},
                    {"type": "tool_use", "id": "tu_1", "name": "shell_command", "input": {"command": "make"}},
                ],
            ),
            _msg(
                3,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "Exit code: 2\nmake: *** Error INJECTEDNOISE",
                    }
                ],
            ),
            _msg(4, "assistant", [{"type": "text", "text": "The Makefile misses a target."}]),
        ],
    )
    _put(
        home,
        project,
        "cc-sess-1.checkpoints.jsonl",
        [{"type": "checkpoint", "id": "cp1", "timestamp": "2026-10-20T09:00:06Z", "files": ["INJECTEDNOISE.go"]}],
    )
    _put(
        home,
        project,
        "cc-sess-2.jsonl",
        [
            {
                "role": "user",
                "content": "rename the helper",
                "timestamp": "2026-10-19T12:00:05Z",
                "sessionId": "cc-sess-2",
            },
            {"role": "tool", "content": "INJECTEDNOISE tool output", "timestamp": "2026-10-19T12:00:07Z"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Renamed it."}],
                "timestamp": "2026-10-19T12:00:21Z",
                "sessionId": "cc-sess-2",
            },
        ],
    )


FIXTURES = {"commandcode": Fixture(build, "quokkaflux-commandcode", "/work/commandcode-app", chats=2)}

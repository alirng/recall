"""Muse Code: `~/.local/share/muse/sessions/YYYY/MM/DD/<id>/session.jsonl`, an event-sourced log with one record
per line, a retained frame whose children are JSON strings, a subagent's task records under another stream id,
and a subagent transcript that is not a session of its own. Shapes: SpecStory's MUSE-CODE-FORMAT.md (Apache-2.0)
and deja-vu's fixtures/registry/muse (MIT); every value is made up."""

import json
import os

from . import Fixture

T0 = 1_789_790_400_000_000  # microseconds


def _rec(stream: str, n: int, payload_type: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "id": f"00000000-0000-4000-8000-{n:012d}",
        "stream": {"kind": "session", "id": stream},
        "sequence": n,
        "recorded_at": T0 + n * 1_000_000,
        "record_type": "event",
        "payload_type": payload_type,
        "payload": payload,
    }


def _run(stream: str, n: int, event: dict) -> dict:
    return _rec(stream, n, "runtime.session", {"kind": "run", "run_id": "run-1", "event": event})


def _put(home: str, sid: str, lines: list[dict], sub: str | None = None) -> None:
    path = os.path.join(home, ".local", "share", "muse", "sessions", "2026", "10", "20", sid)
    path = os.path.join(path, "subagent", sub) if sub else path
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "session.jsonl"), "w") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))


def build(home: str) -> None:
    a = "01a0b7d1-0000-7000-8000-00000000d3a0"
    task = "018f0000-0000-7000-8000-000000000001"
    bash = {
        "command": "go test ./...",
        "exit_code": 1,
        "output": "demo_test.go:12: want 2, got 3 INJECTEDNOISE",
        "truncated": False,
    }
    frame = _rec(a, 2, "runtime.session.permission_format_declared", {"format": "profile_v1"})
    _put(
        home,
        a,
        [
            _rec(
                a, 1, "runtime.session.metadata", {"kind": "metadata", "record": {"workspace_root": "/work/muse-app"}}
            ),
            {
                "retained_frame": "session_permission_transaction",
                "children": [{"child_index": 0, "record_json": json.dumps(frame)}],
            },
            _rec(a, 3, "session.name.changed", {"previous_name": None, "new_name": "Fix the demo test"}),
            _run(a, 4, {"kind": "started", "prompt": "why does the quokkaflux-muse test fail?"}),
            _rec(
                task,
                5,
                "runtime.session",
                {"kind": "run", "event": {"kind": "started", "prompt": "INJECTEDNOISE objective"}},
            ),
            _run(
                a,
                6,
                {"kind": "reasoning_committed", "message_id": "m0", "text": "INJECTEDNOISE", "encrypted_content": "x"},
            ),
            _run(
                a,
                7,
                {
                    "kind": "assistant_tool_calls_committed",
                    "message_id": "m1",
                    "tool_calls": [
                        {
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "bash",
                            "args": json.dumps({"command": "go test ./..."}),
                        }
                    ],
                },
            ),
            _run(
                a,
                8,
                {
                    "kind": "tool_result_batch_committed",
                    "results": [{"tool_call_index": 0, "tool_call_id": "call_1", "text": json.dumps(bash)}],
                },
            ),
            _run(
                a,
                9,
                {
                    "kind": "assistant_message_committed",
                    "message_id": "m2",
                    "text": "The expected value is off by one.",
                },
            ),
        ],
    )
    b = "01a0b7d1-0000-7000-8000-00000000d3a1"
    _put(
        home,
        b,
        [
            _rec(
                b, 1, "runtime.session.metadata", {"kind": "metadata", "record": {"workspace_root": "/work/muse-other"}}
            ),
            _run(b, 2, {"kind": "started", "prompt": "rename the helper"}),
            _run(b, 3, {"kind": "assistant_message_committed", "message_id": "m1", "text": "Renamed."}),
        ],
    )
    _put(home, a, [_run(task, 1, {"kind": "started", "prompt": "INJECTEDNOISE subagent transcript"})], sub=task)


FIXTURES = {"muse": Fixture(build, "quokkaflux-muse", "/work/muse-app", chats=2)}

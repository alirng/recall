"""Gemini CLI, Qwen Code and iFlow: stores as the agents write them, built from deja-vu's registry fixtures (MIT),
agentsview's golden sessions (MIT) and SpecStory's qwencode testdata (Apache-2.0), never from a real chat.

Gemini CLI: a v0.39 JSONL log (header, a reply written again once its calls are filled in, a `$set` summary,
a rewound turn, the CLI's own session context) and a pre-0.39 one-object `.json`. Qwen Code: a chat under
`projects/` and one in Gemini CLI's layout under `tmp/`. iFlow: Claude-like records with a streamed reply."""

import json
import os

from . import Fixture

GEMINI_PROJECT = "/work/quokka-gemini"
QWEN_PROJECT = "/work/quokka-qwen"
IFLOW_PROJECT = "/work/quokka-iflow"


def _write(path: str, rows: list[dict] | dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        if isinstance(rows, dict):
            json.dump(rows, f)
        else:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")


def _gemini(home: str) -> None:
    gemini = os.path.join(home, ".gemini")
    project = os.path.join(gemini, "tmp", "quokka-gemini")
    os.makedirs(project, exist_ok=True)
    with open(os.path.join(project, ".project_root"), "w") as f:
        f.write(GEMINI_PROJECT)
    _write(os.path.join(gemini, "projects.json"), {"projects": {GEMINI_PROJECT: "quokka-gemini"}})
    shell = {
        "id": "call-1",
        "name": "run_shell_command",
        "args": {"command": "npm test"},
        "status": "success",
        "result": [
            {
                "functionResponse": {
                    "id": "call-1",
                    "name": "run_shell_command",
                    "response": {
                        "output": "Command: npm test\nDirectory: (root)\nOutput: FAIL src/db.test.ts\n"
                        "Error: (none)\nExit Code: 1\nSignal: (none)\nProcess Group PGID: 77"
                    },
                }
            }
        ],
    }
    missing = {
        "id": "call-2",
        "name": "read_file",
        "args": {"file_path": "missing.ts"},
        "status": "error",
        "result": [
            {
                "functionResponse": {
                    "id": "call-2",
                    "name": "read_file",
                    "response": {"error": "File not found: missing.ts"},
                }
            }
        ],
    }
    started = "2026-10-04T09:00:00.000Z"
    reply = {"id": "m3", "timestamp": "2026-10-04T09:00:03.000Z", "type": "gemini", "model": "gemini-2.5-pro"}
    _write(
        os.path.join(project, "chats", "session-2026-10-04T09-00-aaaa1111.jsonl"),
        [
            {
                "sessionId": "aaaa1111-0000-4000-8000-000000000001",
                "projectHash": "0123abcd",
                "startTime": started,
                "lastUpdated": started,
                "kind": "main",
            },
            {
                "id": "m0",
                "timestamp": started,
                "type": "user",
                "content": [{"text": "<session_context>INJECTEDNOISE cwd and date</session_context>"}],
            },
            {
                "id": "m1",
                "timestamp": "2026-10-04T09:00:01.000Z",
                "type": "user",
                "content": [{"text": "why does the quokkaflux-gemini suite fail on CI?"}],
            },
            {
                "id": "m2",
                "timestamp": "2026-10-04T09:00:02.000Z",
                "type": "user",
                "content": [{"text": "INJECTEDNOISE rewound turn"}],
            },
            {"$rewindTo": "m2"},
            {
                **reply,
                "content": "Let me run the tests.",
                "thoughts": [{"subject": "INJECTEDNOISE", "description": "x"}],
            },
            {**reply, "content": "Let me run the tests.", "toolCalls": [shell, missing]},
            {
                "id": "m4",
                "timestamp": "2026-10-04T09:00:04.000Z",
                "type": "user",
                "content": [{"functionResponse": {"id": "call-1", "response": {"output": "INJECTEDNOISE"}}}],
            },
            {
                "id": "m5",
                "timestamp": "2026-10-04T09:00:05.000Z",
                "type": "gemini",
                "content": "The tests need a database; start one in CI.",
            },
            {
                "id": "m6",
                "timestamp": "2026-10-04T09:00:06.000Z",
                "type": "info",
                "content": "INJECTEDNOISE update available",
            },
            {"$set": {"summary": "CI test failures", "lastUpdated": "2026-10-04T09:00:06.000Z"}},
        ],
    )
    _write(
        os.path.join(gemini, "tmp", "9f86d081884c7d65", "chats", "session-2026-09-20T15-30-bbbb2222.json"),
        {
            "sessionId": "bbbb2222-0000-4000-8000-000000000002",
            "projectHash": "9f86d081884c7d65",
            "startTime": "2026-09-20T15:30:00.000Z",
            "lastUpdated": "2026-09-20T15:31:00.000Z",
            "messages": [
                {
                    "id": "u1",
                    "timestamp": "2026-09-20T15:30:00.000Z",
                    "type": "user",
                    "content": "rename the config loader",
                },
                {
                    "id": "g1",
                    "timestamp": "2026-09-20T15:31:00.000Z",
                    "type": "gemini",
                    "content": "Renamed it to loadConfig.",
                    "model": "gemini-2.5-pro",
                },
            ],
        },
    )


def _qwen(home: str) -> None:
    qwen = os.path.join(home, ".qwen")
    sid = "cccc3333-0000-4000-8000-000000000003"

    def record(kind: str, t: int, parts=None, **more) -> dict:
        d = {
            "uuid": f"u-{t}",
            "sessionId": sid,
            "timestamp": f"2026-10-05T10:00:{t:02d}.000Z",
            "type": kind,
            "cwd": QWEN_PROJECT,
            "version": "0.23.4",
            **more,
        }
        if parts is not None:
            d["message"] = {"role": "user" if kind in ("user", "tool_result") else "model", "parts": parts}
        return d

    _write(
        os.path.join(qwen, "projects", "-work-quokka-qwen", "chats", f"{sid}.jsonl"),
        [
            record("user", 0, [{"text": "fix the quokkaflux-qwen parser bug"}], provenance="real_user"),
            record(
                "user", 1, [{"text": "INJECTEDNOISE task notification"}], provenance="system", subtype="notification"
            ),
            record(
                "assistant",
                2,
                [
                    {"text": "INJECTEDNOISE reasoning", "thought": True},
                    {"text": "Let me read the parser."},
                    {"functionCall": {"id": "call_a", "name": "read_file", "args": {"file_path": "parser.py"}}},
                ],
                model="qwen3-coder-plus",
            ),
            record(
                "tool_result",
                3,
                [
                    {
                        "functionResponse": {
                            "id": "call_a",
                            "name": "read_file",
                            "response": {"error": "File not found: parser.py"},
                        }
                    }
                ],
                provenance="system",
                toolCallResult={"callId": "call_a", "status": "error", "errorType": "file_not_found"},
            ),
            record(
                "assistant", 4, [{"functionCall": {"id": "call_b", "name": "glob", "args": {"pattern": "**/*.py"}}}]
            ),
            record(
                "tool_result",
                5,
                [
                    {
                        "functionResponse": {
                            "id": "call_b",
                            "name": "glob",
                            "response": {"output": "INJECTEDNOISE src/parse.py"},
                        }
                    }
                ],
                provenance="system",
                toolCallResult={"callId": "call_b", "status": "success"},
            ),
            record(
                "system",
                6,
                subtype="chat_compression",
                systemPayload={
                    "compressedHistory": [
                        {"role": "user", "parts": [{"text": "<state_snapshot>INJECTEDNOISE</state_snapshot>"}]}
                    ]
                },
            ),
            record("assistant", 7, [{"text": "The parser is in src/parse.py; the off-by-one is on line 40."}]),
        ],
    )
    _write(
        os.path.join(qwen, "tmp", "5feceb66ffc86f38", "chats", "session-1731107950138-dddd4444.json"),
        {
            "sessionId": "dddd4444-0000-4000-8000-000000000004",
            "projectHash": "5feceb66ffc86f38",
            "startTime": "2026-08-01T08:00:00.000Z",
            "lastUpdated": "2026-08-01T08:01:00.000Z",
            "messages": [
                {
                    "id": "u1",
                    "timestamp": "2026-08-01T08:00:00.000Z",
                    "type": "user",
                    "content": "list the todo comments",
                },
                {
                    "id": "g1",
                    "timestamp": "2026-08-01T08:01:00.000Z",
                    "type": "gemini",
                    "content": "There are three TODOs, all in utils.py.",
                },
            ],
        },
    )


def _iflow(home: str) -> None:
    folder = os.path.join(home, ".iflow", "projects", "-work-quokka-iflow")
    first = "eeee5555-0000-4000-8000-000000000005"

    def record(sid: str, uuid: str, parent, kind: str, t: int, content, **more) -> dict:
        return {
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": f"session-{sid}",
            "timestamp": f"2026-10-06T11:00:{t:02d}.000Z",
            "type": kind,
            "isSidechain": False,
            "userType": "external",
            "message": {"role": kind, "content": content},
            "cwd": IFLOW_PROJECT,
            "version": "1.0.0",
            **more,
        }

    use = {"type": "tool_use", "id": "call_1", "name": "run_shell_command", "input": {"command": "cargo build"}}
    text = {"type": "text", "text": "I'll build it first."}
    failed = {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": {
            "callId": "call_1",
            "responseParts": {
                "functionResponse": {
                    "id": "call_1",
                    "name": "run_shell_command",
                    "response": {"error": "error[E0432]: unresolved import `foo`"},
                }
            },
        },
    }
    _write(
        os.path.join(folder, f"session-{first}.jsonl"),
        [
            record(first, "a1", None, "user", 0, "why does the quokkaflux-iflow build break?"),
            record(first, "a2", "a1", "user", 1, "INJECTEDNOISE caveat", isMeta=True),
            record(first, "a3", "a1", "assistant", 2, [text, use]),
            record(first, "a4", "a1", "assistant", 2, [use]),
            record(first, "a5", "a3", "user", 3, [failed]),
            record(
                first,
                "a6",
                "a5",
                "assistant",
                4,
                [{"type": "text", "text": "The crate `foo` is missing from Cargo.toml."}],
            ),
        ],
    )
    second = "ffff6666-0000-4000-8000-000000000006"
    _write(
        os.path.join(folder, f"session-{second}.jsonl"),
        [
            record(second, "b1", None, "user", 0, "bump the version to 2.0"),
            record(second, "b2", "b1", "assistant", 1, [{"type": "text", "text": "Bumped it in Cargo.toml."}]),
        ],
    )


FIXTURES = {
    "gemini": Fixture(_gemini, "quokkaflux-gemini", GEMINI_PROJECT),
    "qwen": Fixture(_qwen, "quokkaflux-qwen", QWEN_PROJECT),
    "iflow": Fixture(_iflow, "quokkaflux-iflow", IFLOW_PROJECT),
}

"""GitHub Copilot Chat in VS Code: a `chatSessions` mutation log, a flat `.json` session from before VS Code 1.109,
and the extension's own `GitHub.copilot-chat/transcripts` event log. Shapes: deja-vu docs/registry/copilot-chat.md
and its fixtures/registry/copilot-chat (MIT); every value is made up."""

import json
import os

from . import Fixture

SUPPORT = "Library/Application Support"  # macOS; the eval runs on a Mac, where roots.expand picks this
T0 = 1_761_000_000_000


def _put(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _lines(*ops: dict) -> str:
    return "".join(json.dumps(op) + "\n" for op in ops)


def build(home: str) -> None:
    ws = os.path.join(home, SUPPORT, "Code", "User", "workspaceStorage", "a1b2c3d4e5f6")
    _put(os.path.join(ws, "workspace.json"), json.dumps({"folder": "file:///work/vscode-app"}))
    request = {
        "requestId": "request_1",
        "timestamp": T0,
        "message": {"text": "why does quokkaflux-vscode drop the last retry?"},
        "response": [
            {"kind": "thinking", "value": "INJECTEDNOISE reasoning"},
            {"kind": "progressMessage", "content": {"value": "INJECTEDNOISE progress"}},
            {"value": "The loop compares with < where it needs <=. See "},
            {"kind": "inlineReference", "inlineReference": {"path": "/work/vscode-app/retry.py"}},
            {"value": ".\n"},
            {"value": "```\n"},
            {"kind": "codeblockUri", "uri": {"path": "/work/vscode-app/retry.py"}},
            {
                "kind": "textEditGroup",
                "uri": {"path": "/work/vscode-app/retry.py"},
                "edits": [[{"text": "INJECTEDNOISE edit", "range": {}}]],
            },
            {"value": "\n```\n"},
            {
                "kind": "toolInvocationSerialized",
                "toolId": "run_in_terminal",
                "toolCallId": "call_1",
                "toolSpecificData": {
                    "kind": "terminal",
                    "commandLine": {"original": "pytest -x"},
                    "terminalCommandState": {"exitCode": 1},
                    "terminalCommandOutput": {"text": "FAILED test_retry.py::test_last - AssertionError INJECTEDNOISE"},
                },
            },
            {"value": "I ran the test and it still fails; fixing the bound."},
        ],
        "result": {"metadata": {"summary": {"text": "INJECTEDNOISE compaction summary"}}},
    }
    _put(
        os.path.join(ws, "chatSessions", "3b7e1f1b-0000-4000-8000-000000000000.jsonl"),
        _lines(
            {
                "kind": 0,
                "v": {
                    "version": 3,
                    "sessionId": "3b7e1f1b-0000-4000-8000-000000000000",
                    "creationDate": T0,
                    "customTitle": "Fix the retry loop",
                    "requests": [],
                    "inputState": {"selectedModel": {"metadata": {"auth": {"accountLabel": "someone"}}}},
                },
            },
            {"kind": 2, "k": ["requests"], "v": [request]},
            {"kind": 1, "k": ["requests", 0, "responseTimestamp"], "v": T0 + 4000},
        ),
    )
    _put(
        os.path.join(ws, "chatSessions", "5d0a9e22-0000-4000-8000-000000000000.json"),
        json.dumps(
            {
                "version": 3,
                "sessionId": "5d0a9e22-0000-4000-8000-000000000000",
                "creationDate": T0,
                "requests": [
                    {
                        "requestId": "request_1",
                        "timestamp": T0,
                        "message": {"text": "rename the helper module"},
                        "response": [{"value": "Renamed it and updated the imports."}],
                    }
                ],
            }
        ),
    )
    events = [
        ("session.start", {"sessionId": "7c9e2d2c-0000-4000-8000-000000000000", "startTime": "2026-10-20T09:00:00Z"}),
        ("user.message", {"content": "add a manifest step to the packer build"}),
        ("assistant.message", {"content": "Adding the post-processor now.", "toolRequests": []}),
        ("tool.execution_start", {"toolCallId": "c1", "toolName": "run_in_terminal", "arguments": {"command": "make"}}),
        ("tool.execution_complete", {"toolCallId": "c1", "success": False, "error": "make: *** Error 2 INJECTEDNOISE"}),
    ]
    _put(
        os.path.join(ws, "GitHub.copilot-chat", "transcripts", "7c9e2d2c-0000-4000-8000-000000000000.jsonl"),
        _lines(*({"type": t, "data": d, "timestamp": f"2026-10-20T09:00:{i:02d}Z"} for i, (t, d) in enumerate(events))),
    )


FIXTURES = {"vscode": Fixture(build, "quokkaflux-vscode", "/work/vscode-app", chats=3)}

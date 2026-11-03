"""Continue's `~/.continue/sessions/<id>.json` and `sessions.json`, from deja-vu's docs/registry/continue.md and
fixtures (MIT). The first chat is the IDE's (a `file://` workspace, parts content, context items, a system item, a
tool item and a failed `run_terminal_command`); the second is the CLI's (a plain path, a failed `Bash`, and a
compaction summary item). None of the INJECTEDNOISE text may be indexed."""

import json
import os

from . import Fixture

PROJECT = "/work/continue-app"
IDE_ID = "8f1c2a3e-1111-4000-8000-000000000001"
CLI_ID = "8f1c2a3e-2222-4000-8000-000000000002"


def _item(role: str, content, **extra) -> dict:
    return {"message": {"role": role, "content": content}, "contextItems": [], **extra}


def _call(name: str, args: dict, status: str, output: list[dict]) -> dict:
    return {
        "toolCallId": f"call_{name}",
        "status": status,
        "parsedArgs": args,
        "toolCall": {"function": {"name": name, "arguments": json.dumps(args)}},
        "output": output,
    }


def build(home: str) -> None:
    folder = os.path.join(home, ".continue", "sessions")
    os.makedirs(folder)
    ide = {
        "sessionId": IDE_ID,
        "title": "New Session",
        "workspaceDirectory": f"file://{PROJECT}",
        "history": [
            _item("system", "INJECTEDNOISE system prompt"),
            {
                **_item("user", [{"type": "text", "text": "why does the quokkaflux-continue autocomplete lag?"}]),
                "contextItems": [
                    {"name": "complete.ts", "description": "INJECTEDNOISE file contents", "content": "INJECTEDNOISE"}
                ],
            },
            _item(
                "assistant",
                [{"type": "text", "text": "Let me run the benchmarks."}],
                toolCallStates=[
                    _call(
                        "run_terminal_command",
                        {"command": "npm run bench"},
                        "errored",
                        [{"content": "INJECTEDNOISE bench output", "status": "Command failed with exit code 1"}],
                    )
                ],
            ),
            _item("tool", "INJECTEDNOISE tool result"),
            _item("assistant", "The debounce is set to zero, so every keystroke queries the model."),
        ],
    }
    cli = {
        "sessionId": CLI_ID,
        "title": "Add retries",
        "workspaceDirectory": "/work/continue-cli",
        "history": [
            _item("system", "INJECTEDNOISE system prompt"),
            {
                **_item("assistant", "INJECTEDNOISE summary of earlier turns"),
                "conversationSummary": "INJECTEDNOISE summary of earlier turns",
            },
            _item("user", "add retries to the uploader"),
            _item(
                "assistant",
                "Running the uploader tests first.",
                toolCallStates=[
                    _call(
                        "Bash",
                        {"command": "pytest tests/test_upload.py"},
                        "errored",
                        [{"content": "Error executing tool Bash: Error (exit code 2): no tests ran"}],
                    )
                ],
            ),
            _item("assistant", "The test file is missing; I created it."),
        ],
    }
    for sid, doc in ((IDE_ID, ide), (CLI_ID, cli)):
        with open(os.path.join(folder, f"{sid}.json"), "w") as f:
            json.dump(doc, f)
    with open(os.path.join(folder, "sessions.json"), "w") as f:
        json.dump(
            [
                {
                    "sessionId": IDE_ID,
                    "title": "Autocomplete lag",
                    "dateCreated": "2026-10-20T09:00:00.000Z",
                    "workspaceDirectory": f"file://{PROJECT}",
                },
                {
                    "sessionId": CLI_ID,
                    "title": "Add retries",
                    "dateCreated": "1792572000000",
                    "workspaceDirectory": "/work/continue-cli",
                },
            ],
            f,
        )


FIXTURES = {"continue": Fixture(build, "quokkaflux-continue", PROJECT, chats=2)}

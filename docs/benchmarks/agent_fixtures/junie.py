"""Junie: `~/.junie/sessions/<session-id>/events.jsonl` plus `sessions/index.jsonl`. Blocks are updated in place by
`stepId`, so a command appears first running and then failed. Shapes: deja-vu docs/registry/junie.md and
fixtures/registry/junie (MIT); every value is made up."""

import json
import os

from . import Fixture

T0 = 1_788_257_700_000  # ms
AGENT = {"kind": "MainAgent", "id": "main", "name": "main", "type": "LINEAR"}


def _step(n: int, kind: str, step: str, **body) -> dict:
    event = {"state": "IN_PROGRESS", "agentEvent": {"kind": kind, "agent": AGENT, "stepId": step, **body}}
    return {"kind": "SessionA2uxEvent", "event": event, "taskId": "task-1", "timestampMs": T0 + n * 1000}


def _put(home: str, name: str, lines: list[dict]) -> None:
    path = os.path.join(home, ".junie", "sessions", name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "events.jsonl"), "w") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))


def build(home: str) -> None:
    a, b = "session-261020-090000-k3m9", "session-261019-080000-p4q5"
    _put(
        home,
        a,
        [
            {
                "kind": "UserPromptEvent",
                "prompt": "why does the quokkaflux-junie retry loop drop an attempt",
                "presentablePrompt": "why does the quokkaflux-junie retry loop drop an attempt",
                "timestampMs": T0,
            },
            _step(1, "AgentCurrentStatusUpdatedEvent", "s0", status="INJECTEDNOISE status"),
            _step(2, "ToolBlockUpdatedEvent", "s1", text="INJECTEDNOISE Open retry.go"),
            _step(3, "TerminalBlockUpdatedEvent", "s2", status="IN_PROGRESS", command="go test ./..."),
            _step(
                4,
                "TerminalBlockUpdatedEvent",
                "s2",
                status="FAILED",
                command="go test ./...",
                output="--- FAIL: TestRetry (0.00s) INJECTEDNOISE",
                exitCode=1,
            ),
            _step(5, "MarkdownBlockUpdatedEvent", "s3", text="The loop stops one attempt early."),
            _step(
                6,
                "ResultBlockUpdatedEvent",
                "s4",
                result="Fixed the bound.<additional_context>INJECTEDNOISE</additional_context>",
            ),
            {"kind": "TaskState", "state": "COMPLETED", "timestampMs": T0 + 7000},
        ],
    )
    _put(home, b, [_step(1, "MarkdownBlockUpdatedEvent", "s1", text="Ran headless: nothing to change.")])
    with open(os.path.join(home, ".junie", "sessions", "index.jsonl"), "w") as f:
        f.write(
            json.dumps(
                {
                    "sessionId": a,
                    "createdAt": T0,
                    "updatedAt": T0 + 7000,
                    "projectDir": "/work/junie-app",
                    "taskName": "Fix the retry loop",
                }
            )
            + "\n"
        )


FIXTURES = {"junie": Fixture(build, "quokkaflux-junie", "/work/junie-app", chats=2)}

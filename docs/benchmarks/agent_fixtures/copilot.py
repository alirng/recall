"""GitHub Copilot CLI: `session-state/<id>/events.jsonl` and `workspace.yaml` as deja-vu's docs/registry/copilot.md
and its synthetic fixture (MIT) describe them. The first chat has a system message, a reasoning text, a hook's
`<system_reminder>`, a skill's synthetic turn, a failed command, a sub-agent's records (stamped `agentId`) and a
compaction summary, none of which may be indexed."""

import json
import os

from . import Fixture

PROJECT = "/workspace/quokka-copilot"
FIRST = "7cf44517-55ca-435c-893a-3fde1973a44e"
SECOND = "9a1d2e3f-4b5c-4d6e-8f70-a1b2c3d4e5f6"


def _write(session: str, events: list[dict], workspace: str) -> None:
    os.makedirs(session)
    previous = None
    with open(os.path.join(session, "events.jsonl"), "w") as f:
        for i, (kind, data, ts, extra) in enumerate(events):
            event = {"type": kind, "data": data, "id": f"{os.path.basename(session)[:8]}-{i:04d}", "timestamp": ts}
            f.write(json.dumps({**event, "parentId": previous, **extra}) + "\n")
            previous = event["id"]
    with open(os.path.join(session, "workspace.yaml"), "w") as f:
        f.write(workspace)


def _start(sid: str, ts: str) -> tuple:
    context = {"cwd": PROJECT, "gitRoot": PROJECT, "branch": "main"}
    data = {"sessionId": sid, "version": 1, "producer": "copilot-agent", "copilotVersion": "1.0.92", "startTime": ts}
    return ("session.start", {**data, "context": context}, ts, {})


def build(home: str) -> None:
    state = os.path.join(home, ".copilot", "session-state")
    call = {"toolCallId": "call_1", "name": "bash", "arguments": {"command": "pytest tests/test_pool.py"}}
    hook = "<system_reminder>INJECTEDNOISE hook context</system_reminder>"
    _write(
        os.path.join(state, FIRST),
        [
            _start(FIRST, "2026-10-20T09:00:00.000Z"),
            (
                "session.model_change",
                {"newModel": "gpt-5-mini", "reasoningEffort": None},
                "2026-10-20T09:00:01.000Z",
                {},
            ),
            (
                "system.message",
                {"role": "system", "content": "INJECTEDNOISE system prompt"},
                "2026-10-20T09:00:02.000Z",
                {},
            ),
            (
                "user.message",
                {
                    "content": f"Why does the quokkaflux-copilot pool exhaust connections?\n{hook}",
                    "transformedContent": "INJECTEDNOISE",
                },
                "2026-10-20T09:00:05.000Z",
                {},
            ),
            ("assistant.turn_start", {"turnId": "0"}, "2026-10-20T09:00:06.000Z", {}),
            (
                "assistant.message",
                {
                    "messageId": "m1",
                    "content": "I will run the pool tests first.",
                    "reasoningText": "INJECTEDNOISE reasoning",
                    "toolRequests": [call],
                },
                "2026-10-20T09:00:08.000Z",
                {},
            ),
            (
                "tool.execution_start",
                {"toolCallId": "call_1", "toolName": "bash", "arguments": call["arguments"]},
                "2026-10-20T09:00:09.000Z",
                {},
            ),
            (
                "tool.execution_complete",
                {
                    "toolCallId": "call_1",
                    "success": False,
                    "error": {"message": "bash: pytest: command not found", "code": "failure"},
                    "result": {"content": "INJECTEDNOISE tool output"},
                },
                "2026-10-20T09:00:10.000Z",
                {},
            ),
            ("subagent.started", {"agentName": "explore"}, "2026-10-20T09:00:11.000Z", {"agentId": "sub-1"}),
            (
                "user.message",
                {"content": "INJECTEDNOISE sub-agent brief"},
                "2026-10-20T09:00:12.000Z",
                {"agentId": "sub-1"},
            ),
            (
                "assistant.message",
                {"content": "INJECTEDNOISE sub-agent report"},
                "2026-10-20T09:00:13.000Z",
                {"agentId": "sub-1"},
            ),
            (
                "user.message",
                {"content": "INJECTEDNOISE skill text", "source": "skill-lint"},
                "2026-10-20T09:00:14.000Z",
                {},
            ),
            (
                "assistant.message",
                {"messageId": "m2", "content": "The metrics poller leaks rows: add rows.Close in the scan loop."},
                "2026-10-20T09:00:20.000Z",
                {},
            ),
            (
                "session.compaction_complete",
                {"success": True, "summaryContent": "INJECTEDNOISE summary", "checkpointNumber": 1},
                "2026-10-20T09:00:30.000Z",
                {},
            ),
            ("session.shutdown", {"shutdownType": "routine"}, "2026-10-20T09:00:40.000Z", {}),
        ],
        f"id: {FIRST}\nname: Connection pool exhaustion\nuser_named: false\nsummary_count: 1\n",
    )
    _write(
        os.path.join(state, SECOND),
        [
            _start(SECOND, "2026-10-21T11:00:00.000Z"),
            ("user.message", {"content": "Which linter does this repo use?"}, "2026-10-21T11:00:05.000Z", {}),
            (
                "assistant.message",
                {"content": "It uses ruff for both linting and formatting."},
                "2026-10-21T11:00:09.000Z",
                {},
            ),
        ],
        f"id: {SECOND}\nsummary_count: 0\ncreated_at: 2026-10-21T11:00:00.000Z\n",
    )


FIXTURES = {"copilot": Fixture(build, "quokkaflux-copilot", PROJECT)}

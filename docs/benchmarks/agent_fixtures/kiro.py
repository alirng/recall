"""Kiro's three stores, from deja-vu's docs/registry/kiro.md and fixtures (MIT) and agentsview's kiro tests (MIT):
a CLI transcript with its header (a streamed reply, a failed shell call, a tool result holding INJECTEDNOISE) and a
sub-agent's session, an IDE/V3 session (reasoning, a failed tool call, a tool result), and a `data.sqlite3`
conversation from a headless run. None of the INJECTEDNOISE text may be indexed."""

import json
import os
import sqlite3

from . import Fixture

PROJECT = "/work/kiro-app"
CLI_ID = "3f2a9c1e-5b7d-4e8a-9c0b-1d2e3f4a5b6c"
SUB_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
DB_ID = "c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f"
T0 = 1_792_486_800  # 2026-10-20T09:00:00Z


def _jsonl(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(json.dumps(r) for r in records) + "\n")


def _json(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f)


def _cli(kind: str, mid: str, content: list[dict], ts: float | None = None) -> dict:
    meta = {"timestamp": ts} if ts else {}
    return {"version": "v1", "kind": kind, "data": {"message_id": mid, "content": content, "meta": meta}}


def _text(text: str) -> dict:
    return {"kind": "text", "data": text}


def _data_dir(home: str) -> str:
    if os.uname().sysname == "Darwin":
        return os.path.join(home, "Library", "Application Support", "kiro-cli")
    return os.path.join(home, ".local", "share", "kiro-cli")


def build(home: str) -> None:
    cli = os.path.join(home, ".kiro", "sessions", "cli")
    use = {"toolUseId": "t1", "name": "shell", "input": {"command": "pytest tests/test_cache.py"}}
    failed = {
        "toolUseId": "t1",
        "status": "error",
        "content": [
            {"kind": "json", "data": {"exit_status": "exit status: 1", "stdout": "FAILED test_evict", "stderr": ""}}
        ],
    }
    _json(
        os.path.join(cli, f"{CLI_ID}.json"),
        {"session_id": CLI_ID, "cwd": PROJECT, "created_at": "2026-10-20T09:00:00Z", "title": "Cache eviction"},
    )
    _jsonl(
        os.path.join(cli, f"{CLI_ID}.jsonl"),
        [
            _cli("Prompt", "p1", [_text("why does the quokkaflux-kiro cache never evict?")], T0 + 5.4),
            _cli("AssistantMessage", "a1", [_text("Let me run the cache tests."), {"kind": "toolUse", "data": use}]),
            _cli("ToolResults", "r1", [{"kind": "toolResult", "data": failed}]),
            _cli("AssistantMessage", "a2", [_text("The eviction runs only when ")]),
            _cli("AssistantMessage", "a2", [_text("the cache is full, so raise the limit.")]),
            _cli("AssistantMessage", "a3", [{"kind": "toolUse", "data": {**use, "toolUseId": "t2"}}]),
            _cli(
                "ToolResults",
                "r2",
                [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "t2",
                            "status": "success",
                            "content": [{"kind": "text", "data": "INJECTEDNOISE tool output"}],
                        },
                    }
                ],
            ),
        ],
    )
    _json(
        os.path.join(cli, f"{SUB_ID}.json"),
        {"session_id": SUB_ID, "cwd": PROJECT, "parent_session_id": CLI_ID, "title": "subagent: lint"},
    )
    _jsonl(
        os.path.join(cli, f"{SUB_ID}.jsonl"),
        [
            _cli("Prompt", "p1", [_text("list the lint rules this repo enables")], T0 + 20),
            _cli("AssistantMessage", "a1", [_text("It enables the default ruff set.")]),
        ],
    )

    sess = os.path.join(home, ".kiro", "sessions", "w-kiro-app", "sess_00000000-0000-4000-8000-0000000000a1")
    _json(
        os.path.join(sess, "session.json"),
        {
            "schemaVersion": "1.0.0",
            "id": "sess_00000000-0000-4000-8000-0000000000a1",
            "title": "Run tests",
            "workspacePaths": ["/work/kiro-ide"],
            "createdAt": "2026-10-21T10:00:00.000Z",
            "lastModifiedAt": "2026-10-21T10:01:00.000Z",
        },
    )
    call = {"type": "tool_call", "toolCallId": "c1", "toolName": "execute_bash", "args": {"command": "make test"}}
    _jsonl(
        os.path.join(sess, "messages.jsonl"),
        [
            {"timestamp": "2026-10-21T10:00:05.000Z", "payload": {"type": "user", "content": "run the test suite"}},
            {
                "timestamp": "2026-10-21T10:00:08.000Z",
                "payload": {"type": "assistant", "content": "...", "operationType": "Reasoning"},
            },
            {
                "timestamp": "2026-10-21T10:00:09.000Z",
                "payload": {"type": "assistant", "content": "INJECTEDNOISE reasoning", "operationType": "Reasoning"},
            },
            {
                "timestamp": "2026-10-21T10:00:10.000Z",
                "payload": {**call, "status": "executing", "actionType": "run_command"},
            },
            {
                "timestamp": "2026-10-21T10:00:11.000Z",
                "payload": {**call, "status": "failed", "actionType": "run_command"},
            },
            {
                "timestamp": "2026-10-21T10:00:11.500Z",
                "payload": {
                    "type": "tool_result",
                    "toolCallId": "c1",
                    "content": "INJECTEDNOISE make: *** error",
                    "success": False,
                },
            },
            {"timestamp": "2026-10-21T10:00:12.000Z", "payload": {"type": "session_metadata", "key": "contextUsage"}},
            {
                "timestamp": "2026-10-21T10:00:20.000Z",
                "payload": {
                    "type": "assistant",
                    "content": "make fails: the target is missing.",
                    "operationType": "Say",
                },
            },
            {"timestamp": "2026-10-21T10:00:21.000Z", "payload": {"type": "turn_end", "stopReason": "end_turn"}},
        ],
    )

    os.makedirs(_data_dir(home))
    con = sqlite3.connect(os.path.join(_data_dir(home), "data.sqlite3"))
    con.execute(
        "create table conversations_v2 (key text not null, conversation_id text not null, value text not null, "
        "created_at integer not null, updated_at integer not null, primary key (key, conversation_id))"
    )
    history = [
        {
            "user": {
                "content": {"Prompt": {"prompt": "summarize the open TODOs"}},
                "timestamp": "2026-10-22T08:00:00Z",
            },
            "assistant": {
                "ToolUse": {
                    "content": "Searching.",
                    "tool_uses": [{"id": "u1", "name": "execute_bash", "args": {"command": "grep -rn TODO ."}}],
                }
            },
        },
        {
            "user": {
                "content": {
                    "ToolUseResults": {
                        "tool_use_results": [
                            {
                                "tool_use_id": "u1",
                                "content": [{"Json": {"exit_status": "2", "stdout": "", "stderr": "INJECTEDNOISE"}}],
                            }
                        ]
                    }
                },
                "timestamp": None,
            },
            "assistant": {"Response": {"content": "There are three TODOs, all in the parser."}},
        },
    ]
    con.execute(
        "insert into conversations_v2 values (?, ?, ?, ?, ?)",
        (
            "/work/kiro-headless",
            DB_ID,
            json.dumps({"conversation_id": DB_ID, "history": history}),
            (T0 + 86_400 * 2) * 1000,
            (T0 + 86_400 * 2 + 90) * 1000,
        ),
    )
    con.commit()
    con.close()


FIXTURES = {"kiro": Fixture(build, "quokkaflux-kiro", PROJECT, chats=4)}

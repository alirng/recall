"""Kimi Code: a session as Kimi Code 0.28 to 2.x writes it (deja-vu's docs/registry/kimi.md and its synthetic
fixtures, MIT): `state.json` and `agents/main/wire.jsonl`, with the host's own user lines, a reasoning part, a
failed shell call and a tool result that must stay out of the index."""

import json
import os

from . import Fixture

PROJECT = "/work/quokka-kimi"
FIRST = "session_aaaa1111"
SECOND = "session_bbbb2222"


def _wire(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write("\n".join(json.dumps(r) for r in records) + "\n")


def _state(session: str, title: str, created: str, updated: str) -> None:
    with open(os.path.join(session, "state.json"), "w") as f:
        json.dump(
            {
                "createdAt": created,
                "updatedAt": updated,
                "title": title,
                "isCustomTitle": False,
                "workDir": PROJECT,
                "agents": {"main": {"type": "main", "parentAgentId": None}},
                "custom": {},
            },
            f,
        )


def _user(text, t: int, **more) -> dict:
    content = text if isinstance(text, list) else [{"type": "text", "text": text}]
    return {"type": "context.append_message", "message": {"role": "user", "content": content, **more}, "time": t}


def _loop(event: dict, t: int) -> dict:
    return {"type": "context.append_loop_event", "event": event, "time": t}


def _step(n: int, t: int, *parts: dict) -> list[dict]:
    uuid = f"step_{n}"
    out = [_loop({"type": "step.begin", "uuid": uuid, "turnId": "turn_1", "step": n}, t)]
    out += [
        _loop({"type": "content.part", "uuid": f"part_{n}_{i}", "stepUuid": uuid, "part": p}, t + 1 + i)
        for i, p in enumerate(parts)
    ]
    return [
        *out,
        _loop({"type": "step.end", "uuid": uuid, "turnId": "turn_1", "step": n, "finishReason": "end_turn"}, t + 9),
    ]


def build(home: str) -> None:
    root = os.path.join(home, ".kimi-code", "sessions", "wd_quokka_0123456789ab")
    first = os.path.join(root, FIRST)
    base = 1782298800000
    records = [
        {"type": "metadata", "protocol_version": "1.4", "created_at": base},
        {"type": "config.update", "profileName": "agent", "systemPrompt": "You are Kimi Code. INJECTEDNOISE"},
        _user(
            [
                {"type": "text", "text": '<hook_result name="prompt">INJECTEDNOISE hook output</hook_result>'},
                {"type": "text", "text": "why does the quokkaflux-kimi cache go stale after rotation?"},
            ],
            base + 1000,
            toolCalls=[],
        ),
        _user(
            "<system-reminder>INJECTEDNOISE plan mode</system-reminder>",
            base + 1100,
            origin={"kind": "injection", "variant": "reminder"},
        ),
        *_step(
            0,
            base + 2000,
            {"type": "think", "text": "INJECTEDNOISE reasoning"},
            {"type": "text", "text": "Let me run the tests."},
        ),
        _loop({"type": "tool.call", "toolCallId": "t1", "name": "Bash", "args": {"command": "pytest -x"}}, base + 2100),
        _loop(
            {
                "type": "tool.result",
                "toolCallId": "t1",
                "result": {
                    "isError": True,
                    "output": "ModuleNotFoundError: No module named 'jwks'\n\nCommand failed with exit code: 1.",
                },
            },
            base + 2200,
        ),
        _loop(
            {"type": "tool.call", "toolCallId": "t2", "name": "Read", "args": {"path": f"{PROJECT}/cache.py"}},
            base + 2300,
        ),
        _loop(
            {"type": "tool.result", "toolCallId": "t2", "result": {"output": "INJECTEDNOISE file body"}}, base + 2400
        ),
        *_step(1, base + 3000, {"type": "text", "text": "The cache keeps the old key id; reload it on rotation."}),
        {
            "type": "context.apply_compaction",
            "summary": "INJECTEDNOISE summary",
            "compactedCount": 2,
            "time": base + 4000,
        },
    ]
    _wire(os.path.join(first, "agents", "main", "wire.jsonl"), records)
    _state(first, "Stale cache after rotation", "2026-10-02T10:00:00.000Z", "2026-10-02T10:05:00.000Z")

    second = os.path.join(root, SECOND)
    _wire(
        os.path.join(second, "agents", "main", "wire.jsonl"),
        [
            {"type": "metadata", "protocol_version": "1.4", "created_at": base + 60000},
            _user("rename the settings module", base + 61000),
            *_step(0, base + 62000, {"type": "text", "text": "Renamed it and updated the imports."}),
        ],
    )
    _state(second, "Untitled", "2026-10-02T11:00:00.000Z", "2026-10-02T11:01:00.000Z")


FIXTURES = {"kimi": Fixture(build, "quokkaflux-kimi", PROJECT)}

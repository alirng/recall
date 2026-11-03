"""Pi and the agents that write its session format (deja-vu's docs/registry/{pi,omp,prime,senpi,gjc,kimchi,
openclaw}.md and fixtures, MIT; SpecStory's PI-FORMAT.md, Apache-2.0), each under its own root. The first chat
of each has a reasoning block, a failed tool call, an extension's injected `custom_message` and a branch
that was abandoned (its word must stay out of the index); the second is a short chat, a sub-agent's where
the agent writes those as files of their own."""

import json
import os
import sqlite3

from . import Fixture


def _write(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(json.dumps(r) for r in records) + "\n")


def _entry(n: int, parent: int | None, kind: str, ts: str, **fields) -> dict:
    return {
        "type": kind,
        "id": f"{n:08x}",
        "parentId": None if parent is None else f"{parent:08x}",
        "timestamp": ts,
        **fields,
    }


def _message(n: int, parent: int, ts: str, role: str, content, **more) -> dict:
    message = {"role": role, "content": content, "timestamp": 1792000000000 + n * 1000, **more}
    return _entry(n, parent, "message", ts, message=message)


def _text(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def _chat(cwd: str, sid: str, word: str, day: str, slot: str | None = None, **header) -> list[dict]:
    """The first chat: a branch at the answer, with the abandoned side written before the live one."""
    t = lambda s: f"2026-10-{day}T09:{s}Z"  # noqa: E731
    out = [{"type": "title", "title": slot}] if slot else []
    out += [
        {"type": "session", "version": 3, "id": sid, "timestamp": t("00:00.000"), "cwd": cwd, **header},
        _entry(1, None, "model_change", t("00:01.000"), provider="anthropic", modelId="claude-sonnet-4"),
        _entry(2, 1, "thinking_level_change", t("00:01.500"), thinkingLevel="medium"),
        _message(
            3,
            2,
            t("00:05.000"),
            "user",
            f"Why is the {word} cache slow?\n\n<system-reminder>INJECTEDNOISE reminder</system-reminder>",
        ),
        _message(
            4,
            3,
            t("00:10.000"),
            "assistant",
            [
                {"type": "thinking", "thinking": "INJECTEDNOISE reasoning about the pool"},
                *_text(f"The {word} cache is slow because its pool is undersized."),
                {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "bash",
                    "arguments": {"command": "pytest tests/test_pool.py"},
                },
            ],
            provider="anthropic",
            model="claude-sonnet-4",
            stopReason="toolUse",
        ),
        _message(
            5,
            4,
            t("00:12.000"),
            "toolResult",
            _text("INJECTEDNOISE FAILED tests/test_pool.py::test_resize - AssertionError: pool is 4"),
            toolCallId="call_1",
            toolName="bash",
            isError=True,
        ),
        _entry(
            6,
            5,
            "custom_message",
            t("00:13.000"),
            customType="memory",
            content="INJECTEDNOISE recalled context",
            display=False,
        ),
        _message(7, 6, t("00:20.000"), "assistant", _text("The resize test fails; the pool needs to be larger.")),
        _message(8, 7, t("00:30.000"), "user", "abandonedbranchword: try a different cache"),
        _message(9, 8, t("00:31.000"), "assistant", _text("abandonedbranchword: switching caches")),
        _message(10, 7, t("00:40.000"), "user", "Raise the pool size to 32 and rerun the test"),
        _message(11, 10, t("00:50.000"), "assistant", _text("Done: the pool size is 32 and the test passes.")),
    ]
    return out


def _short(cwd: str, sid: str, day: str, ask: str, answer: str, **header) -> list[dict]:
    t = lambda s: f"2026-10-{day}T11:{s}Z"  # noqa: E731
    return [
        {"type": "session", "version": 3, "id": sid, "timestamp": t("00:00.000"), "cwd": cwd, **header},
        _message(1, None, t("00:05.000"), "user", _text(ask)),
        _message(2, 1, t("00:09.000"), "assistant", _text(answer)),
    ]


def _plain(key: str, home: str, root: str, project: str, folder: str, second: str | None = None, **first) -> None:
    """Two chats under `<home>/<root>/sessions/<folder>/<ts>_<id>.jsonl` (a flat `sessions/` for no folder)."""
    base = os.path.join(home, root, "sessions", folder)
    second = second or f"{key}-0002"
    _write(
        os.path.join(base, f"2026-10-20T09-00-00-000Z_{key}-0001.jsonl"),
        _chat(project, f"{key}-0001", f"quokkaflux-{key}", "20", **first),
    )
    _write(
        os.path.join(base, f"2026-10-21T11-00-00-000Z_{second}.jsonl"),
        _short(
            project, second, "21", "Which linter does this repo use?", "It uses ruff for both linting and formatting."
        ),
    )


def _fixture(key: str, root: str, folder: str) -> Fixture:
    project = f"/workspace/quokka-{key}"
    return Fixture(lambda home: _plain(key, home, root, project, folder), f"quokkaflux-{key}", project)


def _omp(home: str) -> None:
    _plain("omp", home, ".omp/agent", "/workspace/quokka-omp", "-workspace-quokka-omp", slot="Quokka cache tuning")
    first = os.path.join(home, ".omp/agent/sessions/-workspace-quokka-omp/2026-10-20T09-00-00-000Z_omp-0001.jsonl")
    with open(first, "a") as f:  # omp's own `developer` turns carry the system prompt
        f.write(
            json.dumps(
                _message(12, 11, "2026-10-20T09:00:51.000Z", "developer", _text("INJECTEDNOISE developer prompt"))
            )
            + "\n"
        )


def _gjc(home: str) -> None:
    """A session, and a sub-agent's pass in the folder named for it."""
    project, folder = "/workspace/quokka-gjc", os.path.join(home, ".gjc/agent/sessions/v2-quokka")
    _write(
        os.path.join(folder, "gjc-session.jsonl"),
        _chat(project, "gjc-0001", "quokkaflux-gjc", "20", title="Cache tuning"),
    )
    _write(
        os.path.join(folder, "gjc-session", "1-pass.jsonl"),
        _short(
            project,
            "gjc-0001-pass-1",
            "20",
            "Check whether the pool size is configurable",
            "It is set by POOL_SIZE.",
            title="sub-agent pass",
        ),
    )


def _kimchi(home: str) -> None:
    """A session under its `--<cwd>--` folder, and an `Agent` tool run written beside it."""
    project = "/workspace/quokka-kimchi"
    folder = os.path.join(home, ".config/kimchi/harness/sessions/--workspace-quokka-kimchi--")
    parent = os.path.join(folder, "2026-10-20T09-00-00-000Z_kimchi-0001.jsonl")
    _write(parent, _chat(project, "kimchi-0001", "quokkaflux-kimchi", "20"))
    child = _short(
        project, "kimchi-0002", "20", "Summarise retry.cfg in one line", "It retries three times.", parentSession=parent
    )
    marker = {"customType": "kimchi:subagent-session", "data": {"type": "General-Purpose"}}
    child.insert(1, _entry(1, None, "custom", "2026-10-20T11:00:01.000Z", **marker))
    _write(os.path.join(folder, "2026-10-20T11-00-00-000Z_kimchi-0002.jsonl"), child)


def _stepcode(home: str) -> None:
    """A session, and a sub-agent beside it whose header id says so."""
    _plain(
        "stepcode",
        home,
        ".stepcode/agent",
        "/workspace/quokka-stepcode",
        "--workspace-quokka-stepcode--",
        "subagent-0002",
    )


OPENCLAW = "/workspace/quokka-openclaw"
_SCHEMA = """
create table session_windows (session_id text not null primary key, session_key text not null, previous_session_id text,
  reason text, created_at integer not null, updated_at integer not null, parent_session_key text, spawned_by text,
  display_name text) strict;
create table transcript_events (session_id text not null, seq integer not null, event_json text not null,
  created_at integer not null, primary key (session_id, seq),
  foreign key (session_id) references session_windows(session_id) on delete cascade) strict;
"""


def _openclaw(home: str) -> None:
    """The live chat in the agent's SQLite store (2026.8 and later), an older one as a JSONL transcript."""
    agent = os.path.join(home, ".openclaw", "agents", "main")
    os.makedirs(os.path.join(agent, "agent"))
    first = _chat(OPENCLAW, "openclaw-0001", "quokkaflux-openclaw", "20")
    gateway = (
        "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nINJECTEDNOISE gateway context\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>"
    )
    first[3]["message"]["content"] = f"{gateway}\n\n[Tue 2026-10-20 09:00 GMT+1] " + first[3]["message"]["content"]
    con = sqlite3.connect(os.path.join(agent, "agent", "openclaw-agent.sqlite"))
    con.executescript(_SCHEMA)
    con.execute(
        "insert into session_windows values (?, 'agent:main:main', null, 'initial', ?, ?, null, null, null)",
        ("openclaw-0001", 1792000000000, 1792000050000),
    )
    con.executemany(
        "insert into transcript_events values ('openclaw-0001', ?, ?, ?)",
        [(i, json.dumps(e), 1792000000000 + i * 1000) for i, e in enumerate(first)],
    )
    con.commit()
    con.close()
    _write(
        os.path.join(agent, "sessions", "openclaw-0002.jsonl"),
        _short(
            OPENCLAW,
            "openclaw-0002",
            "21",
            "Which linter does this repo use?",
            "It uses ruff for both linting and formatting.",
        ),
    )


FIXTURES = {
    "pi": _fixture("pi", ".pi/agent", "--workspace-quokka-pi--"),
    "omp": Fixture(_omp, "quokkaflux-omp", "/workspace/quokka-omp"),
    "prime": _fixture("prime", ".prime/agent", ""),
    "omo": _fixture("omo", ".omo/agent", "--workspace-quokka-omo--"),
    "stepcode": Fixture(_stepcode, "quokkaflux-stepcode", "/workspace/quokka-stepcode"),
    "senpi": _fixture("senpi", ".senpi/agent", "-workspace-quokka-senpi"),
    "gjc": Fixture(_gjc, "quokkaflux-gjc", "/workspace/quokka-gjc"),
    "kimchi": Fixture(_kimchi, "quokkaflux-kimchi", "/workspace/quokka-kimchi"),
    "openclaw": Fixture(_openclaw, "quokkaflux-openclaw", OPENCLAW),
}

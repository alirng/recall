"""Hermes Agent's stores, from deja-vu's docs/registry/hermes.md and fixtures (MIT) and agentsview's hermes tests
(MIT): `~/.hermes/state.db` (a CLI session with a failed terminal call, a rewound row, a compaction, a reasoning
column and a tool row holding INJECTEDNOISE; a cron run; a delegate_task child), an older profile database
with no `cwd`, and the transcript files of `sessions/` (one JSONL, one JSON, and one JSON whose id the database
already holds, which must not be read twice)."""

import json
import os
import sqlite3

from . import Fixture

PROJECT = "/work/hermes-app"
T0 = 1_792_486_800.0  # 2026-10-20T09:00:00Z

SESSIONS = """create table sessions (id text primary key, source text not null, model text, model_config text,
  parent_session_id text, started_at real not null, ended_at real, title text, cwd text);"""
MESSAGES = """create table messages (id integer primary key autoincrement, session_id text not null, role text not null,
  content text, tool_call_id text, tool_calls text, tool_name text, timestamp real not null, finish_reason text,
  reasoning text, active integer not null default 1, compacted integer not null default 0);"""


def _tool_call(call_id: str, name: str, args: dict) -> str:
    return json.dumps([{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}])


def _row(con: sqlite3.Connection, sid: str, n: int, role: str, content: str | None, **extra) -> None:
    columns = {"session_id": sid, "role": role, "content": content, "timestamp": T0 + n, **extra}
    con.execute(
        f"insert into messages ({', '.join(columns)}) values ({', '.join('?' * len(columns))})", tuple(columns.values())
    )


def build(home: str) -> None:
    hermes = os.path.join(home, ".hermes")
    os.makedirs(hermes)
    con = sqlite3.connect(os.path.join(hermes, "state.db"))
    con.executescript(SESSIONS + MESSAGES)
    for sid, source, parent, config, title, cwd in (
        ("20261020_090000_a1b2c3", "cli", None, None, "Retry backoff", PROJECT),
        ("cron_digest_20261020_030000", "cron", None, None, "Nightly digest", "/work/cron"),
        (
            "20261020_091000_d4e5f6",
            "subagent",
            "20261020_090000_a1b2c3",
            json.dumps({"_delegate_from": "20261020_090000_a1b2c3"}),
            "child",
            None,
        ),
    ):
        con.execute(
            "insert into sessions values (?, ?, 'hermes-4', ?, ?, ?, ?, ?, ?)",
            (sid, source, config, parent, T0, T0 + 60, title, cwd),
        )
    s = "20261020_090000_a1b2c3"
    _row(con, s, 1, "user", "why does the quokkaflux-hermes fetcher never back off?")
    _row(
        con,
        s,
        2,
        "assistant",
        "Let me run the fetcher tests.",
        tool_calls=_tool_call("call_1", "terminal", {"command": "pytest tests/test_fetch.py"}),
        reasoning="INJECTEDNOISE thinking about the fetcher",
    )
    _row(
        con,
        s,
        3,
        "tool",
        json.dumps({"output": "FAILED test_backoff", "exit_code": 1, "error": None}),
        tool_call_id="call_1",
        tool_name="terminal",
    )
    _row(con, s, 4, "assistant", "The sleep sits in the success branch, so it never runs on a retry.")
    _row(con, s, 5, "user", "INJECTEDNOISE a turn the person rewound", active=0)
    _row(con, s, 6, "user", "does the same hold for the poller module as well?", active=0, compacted=1)
    _row(con, s, 7, "user", "[CONTEXT COMPACTION - REFERENCE ONLY] INJECTEDNOISE summary of earlier turns")
    _row(con, s, 8, "user", "does the same hold for the poller module as well?")  # the tail's copy of the archived turn
    _row(con, s, 9, "assistant", "Yes, the poller has the same bug.")
    _row(con, s, 10, "tool", "INJECTEDNOISE plain tool output", tool_call_id="call_2", tool_name="read_file")
    _row(con, "cron_digest_20261020_030000", 1, "user", "summarize yesterday's commits")
    _row(con, "20261020_091000_d4e5f6", 1, "user", "check the poller for the same bug")
    _row(con, "20261020_091000_d4e5f6", 2, "assistant", "It has the same bug.")
    con.commit()
    con.close()

    profile = os.path.join(hermes, "profiles", "architect")
    os.makedirs(profile)
    con = sqlite3.connect(os.path.join(profile, "state.db"))
    con.execute(
        "create table messages (id integer primary key autoincrement, session_id text not null, role text not null, "
        "content text, timestamp real not null)"
    )
    for n, (role, text) in enumerate(
        (("user", "why msgpack over json for the wire format"), ("assistant", "Payloads are mostly float arrays.")), 100
    ):
        con.execute(
            "insert into messages (session_id, role, content, timestamp) values ('01JCH1', ?, ?, ?)",
            (role, text, T0 + n),
        )
    con.commit()
    con.close()

    sessions = os.path.join(hermes, "sessions")
    os.makedirs(sessions)
    with open(os.path.join(sessions, "20261021_090000_ef34ab.jsonl"), "w") as f:
        for line in (
            {"role": "session_meta", "model": "hermes-4", "platform": "cli", "timestamp": "2026-10-21T09:00:00.000000"},
            {"role": "user", "content": "list the todo comments", "timestamp": "2026-10-21T09:00:05.000000"},
            {
                "role": "assistant",
                "content": "",
                "reasoning": "INJECTEDNOISE",
                "tool_calls": json.loads(_tool_call("c1", "terminal", {"command": "grep -rn TODO ."})),
                "timestamp": "2026-10-21T09:00:06.000000",
            },
            {
                "role": "tool",
                "content": "INJECTEDNOISE grep output",
                "tool_call_id": "c1",
                "timestamp": "2026-10-21T09:00:07.000000",
            },
            {
                "role": "assistant",
                "content": "Two TODOs, both in the parser.",
                "timestamp": "2026-10-21T09:00:09.000000",
            },
        ):
            f.write(json.dumps(line) + "\n")
    with open(os.path.join(sessions, "session_20261022_100000_aa11bb.json"), "w") as f:
        json.dump(
            {
                "session_id": "20261022_100000_aa11bb",
                "platform": "cli",
                "session_start": "2026-10-22T10:00:00.000000",
                "last_updated": "2026-10-22T10:01:00.000000",
                "messages": [
                    {"role": "user", "content": "rename the config loader"},
                    {"role": "assistant", "content": "Renamed it to load_config."},
                ],
            },
            f,
        )
    with open(os.path.join(sessions, "session_20261020_090000_a1b2c3.json"), "w") as f:
        json.dump(
            {"messages": [{"role": "user", "content": "INJECTEDNOISE the database already holds this session"}]}, f
        )


FIXTURES = {"hermes": Fixture(build, "quokkaflux-hermes", PROJECT, chats=6)}

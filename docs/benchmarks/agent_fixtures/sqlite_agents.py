"""Goose's `sessions.db` and Crush's per-project `crush.db`. The tables carry the columns the readers use, as
agentsview's parsers (MIT) list them; every row is made up."""

import json
import os
import sqlite3

from . import Fixture

T0 = 1_761_000_000


def _db(path: str, ddl: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(ddl)
    return con


def build_goose(home: str) -> None:
    con = _db(
        os.path.join(home, ".local", "share", "goose", "sessions", "sessions.db"),
        """create table sessions (id text primary key, name text, description text, session_type text,
             working_dir text, created_at text, updated_at text, parent_session_id text);
           create table messages (id integer primary key autoincrement, message_id text, session_id text, role text,
             content_json text, created_timestamp integer, timestamp text, tokens integer, metadata_json text);""",
    )
    con.execute(
        "insert into sessions values ('g1', 'chat', 'Speed up the quokkaflux-goose importer', 'user', "
        "'/work/goose-app', '2026-10-20 09:00:00', '2026-10-20 09:05:00', null)"
    )
    con.execute(
        "insert into sessions values ('g2', 'run', 'Nightly digest', 'scheduled', '/work/cron', null, null, null)"
    )
    rows = [
        ("user", [{"type": "text", "text": "speed up the quokkaflux-goose importer"}], {}),
        (
            "assistant",
            [
                {"type": "thinking", "thinking": "INJECTEDNOISE"},
                {"type": "text", "text": "Profiling first."},
                {"type": "toolRequest", "id": "t1", "toolCall": {"value": {"name": "shell"}}},
            ],
            {},
        ),
        ("user", [{"type": "text", "text": "INJECTEDNOISE context the person never saw"}], {"userVisible": False}),
    ]
    for i, (role, blocks, meta) in enumerate(rows):
        con.execute(
            "insert into messages values (null, ?, 'g1', ?, ?, ?, null, 0, ?)",
            (f"m{i}", role, json.dumps(blocks), T0 + i, json.dumps(meta)),
        )
    con.execute(
        "insert into messages values (null, 'n0', 'g2', 'user', ?, ?, null, 0, '{}')",
        (json.dumps([{"type": "text", "text": "summarize yesterday's commits"}]), T0),
    )
    con.commit()
    con.close()


def build_crush(home: str) -> None:
    project = os.path.join(home, "work", "crush-app")
    con = _db(
        os.path.join(project, ".crush", "crush.db"),
        """create table sessions (id text primary key, parent_session_id text, title text, message_count integer,
             prompt_tokens integer, completion_tokens integer, cost real, created_at integer, updated_at integer);
           create table messages (id text primary key, session_id text, role text, parts text, model text,
             provider text, created_at integer, updated_at integer, is_summary_message integer default 0);""",
    )
    con.execute(
        "insert into sessions values ('c1', null, 'Fix the quokkaflux-crush flake', 3, 0, 0, 0, ?, ?)", (T0, T0 + 9)
    )
    con.execute("insert into sessions values ('c2', null, 'Second chat', 1, 0, 0, 0, ?, ?)", (T0 + 100, T0 + 100))
    rows = [
        ("user", [{"type": "text", "data": {"text": "fix the quokkaflux-crush flake"}}], 0),
        (
            "assistant",
            [
                {"type": "reasoning", "data": {"thinking": "INJECTEDNOISE"}},
                {"type": "text", "data": {"text": "It is a race in the cache."}},
                {"type": "tool_call", "data": {"id": "t1", "name": "bash", "input": "{}"}},
                {"type": "finish", "data": {"reason": "end_turn"}},
            ],
            0,
        ),
        ("tool", [{"type": "tool_result", "data": {"tool_call_id": "t1", "content": "INJECTEDNOISE output"}}], 0),
        ("user", [{"type": "text", "data": {"text": "INJECTEDNOISE summary of earlier turns"}}], 1),
    ]
    for i, (role, parts, summary) in enumerate(rows):
        con.execute(
            "insert into messages values (?, 'c1', ?, ?, 'm', 'p', ?, ?, ?)",
            (f"cm{i}", role, json.dumps(parts), T0 + i, T0 + i, summary),
        )
    con.execute(
        "insert into messages values ('cm9', 'c2', 'user', ?, 'm', 'p', ?, ?, 0)",
        (json.dumps([{"type": "text", "data": {"text": "second crush chat"}}]), T0 + 100, T0 + 100),
    )
    con.commit()
    con.close()
    registry = os.path.join(home, ".local", "share", "crush", "projects.json")
    os.makedirs(os.path.dirname(registry), exist_ok=True)
    with open(registry, "w") as f:
        json.dump({"projects": [{"path": project, "data_dir": ".crush", "last_accessed": T0}]}, f)


FIXTURES = {
    "goose": Fixture(build_goose, "quokkaflux-goose", "/work/goose-app", chats=2),
    "crush": Fixture(build_crush, "quokkaflux-crush", None, chats=2),
}

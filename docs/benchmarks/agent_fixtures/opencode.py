"""OpenCode and Kilo Code: one SQLite file each. OpenCode's holds both schema families (v1 `session`, `message`,
`part`; v2 `session_v2`, `session_message`), which is what a v1 install looks like once v2 has first run.
The tables are OpenCode's own DDL (anomalyco/opencode, MIT); the rows are made up."""

import json
import os
import sqlite3

from . import Fixture

V1 = """
create table project (id text primary key, worktree text not null, time_created integer not null,
  time_updated integer not null, sandboxes text not null);
create table session (id text primary key, project_id text not null, parent_id text, slug text not null,
  directory text not null, title text not null, version text not null, time_created integer not null,
  time_updated integer not null);
create table message (id text primary key, session_id text not null, time_created integer not null,
  time_updated integer not null, data text not null);
create table part (id text primary key, message_id text not null, session_id text not null,
  time_created integer not null, time_updated integer not null, data text not null);
"""
V2 = """
create table session_v2 (id text primary key, project_id text not null, parent_id text, slug text not null,
  directory text not null, title text, version text not null, time_created integer not null,
  time_updated integer not null);
create table session_message (id text primary key, session_id text not null, type text not null, seq integer not null,
  time_created integer not null, time_updated integer not null, data text not null);
"""
T0 = 1_761_000_000_000  # ms


def _v1(con: sqlite3.Connection, query: str, directory: str) -> None:
    con.execute("insert into project values ('p1', ?, ?, ?, '[]')", (directory, T0, T0))
    con.execute(
        "insert into session values ('ses_a1', 'p1', null, 'a', ?, 'Fix the flaky retry', '1.18.0', ?, ?)",
        (directory, T0, T0 + 9000),
    )
    rows = [
        ("msg_1", {"role": "user"}, [("prt_1", {"type": "text", "text": f"why does the {query} retry loop spin?"})]),
        (
            "msg_2",
            {"role": "assistant"},
            [
                ("prt_2", {"type": "reasoning", "text": "INJECTEDNOISE thinking"}),
                (
                    "prt_3",
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "c1",
                        "state": {
                            "status": "error",
                            "input": {"command": "pytest -x"},
                            "error": "Error: 3 tests failed",
                        },
                    },
                ),
                ("prt_4", {"type": "text", "text": "The loop never backs off; add a delay."}),
            ],
        ),
        (
            "msg_3",
            {"role": "user"},
            [("prt_5", {"type": "text", "text": "INJECTEDNOISE file contents", "synthetic": True})],
        ),
    ]
    for i, (mid, data, parts) in enumerate(rows):
        con.execute("insert into message values (?, 'ses_a1', ?, ?, ?)", (mid, T0 + i, T0 + i, json.dumps(data)))
        for pid, part in parts:
            con.execute(
                "insert into part values (?, ?, 'ses_a1', ?, ?, ?)", (pid, mid, T0 + i, T0 + i, json.dumps(part))
            )


def _v2(con: sqlite3.Connection, directory: str) -> None:
    con.execute(
        "insert into session_v2 values ('ses_b2', 'p1', null, 'b', ?, 'Plan the schema change', '2.0.15', ?, ?)",
        (directory, T0 + 1000, T0 + 5000),
    )
    rows = [
        ("user", {"text": "outline the migration for the orders table"}),
        ("system", {"text": "INJECTEDNOISE system prompt"}),
        (
            "assistant",
            {"content": [{"type": "reasoning", "text": "INJECTEDNOISE"}, {"type": "text", "text": "Three steps."}]},
        ),
    ]
    for seq, (kind, data) in enumerate(rows):
        con.execute(
            "insert into session_message values (?, 'ses_b2', ?, ?, ?, ?, ?)",
            (f"msg_v{seq}", kind, seq, T0 + 1000 + seq, T0 + 1000 + seq, json.dumps(data)),
        )


def _db(home: str, app: str, name: str, ddl: tuple[str, ...], fill) -> None:
    folder = os.path.join(home, ".local", "share", app)
    os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(os.path.join(folder, name))
    for sql in ddl:
        con.executescript(sql)
    fill(con)
    con.commit()
    con.close()


def build_opencode(home: str) -> None:
    def fill(con):
        _v1(con, "quokkaflux-opencode", "/work/quokka")
        _v2(con, "/work/quokka")

    _db(home, "opencode", "opencode.db", (V1, V2), fill)


def build_kilo(home: str) -> None:
    _db(home, "kilo", "kilo.db", (V1,), lambda con: _v1(con, "quokkaflux-kilo", "/work/kilo-quokka"))


FIXTURES = {
    "opencode": Fixture(build_opencode, "quokkaflux-opencode", "/work/quokka"),
    "kilo": Fixture(build_kilo, "quokkaflux-kilo", "/work/kilo-quokka", chats=1),
}

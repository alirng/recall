"""Devin CLI's `<data>/devin/cli/sessions.db`, from deja-vu's docs/registry/devin.md and its devin.sql fixture
(MIT). The first chat has a rebuilt prefix pair, a failed exec call, an unflagged system row (a hook's injected
context) and a replay of the user turn under the same message_id; the second has a `run_subagent` run its
`subagent_heads` row declares and a chain a rewind orphaned; the third is hidden and must not be read."""

import json
import os
import sqlite3

from . import Fixture

PROJECT = "/work/devin-app"
T0 = 1_792_486_800

SCHEMA = """
create table sessions (id text primary key, working_directory text, backend_type text, model text, agent_mode text,
  created_at integer, last_activity_at integer, title text, main_chain_id integer, shell_last_seen_index integer,
  cogs_json text, workspace_dirs text, hidden integer not null default 0, metadata text);
create table message_nodes (row_id integer primary key autoincrement, session_id text not null,
  node_id integer not null, parent_node_id integer, chat_message text not null, created_at integer, metadata text);
create table subagent_heads (session_id text not null, agent_id text not null, chain_node_id integer not null,
  updated_at integer not null, primary key (session_id, agent_id));
create table prompt_history (id integer primary key autoincrement, content text, timestamp integer, session_id text,
  is_shell integer not null default 0);
"""


def _node(
    con: sqlite3.Connection, sid: str, node: int, parent: int | None, msg: dict, meta: dict | None = None
) -> None:
    con.execute(
        "insert into message_nodes (session_id, node_id, parent_node_id, chat_message, created_at, metadata) "
        "values (?, ?, ?, ?, ?, ?)",
        (sid, node, parent, json.dumps(msg), T0 + node, json.dumps(meta) if meta else None),
    )


def build(home: str) -> None:
    path = os.path.join(home, ".local", "share", "devin", "cli", "sessions.db")
    os.makedirs(os.path.dirname(path))
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for row in (
        ("devin-1", PROJECT, "Retry loop", 8, 0),
        ("devin-2", PROJECT, "Run the worker", 13, 0),
        ("devin-hidden", PROJECT, "Hidden", 16, 1),
    ):
        con.execute(
            "insert into sessions values (?, ?, 'cli', 'swe-2', 'normal', ?, ?, ?, ?, 0, null, null, ?, null)",
            (row[0], row[1], T0, T0 + 90, row[2], row[3], row[4]),
        )
    prefix = {"is_system_prefix": True}
    _node(
        con,
        "devin-1",
        1,
        None,
        {"message_id": "sys-1", "role": "system", "content": "INJECTEDNOISE system prompt"},
        prefix,
    )
    _node(con, "devin-1", 2, 1, {"message_id": "sys-2", "role": "system", "content": "INJECTEDNOISE rules"}, prefix)
    _node(
        con,
        "devin-1",
        3,
        2,
        {
            "message_id": "u-1",
            "role": "user",
            "content": "why does the quokkaflux-devin retry loop drop the last attempt",
        },
    )
    call = {"id": "call_1", "name": "exec", "arguments": {"command": "go test ./loop"}, "index": 0, "kind": "function"}
    _node(
        con,
        "devin-1",
        4,
        3,
        {"message_id": "a-1", "role": "assistant", "content": "Checking the loop.", "tool_calls": [call]},
    )
    _node(
        con,
        "devin-1",
        5,
        4,
        {
            "message_id": "t-1",
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Output from command in shell abc:\nINJECTEDNOISE FAIL attempt three\n\n\nExit code: 1",
            "metadata": {"extensions": {"chisel/terminal_output": {"exit": {"exit_code": 1}}}},
        },
    )
    edit = {
        "id": "call_2",
        "name": "edit",
        "arguments": {"file_path": "/work/devin-app/loop.go", "old_string": "i < n", "new_string": "i <= n"},
    }
    _node(
        con,
        "devin-1",
        6,
        5,
        {"message_id": "a-2", "role": "assistant", "content": "The bound is off by one.", "tool_calls": [edit]},
    )
    _node(con, "devin-1", 7, 6, {"message_id": "inj-1", "role": "system", "content": "INJECTEDNOISE hook context"})
    _node(
        con,
        "devin-1",
        8,
        7,
        {
            "message_id": "u-1",
            "role": "user",
            "content": "why does the quokkaflux-devin retry loop drop the last attempt",
        },
        {"extensions": {"compact/prior_node_ids": [3]}},
    )

    _node(
        con,
        "devin-2",
        9,
        None,
        {"message_id": "p-sys", "role": "system", "content": "INJECTEDNOISE parent prompt"},
        prefix,
    )
    _node(con, "devin-2", 10, 9, {"message_id": "p-u", "role": "user", "content": "run the worker"})
    spawn = {
        "id": "call_9",
        "name": "run_subagent",
        "arguments": {"instructions": "echo done"},
        "index": 0,
        "kind": "function",
    }
    _node(con, "devin-2", 11, 10, {"message_id": "p-a", "role": "assistant", "content": "", "tool_calls": [spawn]})
    _node(
        con,
        "devin-2",
        12,
        11,
        {"message_id": "p-t", "role": "tool", "tool_call_id": "call_9", "content": "INJECTEDNOISE started"},
    )
    _node(con, "devin-2", 13, 12, {"message_id": "p-a2", "role": "assistant", "content": "The worker finished."})
    _node(con, "devin-2", 14, None, {"message_id": "w-u", "role": "user", "content": "echo done"})
    _node(con, "devin-2", 15, 14, {"message_id": "w-a", "role": "assistant", "content": "It printed done."})
    _node(con, "devin-2", 16, None, {"message_id": "rw-u", "role": "user", "content": "an earlier draft of the ask"})
    _node(
        con, "devin-2", 17, 16, {"message_id": "rw-a", "role": "assistant", "content": "an earlier draft of the answer"}
    )
    con.execute("insert into subagent_heads values ('devin-2', 'ag-worker', 15, ?)", (T0 + 107,))

    _node(
        con,
        "devin-hidden",
        16,
        None,
        {"message_id": "h-u", "role": "user", "content": "INJECTEDNOISE a session its owner removed"},
    )
    con.execute(
        "insert into prompt_history (content, timestamp, session_id, is_shell) values ('git status', ?, 'devin-1', 1)",
        (T0 + 40,),
    )
    con.commit()
    con.close()


FIXTURES = {"devin": Fixture(build, "quokkaflux-devin", PROJECT, chats=4)}

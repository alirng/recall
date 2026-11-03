"""Zed's agent panel: `threads/threads.db`, a `threads` table whose `data` is a zstd-compressed (or, in rows from
before compression, plain) thread document, in the 0.3.0 shape (externally tagged `User` / `Agent` messages) and the
0.2.0 one (`role` + `segments`). Shapes: deja-vu docs/registry/zed.md (MIT); every value is made up."""

import io
import json
import os
import sqlite3
import subprocess

from . import Fixture

SUPPORT = "Library/Application Support"  # macOS; the eval runs on a Mac, where roots.expand picks this
SCHEMA = """
create table threads (id text primary key, summary text not null, updated_at text not null,
  data_type text not null, data blob not null, parent_id text, folder_paths text, folder_paths_order text,
  created_at text);
"""


def _zstd(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+

        return zstd.compress(data)
    except ImportError:
        pass
    try:
        import zstandard

        return zstandard.ZstdCompressor().stream_reader(io.BytesIO(data)).read()
    except ImportError:
        return subprocess.run(["zstd", "-c"], input=data, capture_output=True, check=True).stdout


def build(home: str) -> None:
    current = {
        "version": "0.3.0",
        "title": "Read the thread store",
        "updated_at": "2026-10-20T09:00:09+00:00",
        "messages": [
            {
                "User": {
                    "id": "u1",
                    "content": [
                        {"Text": "where does zed keep quokkaflux-zed threads?"},
                        {"Mention": {"uri": {"File": {"abs_path": "/work/zed-app/a.rs"}}, "content": "INJECTEDNOISE"}},
                    ],
                }
            },
            {
                "Agent": {
                    "content": [
                        {"Thinking": {"text": "INJECTEDNOISE thinking", "signature": None}},
                        {"Text": "Let me look at the data dir."},
                        {"ToolUse": {"id": "t1", "name": "terminal", "raw_input": "{}", "input": {"command": "ls"}}},
                    ],
                    "tool_results": {
                        "t1": {
                            "tool_use_id": "t1",
                            "tool_name": "terminal",
                            "is_error": True,
                            "content": {"Text": "ls: threads: No such file or directory INJECTEDNOISE"},
                            "output": None,
                        }
                    },
                }
            },
            "Resume",
            {"Agent": {"content": [{"Text": "They are in threads/threads.db."}], "tool_results": {}}},
        ],
    }
    legacy = {
        "version": "0.2.0",
        "summary": "Legacy thread",
        "updated_at": "2026-10-19T08:00:02+00:00",
        "messages": [
            {"id": 1, "role": "user", "segments": [{"type": "text", "text": "does the old shape still load?"}]},
            {
                "id": 2,
                "role": "assistant",
                "segments": [
                    {"type": "thinking", "text": "INJECTEDNOISE", "signature": None},
                    {"type": "text", "text": "Yes, it does."},
                ],
            },
        ],
    }
    sub = {
        "version": "0.3.0",
        "title": "Subagent run",
        "messages": [
            {"User": {"id": "s1", "content": [{"Text": "summarise the readme"}]}},
            {"Agent": {"content": [{"Text": "It describes the project."}], "tool_results": {}}},
        ],
    }
    path = os.path.join(home, SUPPORT, "Zed", "threads", "threads.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rows = [
        (
            "t-1",
            "Read the thread store",
            "2026-10-20T09:00:09+00:00",
            "zstd",
            _zstd(json.dumps(current).encode()),
            None,
            "/work/zed-app",
        ),
        ("t-2", "Legacy thread", "2026-10-19T08:00:02+00:00", "json", json.dumps(legacy).encode(), None, None),
        ("t-3", "Subagent run", "2026-10-20T09:00:05+00:00", "json", json.dumps(sub).encode(), "t-1", "/work/zed-app"),
    ]
    con.executemany(
        "insert into threads (id, summary, updated_at, data_type, data, parent_id, folder_paths, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?)",
        [(*r, r[2]) for r in rows],
    )
    con.commit()
    con.close()


FIXTURES = {"zed": Fixture(build, "quokkaflux-zed", "/work/zed-app", chats=3)}

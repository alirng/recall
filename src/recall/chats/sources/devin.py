"""Devin CLI history (Cognition): `<data>/devin/cli/sessions.db` (`cli_sessions.db` beside it is the pre-rename
copy), read with mode=ro.

`sessions` holds the chat (id, title, working_directory, created_at and last_activity_at in Unix seconds,
main_chain_id, hidden: a hidden session is one its owner removed from view and is not read) and `message_nodes`
its turns, linked by parent_node_id. The table in order is not the conversation: the agent rebuilds its context
chain when the system prefix changes, so several copies share a `message_id`, and `main_chain_id` names the head
of the live one. The chat is the walk from that head back to the root, one copy of each message_id. A node's
`chat_message` is `{message_id, role, content, tool_calls[{id, name, arguments}], tool_call_id, metadata}`: only
the `user` and `assistant` text is kept; a `system` row is the prompt or context something injected (a hook's), and
a `tool` row is output, whose `metadata.extensions."chisel/terminal_output".exit.exit_code` says whether a
command failed.

A chain the main one does not reach is a conversation of its own: a `run_subagent` run (the `subagent_heads` table
names the ones a build declares) or a branch a rewind left behind. Each is read as a subagent session
`<session>:<agent id, or the first message id of its part>`.

Format knowledge: deja-vu docs/registry/devin.md and internal/sources/devin.go (MIT), agentsview
internal/parser/devin.go (MIT).
"""

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass

from ... import roots
from ..errors import Calls, position, signature
from ..transcript import Message, Session, clean_assistant_text, clean_user_text
from . import Item, append, iso_ts, open_ro, table_names

CHAT_SCHEMA = "2026-11-03 new source: Devin CLI"

KEY = "devin"
_NAMES = ("sessions.db", "cli_sessions.db")


@dataclass
class _Node:
    id: int
    parent: int | None
    mid: str
    created: int | None
    msg: dict | None = None


def _databases() -> list[str]:
    out, seen = [], set()
    for folder in roots.other_dirs(KEY):
        for name in _NAMES:
            path = os.path.join(folder, "cli", name)
            if os.path.isfile(path) and os.path.realpath(path) not in seen:
                seen.add(os.path.realpath(path))
                out.append(path)
    return out


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"pragma table_info({table})")}


def _sessions(con: sqlite3.Connection) -> list[tuple]:
    """(id, last_activity_at, main_chain_id, node count, newest node) of each session that is not hidden."""
    if not {"sessions", "message_nodes"} <= table_names(con):
        return []
    columns = _columns(con, "sessions")
    head = "s.main_chain_id" if "main_chain_id" in columns else "null"
    shown = "where coalesce(s.hidden, 0) = 0" if "hidden" in columns else ""
    return con.execute(
        f"select s.id, coalesce(s.last_activity_at, 0), {head}, coalesce(m.n, 0), coalesce(m.t, 0) from sessions s "
        "left join (select session_id, count(*) n, max(node_id) t from message_nodes group by 1) m "
        f"on m.session_id = s.id {shown}"
    ).fetchall()


def _nodes(con: sqlite3.Connection, sid: str, full: bool) -> dict[int, _Node]:
    """The session's nodes by id. Without `full` the messages are not parsed: only their ids are read."""
    chat = (
        "chat_message"
        if full
        else "case when json_valid(chat_message) then json_extract(chat_message, '$.message_id') end"
    )
    out = {}
    for node, parent, created, body in con.execute(
        f"select node_id, parent_node_id, created_at, {chat} from message_nodes where session_id = ? order by node_id",
        (sid,),
    ):
        msg = None
        if full:
            with contextlib.suppress(ValueError, TypeError):
                msg = json.loads(body)
            msg = msg if isinstance(msg, dict) else {}  # a node that is not a message still links its chain
        mid = msg.get("message_id") if full else body
        out[node] = _Node(node, parent, mid if isinstance(mid, str) else "", created, msg)
    return out


def _chain(nodes: dict[int, _Node], head: int) -> list[_Node]:
    """The nodes from the root to `head`, the first copy of each message id (a replay after a rebuild comes later)."""
    walk, n = [], nodes.get(head)
    for _ in range(len(nodes) + 1):
        if n is None:
            break
        walk.append(n)
        n = nodes.get(n.parent) if n.parent is not None else None
    chain, seen = [], set()
    for n in reversed(walk):
        if not n.mid or n.mid not in seen:
            seen.add(n.mid)
            chain.append(n)
    return chain


def _split(nodes: dict[int, _Node], head: int | None, declared: list[tuple[str, int]]):
    """(the main chain, [(subagent id, its chain)]): what the main chain does not reach, declared runs first,
    then each remaining leaf, newest first, cut at the first node an earlier chain already carried."""
    main = _chain(nodes, max(nodes) if head not in nodes else head)
    done_nodes = {n.id for n in main}
    done_mids = {n.mid for n in main if n.mid}
    subs = []

    def take(chain: list[_Node], sub: str) -> None:
        subs.append((sub, chain))
        done_nodes.update(n.id for n in chain)
        done_mids.update(n.mid for n in chain if n.mid)

    for agent, node in declared:
        if node in nodes and (chain := _chain(nodes, node)):
            take(chain, agent)
    parents = {n.parent for n in nodes.values() if n.parent is not None}
    for leaf in sorted(
        (n for n in nodes.values() if n.id not in done_nodes and n.id not in parents), key=lambda n: -n.id
    ):
        chain = _chain(nodes, leaf.id)
        i = len(chain)
        while i > 0 and chain[i - 1].id not in done_nodes and chain[i - 1].mid not in done_mids:
            i -= 1
        if suffix := chain[i:]:
            take(suffix, suffix[0].mid[:8] or f"n{suffix[0].id}")
    return main, subs


def _declared(con: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = {}
    if "subagent_heads" in table_names(con):
        with contextlib.suppress(sqlite3.Error):
            for sid, agent, node in con.execute("select session_id, agent_id, chain_node_id from subagent_heads"):
                out.setdefault(sid, []).append((str(agent), node))
    return out


def _roots(con: sqlite3.Connection) -> set[str]:
    """Sessions with more than one chain root: the only ones that can hold a side chain."""
    return {
        r[0]
        for r in con.execute(
            "select session_id from message_nodes where parent_node_id is null group by 1 having count(*) > 1"
        )
    }


def detect() -> int | None:
    dbs = _databases()
    if not dbs:
        return None
    total = 0
    for db in dbs:
        if con := open_ro(db):
            with contextlib.suppress(sqlite3.Error):
                total += len(_sessions(con))
            con.close()
    return total


def items():
    seen: set[str] = set()  # a session in both databases is read once, from the newer name's
    for db in _databases():
        con = open_ro(db)
        if con is None:
            continue
        found = []
        try:
            sessions = _sessions(con)
            declared, multi = _declared(con), _roots(con) if sessions else set()
            for sid, updated, head, n, last in sessions:
                if sid in seen:
                    continue
                seen.add(sid)
                fingerprint = f"{updated}:{head}:{n}:{last}"
                subs = []
                if sid in multi or sid in declared:
                    nodes = _nodes(con, sid, full=False)
                    subs = [sub for sub, _ in _split(nodes, head, declared.get(sid, []))[1]] if nodes else []
                found.append((sid, fingerprint, subs))
        except sqlite3.Error:  # an unknown schema, or a database busy beyond the timeout
            continue
        finally:
            con.close()
        for sid, fingerprint, subs in found:
            yield Item(f"{db}#{sid}", fingerprint, lambda d=db, i=sid: load(d, i))
            for sub in subs:
                yield Item(f"{db}#{sid}:{sub}", fingerprint, lambda d=db, i=sid, u=sub: load(d, i, u))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))


def _extension(msg: dict, name: str, *path: str):
    """A value under the tool node's `metadata.extensions.<name>`, None when any step is missing."""
    value = (msg.get("metadata") or {}).get("extensions") if isinstance(msg.get("metadata"), dict) else None
    for step in (name, *path):
        value = value.get(step) if isinstance(value, dict) else None
    return value


def _calls(call) -> dict[str, tuple[str, object]]:
    out = {}
    for c in call if isinstance(call, list) else []:
        if isinstance(c, dict) and isinstance(c.get("id"), str):
            args = c.get("arguments")
            if isinstance(args, str):
                with contextlib.suppress(ValueError):
                    args = json.loads(args)
            out[c["id"]] = (c.get("name") or "", args)
    return out


def load(db: str, sid: str, sub: str | None = None) -> Session | None:
    con = open_ro(db)
    if con is None:
        return None
    try:
        columns = _columns(con, "sessions")
        main_chain = "main_chain_id" if "main_chain_id" in columns else "null"
        row = con.execute(
            "select coalesce(title, ''), coalesce(working_directory, ''), created_at, last_activity_at, "
            f"{main_chain} from sessions where id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        title, folder, created, updated, head = row
        nodes = _nodes(con, sid, full=True)
        main, subs = _split(nodes, head, _declared(con).get(sid, [])) if nodes else ([], [])
    finally:
        con.close()
    chain = main if sub is None else next((c for name, c in subs if name == sub), None)
    if chain is None:
        return None
    s = Session(KEY, sid if sub is None else f"{sid}:{sub}", f"{db}#{sid}" + ("" if sub is None else f":{sub}"))
    s.root, s.project = KEY, folder or None
    if sub is None:
        s.title = title.strip() or None
        s.started_at, s.updated_at = iso_ts(created), iso_ts(updated)
    else:
        s.origin, s.parent = "subagent", f"{KEY}:{sid}"
        s.started_at = s.updated_at = iso_ts(chain[0].created)
    messages: list[Message] = []
    calls = Calls()
    pending: dict[str, tuple[str, object]] = {}
    for n in chain:
        msg, ts = n.msg or {}, iso_ts(n.created)
        role = msg.get("role")
        if role == "user":
            append(messages, "user", clean_user_text(_text(msg.get("content"))), ts)
        elif role == "assistant":
            append(messages, "assistant", clean_assistant_text(_text(msg.get("content"))), ts)
            pending.update(_calls(msg.get("tool_calls")))
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            name, args = pending.pop(call_id, ("", None)) if isinstance(call_id, str) else ("", None)
            code = _extension(msg, "chisel/terminal_output", "exit", "exit_code")
            ok = _extension(msg, "chisel/tool_result_meta", "success")
            failed = ok is False or (isinstance(code, int) and code != 0)
            failure = (signature(_text(msg.get("content"))) or "error") if failed else None
            calls.add(*position(messages), ts, name, args, call_id, failure)
    s.messages = messages
    s.tool_errors = calls.errors()
    return s if messages else None


AGENTS = {KEY: ("Devin CLI", detect, items)}

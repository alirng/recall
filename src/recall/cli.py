"""Command-line interface: `recall <command>`. Run `recall --help` for the list."""

import argparse
import json
import os
import sys
import time

from . import __version__, config, paths
from .chats import indexer, store


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except KeyboardInterrupt:
        sys.exit(130)


# --- chat history -------------------------------------------------------------


def cmd_index(a) -> None:
    started = time.time()
    result = indexer.index(store.connect(), only=a.source, force=a.force, log=lambda m: print(m, file=sys.stderr))
    for name, r in result.items():
        print(f"{name:14} {r['indexed']:5} indexed  {r['unchanged']:5} unchanged")
    print(f"done in {time.time() - started:.1f}s → {paths.DB}")


def cmd_search(a) -> None:
    con = store.connect()
    indexer.index(con, max_age=60)
    if a.cmd == "search":
        rows = store.search(con, " ".join(a.query), a.agent, a.project, a.since, a.limit)
    else:
        rows = store.recent(con, a.agent, a.project, a.since, a.limit)
    if a.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        print(f"{r['chat_id']}  [{r['agent']}]  {r['updated']}  {r['messages']} msgs\n  {r['title']}")
        if r.get("project"):
            print(f"  {r['project']}")
        snippet = r.get("snippet") or r.get("first_prompt") or ""
        if snippet:
            print("  " + " ".join(snippet.split())[:220])
        print()


def cmd_show(a) -> None:
    chat = store.read(store.connect(), a.chat_id, a.query, a.offset, a.limit, max_chars=4000)
    if not chat:
        sys.exit(f"no chat {a.chat_id!r}")
    print(f"{chat['title']}  [{chat['agent']}]  {chat['project']}  ({chat['showing']})\n")
    for m in chat["transcript"]:
        print(f"── {m['seq']} {m['role']} {m['time'] or ''}\n{m['text']}\n")


def cmd_stats(a) -> None:
    print(json.dumps(store.stats(store.connect()), indent=2))


# --- memory -------------------------------------------------------------------


def cmd_dream(a) -> None:
    from .llm import FatalError
    from .memory import dream

    opts = dream.Options(
        model=a.model,
        max_chats=a.max_chats,
        max_calls=a.max_calls,
        max_minutes=a.max_minutes,
        read=not a.consolidate_only,
        fold=not a.extract_only,
    )
    try:
        result = dream.run(opts, log=lambda m: print(time.strftime("%H:%M:%S"), m, file=sys.stderr, flush=True))
    except FatalError as e:
        sys.exit(f"recall dream: {e}")
    print(json.dumps(result, indent=2))


def cmd_memory(a) -> None:
    from .memory import context
    from .memory.notes import NoteStore

    con = store.connect()
    notes = NoteStore().load()
    context.index_notes(con, notes)
    if a.action == "search":
        for r in context.search(con, " ".join(a.terms), a.project):
            where = f"/{r['project']}" if r["project"] else ""
            print(f"{r['note_id']}  [{r['type']}/{r['scope']}{where}]")
            print(f"  {r['title']}: {' '.join(r['body'].split())[:200]}\n")
    elif a.action == "show":
        note = context.read_note(a.terms[0] if a.terms else "")
        print(json.dumps(note, indent=2) if note else "no such note")
    else:
        print(f"{len(notes)} notes in {paths.MEMORY} (start at MEMORY.md)")
        rows = con.execute("select scope, coalesce(project_name, '') p, type, count(*) c from notes group by 1, 2, 3")
        for r in rows:
            print(f"  {r['scope']:8} {r['p'][:24]:24} {r['type']:10} {r['c']}")


def cmd_context(a) -> None:
    try:  # a session-start hook must never break the session, whatever goes wrong
        from .memory.context import session_context

        cwd = json.load(sys.stdin).get("cwd") if a.hook else a.cwd
        print(session_context(cwd or os.getcwd()), end="")
    except Exception as e:
        print(f"recall context: {e}", file=sys.stderr)


# --- setup and integrations ---------------------------------------------------


def cmd_setup(a) -> None:
    from . import onboarding

    onboarding.run(assume_yes=a.yes)


def cmd_model(a) -> None:
    from . import onboarding

    cfg = config.load()
    if a.show:
        print(json.dumps(cfg["model"], indent=2) if cfg["model"] else "no model configured: run `recall model`")
        return
    cfg["model"] = onboarding.choose_model(onboarding.Prompter(), current=cfg["model"])
    config.save(cfg)
    print(f"saved to {paths.CONFIG}")


def cmd_install(a) -> None:
    from .integrations import agents

    parts = tuple(a.only.split(",")) if a.only else agents.COMPONENTS
    dry_run = getattr(a, "dry_run", False)
    lines = agents.install(parts, dry_run) if a.cmd == "install" else agents.uninstall(parts)
    print("\n".join(("[dry run] " if dry_run else "") + line for line in lines))
    if a.cmd == "install" and not dry_run:
        print("Restart open Codex and Cursor windows to pick up the changes.")


def cmd_schedule(a) -> None:
    from .integrations import scheduler

    if a.action == "enable":
        dream = config.load()["dream"]
        hour, minute = (int(x) for x in a.at.split(":"))
        print(scheduler.enable(hour, minute, a.max_minutes or dream["max_minutes"], a.max_calls or dream["max_calls"]))
    else:
        print(scheduler.disable() if a.action == "disable" else scheduler.status())


def cmd_serve(a) -> None:
    from .server import run

    run()


def cmd_eval(a) -> None:
    from . import evals

    if a.kind == "memory":
        evals.run_memory(store.connect(), a.cases or os.path.join(paths.EVALS, "memory_gold.jsonl"), a.model, a.fresh)
    else:
        default = os.path.join(paths.EVALS, "search_cases.jsonl")
        evals.run_search(store.connect(), a.cases or (default if os.path.exists(default) else None), a.sample)


# --- arguments ----------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recall", description="One memory for all your coding agents.")
    p.add_argument("--version", action="version", version=f"recall {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def command(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(handler=handler)
        return sp

    sp = command("setup", cmd_setup, "first run: pick agents and a model, index, connect, schedule")
    sp.add_argument("--yes", action="store_true", help="accept every default")
    sp = command("model", cmd_model, "choose which model writes memories")
    sp.add_argument("--show", action="store_true", help="print the current choice")

    sp = command("index", cmd_index, "index new and changed chats")
    sp.add_argument("--force", action="store_true", help="re-read every chat")
    sp.add_argument("--source", action="append", help="claude, codex, cursor or cursor-legacy")
    for name, help_text in (("search", "find chats by topic"), ("recent", "most recently active chats")):
        sp = command(name, cmd_search, help_text)
        if name == "search":
            sp.add_argument("query", nargs="+")
        sp.add_argument("--agent", choices=["claude", "codex", "cursor"])
        sp.add_argument("--project")
        sp.add_argument("--since", help="e.g. 7d, 2w, 3m or 2026-09-01")
        sp.add_argument("--limit", type=int, default=8)
        sp.add_argument("--json", action="store_true")
    sp = command("show", cmd_show, "print a chat's transcript")
    sp.add_argument("chat_id")
    sp.add_argument("--query")
    sp.add_argument("--offset", type=int)
    sp.add_argument("--limit", type=int, default=30)
    command("stats", cmd_stats, "what's in the index")

    sp = command("dream", cmd_dream, "distil new chats into memory notes (runs nightly)")
    sp.add_argument("--model", help="one-off override, e.g. claude:haiku or local:qwen3:4b")
    sp.add_argument("--max-chats", type=int, help="read at most N chats")
    sp.add_argument("--max-calls", type=int, help="stop after N model calls")
    sp.add_argument("--max-minutes", type=int, help="time budget; unread chats carry over")
    sp.add_argument("--extract-only", action="store_true", help="read chats but do not update notes")
    sp.add_argument("--consolidate-only", action="store_true", help="update notes without reading chats")
    sp = command("memory", cmd_memory, "browse memory notes")
    sp.add_argument("action", nargs="?", choices=["stats", "search", "show"], default="stats")
    sp.add_argument("terms", nargs="*")
    sp.add_argument("--project")
    sp = command("context", cmd_context, "print the memory block agents load at session start")
    sp.add_argument("--cwd", help="project folder (default: the current directory)")
    sp.add_argument("--hook", action="store_true", help="read the folder from a hook's JSON input")

    for name, help_text in (("install", "connect recall to your agents"), ("uninstall", "disconnect them")):
        sp = command(name, cmd_install, help_text)
        sp.add_argument("--only", help="comma-separated subset of: mcp,skill,hook")
        if name == "install":
            sp.add_argument("--dry-run", action="store_true")
    sp = command("schedule", cmd_schedule, "run the memory update nightly")
    sp.add_argument("action", choices=["enable", "disable", "status"])
    sp.add_argument("--at", default="03:30", help="HH:MM, local time")
    sp.add_argument("--max-minutes", type=int)
    sp.add_argument("--max-calls", type=int, help="cap on model calls per night")
    command("serve", cmd_serve, "run the MCP server over stdio")

    sp = command("eval", cmd_eval, "measure search or memory quality")
    sp.add_argument("kind", nargs="?", choices=["search", "memory"], default="search")
    sp.add_argument("--cases", help="JSONL of hand-written cases")
    sp.add_argument("--sample", type=int, default=150)
    sp.add_argument("--model", help="memory evals: e.g. chatgpt:gpt-6-luna, claude:haiku, local:qwen3:4b")
    sp.add_argument("--fresh", action="store_true", help="memory evals: skip cached answers, to measure variance")
    return p


if __name__ == "__main__":
    main()

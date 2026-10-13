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
        if "restored" in r:
            print(f"{name:14} {r['restored']:5} restored (no source has them any more)")
        else:
            print(f"{name:14} {r['indexed']:5} indexed  {r['unchanged']:5} unchanged")
    print(f"done in {time.time() - started:.1f}s → {paths.DB}")


def cmd_embed(a) -> None:
    from . import embeddings

    embedder = embeddings.configured()
    if embedder is None:
        sys.exit(
            "embeddings are off: add an [embeddings] table to config.toml and install the extra "
            "(uv tool install 'recall-agents[embeddings]'); see embeddings.py"
        )
    con = store.connect()
    todo = embeddings.pending(con, embedder)
    print(f"{len(todo)} messages to embed with {embedder.model}", file=sys.stderr)
    started = time.time()
    done = embeddings.index_messages(con, embedder, todo, log=lambda m: print(m, file=sys.stderr))
    print(f"embedded {done} messages in {time.time() - started:.0f}s")


def cmd_search(a) -> None:
    from . import usage

    con = store.connect()
    indexer.index(con, max_age=60)
    args = {"query": " ".join(a.query) if a.cmd == "search" else None, "agent": a.agent, "project": a.project}
    if a.cmd == "search":
        rows = store.search(con, " ".join(a.query), a.agent, a.project, a.since, a.limit, a.until)
    else:
        rows = store.recent(con, a.agent, a.project, a.since, a.limit, a.until)
    usage.record("cli", a.cmd, args, rows)
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
    from . import usage

    chat = store.read(store.connect(), a.chat_id, a.query, a.offset, a.limit, max_chars=4000)
    usage.record("cli", "show", {"chat_id": a.chat_id, "query": a.query}, chat or {"error": "not found"})
    if not chat:
        sys.exit(f"no chat {a.chat_id!r}")
    print(f"{chat['title']}  [{chat['agent']}]  {chat['project']}  ({chat['showing']})\n")
    if chat.get("archived"):
        print(f"({chat['archived']})\n")
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
        from . import usage

        found = context.search(con, " ".join(a.terms), a.project)
        usage.record("cli", "memory search", {"query": " ".join(a.terms), "project": a.project}, found)
        for r in found:
            where = f"/{r['project']}" if r["project"] else ""
            print(f"{r['note_id']}  [{r['type']}/{r['scope']}{where}]")
            print(f"  {r['title']}: {' '.join(r['body'].split())[:200]}\n")
    elif a.action == "show":
        from . import usage

        note = context.read_note(a.terms[0] if a.terms else "")
        usage.record("cli", "memory show", {"note_id": a.terms[0] if a.terms else None}, note)
        print(json.dumps(note, indent=2) if note else "no such note")
    elif a.action == "link":
        from .memory import links
        from .memory.notes import writing

        with writing(timeout=10) as free:
            if not free:
                sys.exit("a memory run is updating notes right now; try again in a minute")
            store_ = NoteStore()
            notes = store_.load()
            changed = links.relink(store_, notes)
            if changed:
                store_.compile(notes)
                store_.commit(f"relink: {changed} notes")
        print(f"{changed} notes relinked")
    else:
        print(f"{len(notes)} notes in {paths.MEMORY} (start at MEMORY.md)")
        rows = con.execute("select scope, coalesce(project_name, '') p, type, count(*) c from notes group by 1, 2, 3")
        for r in rows:
            print(f"  {r['scope']:8} {r['p'][:24]:24} {r['type']:10} {r['c']}")


def cmd_context(a) -> None:
    if a.hook:  # the old session-start hook command; `recall hook` answers it now
        return cmd_hook(a)
    try:
        from .memory.context import session_context

        print(session_context(a.cwd or os.getcwd(), agent=a.agent), end="")
    except Exception as e:
        print(f"recall context: {e}", file=sys.stderr)


def cmd_hook(a) -> None:
    """Every hook of every agent: read its JSON, answer through hooks.py. Never breaks the
    session, whatever goes wrong: an agent that blocks an edit on a bad reply (Cursor) still
    gets its "go ahead"."""
    from .integrations import hooks

    raw = {}
    try:
        raw = json.load(sys.stdin)
        # The older entries' flags still apply: a folder for an event that names none, a window.
        if getattr(a, "cwd", None) and not (raw.get("cwd") or raw.get("workspace_roots")):
            raw["cwd"] = a.cwd
        if getattr(a, "since", None):
            raw["recall_since"] = a.since
        out, code = hooks.dispatch(a.agent, raw)
    except Exception as e:
        print(f"recall hook: {e}", file=sys.stderr)
        out, code = hooks.silent(a.agent, raw), 0
    sys.stdout.write(out)
    sys.exit(code)


def cmd_explore(a) -> None:
    from . import explore

    con = explore.connect()
    if a.action == "backfill":
        started = time.time()
        r = explore.backfill(con, store.connect(), log=lambda m: print(m, file=sys.stderr))
        print(f"{r['stored']} past explorations stored ({r['skipped']} skipped) in {time.time() - started:.0f}s")
        return
    folder = a.cwd or os.getcwd()
    if a.action == "lookup":
        text, info = explore.brief(folder, " ".join(a.terms), con)
        if text:
            print(text)
            if not explore.serving():
                print(
                    "(not handed to new explorations: serving is off, set serve = true under [explore])",
                    file=sys.stderr,
                )
        elif info.get("candidates"):
            print(
                f"no match: the best of {info['candidates']} earlier explorations scored {info['top']}, "
                f"under the {explore.THRESHOLD} gate"
            )
        else:
            print(f"no match: {info.get('reason')}")
        return
    here = explore.checkout(folder)
    if not here:
        sys.exit(f"{folder} is not in a git repo")
    rows = con.execute(
        "select created_at, agent, agent_type, commit_sha, commit_source, prompt from explorations"
        " where repo = ? order by created_at desc limit ?",
        (here.repo, a.limit),
    )
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
        sha = (r["commit_sha"] or "?")[:9] + ("~" if r["commit_source"] != "head" else "")
        print(f"{when}  {r['agent']:6} {sha:10}  {' '.join(r['prompt'].split())[:100]}")


# --- projects -------------------------------------------------------------------


def cmd_projects(a) -> None:
    from . import projects

    con = store.connect()
    indexer.index(con, max_age=60)
    if a.action == "suggest":
        drafts = projects.suggest(con)
        if not drafts:
            print(f"Every folder in your chat history is covered by {projects.file()}.")
            return
        if a.write:
            path = projects.append(drafts)
            from .memory.notes import NoteStore

            NoteStore().commit(f"projects: add {len(drafts)} suggested")
            print(f"Added {len(drafts)} projects to {path}. Edit it: rename, merge, set kind and status.")
        else:
            print(projects.render(drafts))
            print(f"# {len(drafts)} projects. Add them to {projects.file()} with --write, then edit freely.")
        return
    if a.action == "show":
        found = projects.find(" ".join(a.terms))
        if not found:
            sys.exit(f"no project {' '.join(a.terms)!r} in {projects.file()}")
        d = projects.detail(con, found)
        if a.json:
            print(json.dumps(d, indent=2))
            return
        print(
            f"{d['name']}  ({d['kind'] or 'kind not set'}, {d['status']}){'  from ' + d['from'] if d['from'] else ''}"
        )
        print(f"{d['chats']} chats: " + ", ".join(f"{n} {agent}" for agent, n in d["by_agent"].items()))
        if d["placed_by_words"]:
            print(f"{d['placed_by_words']} of them placed by words: {', '.join(d['words'])}")
        print("\nWhere they ran:")
        for key, n in d["by_folder"].items():
            print(f"  {n:5}  {key}")
        print("\nLatest:")
        for r in d["recent"]:
            print(f"  {r['updated']}  [{r['agent']}]  {r['title']}")
        return
    rows = projects.overview(con, a.months, a.all)
    if a.json:
        print(json.dumps(rows, indent=2))
    elif not projects.load():
        print(f"No projects yet. `recall projects suggest` drafts {projects.file()} from your chat history.")
    else:
        print(projects.render_lanes(rows))


def cmd_rules(a) -> None:
    from . import projects, rules

    con = store.connect()
    target = " ".join(a.target) if a.target else None
    found = projects.find(target) if target else None
    if found:
        roots = projects.roots(con, found)
        name = found.name
    else:
        folder = os.path.abspath(os.path.expanduser(target)) if target else os.getcwd()
        if target and not os.path.isdir(folder):
            sys.exit(f"{target!r} is neither a project in {projects.file()} nor a folder")
        roots = [r] if (r := rules.repo_root(folder)) else []
        name = None
    if not roots:
        sys.exit("no repo on disk for that: recall checks the repos a project's chats ran in")
    for i, root in enumerate(roots):
        repo = rules.scan(root)
        if i:
            print("\n" + "─" * 60 + "\n")
        print(rules.report(repo, name))
        changes = rules.plan(repo)
        if not changes:
            continue
        print()
        if not a.write:
            print(rules.diff(changes), end="")
            print(f"\nRun again with --write to make {'this change' if len(changes) == 1 else 'these changes'}.")
            continue
        written = rules.write(root, changes)
        print(
            f"Wrote {', '.join(written)} in {root}. Review with `git -C {root} diff` and commit it if it looks right."
        )


def cmd_usage(a) -> None:
    from . import usage

    s = usage.summary(store.parse_since(a.since))
    if a.json:
        print(json.dumps(s, indent=2))
        return
    if not s["calls"]:
        print(f"Nothing logged yet{' in that window' if a.since else ''} ({usage.LOG}).")
        return
    print(f"{s['calls']} calls since {s['since'][:16].replace('T', ' ')}\n")
    kinds = ["search", "read", "recent", "load", "other"]
    print("".ljust(10) + "".join(k.rjust(8) for k in kinds))
    for who, counts in s["by_agent"].items():
        print(who.ljust(10) + "".join(str(counts.get(k, 0) or "·").rjust(8) for k in kinds))
    q = s["searches"]
    if q["total"]:
        print(f"\nSearches: {q['total']}, {q['empty']} found nothing, {q['opened']} led to opening a result")
    loads = s["loads"]
    if loads["total"]:
        print(f"Session starts: {loads['total']}, {loads['with_project']} in a known project, {loads['empty']} empty")
    if s["projects"]:
        print("Projects: " + ", ".join(f"{p} ({n})" for p, n in s["projects"].items()))
    print("\nLatest:")
    for e in s["latest"]:
        what = (e.get("args") or {}).get("query") or (e.get("args") or {}).get("chat_id") or e.get("project") or ""
        result = f"→ {e['n']}" if "n" in e else f"{e['chars']} chars" if "chars" in e else e.get("error", "")
        print(
            f"  {e['at'][5:16].replace('T', ' ')}  {e.get('client') or e['via']:8} {e['tool']:15} {what[:40]}  {result}"
        )


def cmd_status(a) -> None:
    from . import status

    if a.hook:  # the edit hook's first command; `recall hook` answers it now
        return cmd_hook(a)
    s = status.status(store.parse_since(a.since))
    if a.json:
        print(json.dumps(s, indent=2))
        return
    names = {x["id"]: f"{status.AGENTS.get(x['agent'], x['agent'])} · {x['title'][:40]}" for x in s["sessions"]}
    for o in s["overlaps"]:
        verdict = {True: "will conflict", False: "merges clean", None: "same working copy"}[o["conflict"]]
        print(f"{o['file']} {verdict}  ({o['repo']} · {' ↔ '.join(o['branches'])})")
        for sid in o["sessions"]:
            print(f"    {names[sid]}")
    if not s["overlaps"]:
        print("Nothing needs you.")
    live = [x for x in s["sessions"] if x["live"]]
    print(f"\n{len(live)} live, {len(s['sessions'])} since {a.since}")
    repos: dict[str, list] = {}
    for x in live:
        repos.setdefault(x["repo"] or "no folder", []).append(x)
    for repo, xs in repos.items():
        print(f"  {repo:14} {', '.join(dict.fromkeys(x['branch'] or '?' for x in xs))}")


def cmd_resolve(a) -> None:
    from . import mail

    out = mail.resolve(a.repo, a.file, store.parse_since(a.since))
    if a.json:
        print(json.dumps(out))
    elif out["sent"]:
        print(f"Sent a note to {out['sent']} chats; each reads it at its next edit, turn end or prompt.")
    else:
        print(f"Nothing sent: {out['reason']}.")


# --- setup and integrations ---------------------------------------------------


def cmd_setup(a) -> None:
    from . import onboarding

    onboarding.run(assume_yes=a.yes, model=a.model, embeddings=a.embeddings)


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

    if a.kind == "swe-contextbench":
        from . import evals_swecb

        evals_swecb.from_args(a)
        return
    if a.kind == "memorycode":
        from . import evals_memorycode

        evals_memorycode.from_args(a)
        return
    if a.kind == "explore":
        from . import evals_explore

        evals_explore.from_args(a)
        return
    if a.kind == "amb":
        from . import evals_amb

        evals_amb.from_args(a)
        return
    if a.kind == "agent" or a.v2:
        _eval_v2(a)
    elif a.kind in ("longmemeval", "locomo"):
        if not a.cases:
            sys.exit(f"recall eval {a.kind} needs --cases pointing at the dataset's JSON file")
        embedder = cache = None
        if a.hybrid:
            import sqlite3

            from . import embeddings

            embedder = embeddings.configured()
            if embedder is None:
                sys.exit("--hybrid needs an [embeddings] table in config.toml and the embeddings extra")
            cache = sqlite3.connect(os.path.join(paths.HOME, "eval-vectors.db"), timeout=60)
        if a.kind == "locomo":
            evals.run_locomo(a.cases, embedder=embedder, cache=cache)
        elif a.qa:
            evals.run_longmemeval_qa(
                a.cases,
                a.qa,
                a.qa_sample,
                judge=a.judge,
                context=a.context,
                top_k=a.top_k,
                embedder=embedder,
                cache=cache,
                out_path=a.out,
            )
        else:
            evals.run_longmemeval(a.cases, a.limit, a.user_only, embedder=embedder, cache=cache, official=a.official)
    elif a.kind == "session":
        cases = a.cases or os.path.join(paths.EVALS, "session_cases.json")
        if not os.path.exists(cases):
            sys.exit(f"no session-start cases at {cases}; evals.py describes the format")
        evals.run_session(cases, None if a.split == "all" else a.split)
    elif a.kind == "memory":
        evals.run_memory(store.connect(), a.cases or os.path.join(paths.EVALS, "memory_gold.jsonl"), a.model, a.fresh)
    else:
        default = os.path.join(paths.EVALS, "search_cases.jsonl")
        paraphrased = os.path.join(paths.EVALS, "paraphrase_cases.jsonl")
        evals.run_search(
            store.connect(),
            a.cases or (default if os.path.exists(default) else None),
            a.sample,
            paraphrased if os.path.exists(paraphrased) else None,
        )


def _eval_v2(a) -> None:
    """The v2 evals: search_v2 (find rate), qa_v2 (an agent answering with recall's tools) and
    memory_v2 (note quality), each writing a run folder; see evals_harness.py."""
    from . import evals_harness as harness

    kinds = {"search": "search_v2", "agent": "qa_v2", "memory": "memory_v2"}
    if a.kind not in kinds:
        sys.exit(f"--v2 applies to search and memory, not {a.kind}")
    cases = harness.load_cases(kinds[a.kind], a.split, a.cases, a.case, a.limit)
    settings = {"cases": len(cases), "note": a.note or ""}
    if a.kind == "agent":
        from . import evals_agent

        model = a.model or evals_agent.DEFAULT_MODEL
        settings.update(repeats=a.repeats, workers=a.workers, timeout=a.timeout, skill=a.skill)
        run = harness.Run(kinds[a.kind], a.split, settings, a.who, a.runs_dir)
        evals_agent.run(
            cases,
            run,
            model,
            a.judge or harness.JUDGE,
            a.repeats,
            a.workers,
            a.timeout,
            a.consistency,
            a.skill,
            a.against,
        )
    elif a.kind == "memory":
        from . import evals_memory

        settings.update(repeats=a.repeats)
        con = harness.corpus()
        run = harness.Run(kinds[a.kind], a.split, settings, a.who, a.runs_dir)
        evals_memory.run(
            con, cases, run, a.model, a.judge or harness.JUDGE, a.fresh, a.repeats, a.consistency, a.against
        )
    else:
        from . import evals_search

        con = harness.corpus()
        settings["keyword_only"] = a.keyword
        run = harness.Run(kinds[a.kind], a.split, settings, a.who, a.runs_dir)
        evals_search.run(con, cases, run, a.against, keyword_only=a.keyword)


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
    sp.add_argument("--model", help="who writes memories, e.g. claude:haiku, chatgpt:gpt-6-luna, local:qwen3:4b")
    sp.add_argument("--embeddings", metavar="NAME", help="turn on semantic search with this Ollama model")
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
        sp.add_argument("--until", help="end of the window, e.g. 2026-09-30 or 7d (up to a week ago)")
        sp.add_argument("--limit", type=int, default=8)
        sp.add_argument("--json", action="store_true")
    command("embed", cmd_embed, "embed messages for semantic search (when [embeddings] is set)")
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
    sp.add_argument("action", nargs="?", choices=["stats", "search", "show", "link"], default="stats")
    sp.add_argument("terms", nargs="*")
    sp.add_argument("--project")
    sp = command("context", cmd_context, "print the memory block agents load at session start")
    sp.add_argument("--cwd", help="project folder (default: the current directory)")
    sp.add_argument("--hook", action="store_true", help="old hook entry; same as `recall hook`")
    sp.add_argument("--agent", choices=["claude", "codex", "cursor"], default="claude", help="who will read it")
    sp = command("hook", cmd_hook, "answer an agent's hook, reading its JSON input (recall install sets this up)")
    sp.add_argument("--agent", default="claude", help="claude, codex, cursor, gemini or opencode")

    sp = command(
        "explore", cmd_explore, "earlier exploration reports per repo, and what a new exploration would be handed"
    )
    sp.add_argument("action", nargs="?", choices=["list", "lookup", "backfill"], default="list")
    sp.add_argument("terms", nargs="*", help="lookup: the exploration prompt")
    sp.add_argument("--cwd", help="the repo folder (default: the current directory)")
    sp.add_argument("--limit", type=int, default=20)

    sp = command("projects", cmd_projects, "your projects over time, as git-style lanes")
    sp.add_argument("action", nargs="?", choices=["lanes", "suggest", "show"], default="lanes")
    sp.add_argument("terms", nargs="*", help="show: the project's name")
    sp.add_argument("--months", type=int, default=6, help="lanes: how far back")
    sp.add_argument("--all", action="store_true", help="lanes: include projects with no chats in that window")
    sp.add_argument("--write", action="store_true", help="suggest: add the drafts to projects.yaml")
    sp.add_argument("--json", action="store_true")
    sp = command("rules", cmd_rules, "what each agent is told about a repo, and closing the gaps")
    sp.add_argument("target", nargs="*", help="a project's name or a folder (default: the current folder)")
    sp.add_argument("--write", action="store_true", help="make the changes shown in the diff")
    sp = command("status", cmd_status, "what your agents are working on now, and files two of them both changed")
    sp.add_argument("--since", default="12h", help="how far back, e.g. 2h, 12h or 1d")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--hook", action="store_true", help="old hook entry; same as `recall hook`")
    sp.add_argument("--agent", default="claude", help="--hook: which agent (claude, codex, cursor, …)")
    sp = command("resolve", cmd_resolve, "message every chat in an overlap to agree who goes first")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--file", required=True, help="the file, relative to the repo, as recall status shows it")
    sp.add_argument("--since", default="12h")
    sp.add_argument("--json", action="store_true")
    sp = command("usage", cmd_usage, "how recall itself gets used, from its local log")
    sp.add_argument("--since", help="e.g. 7d, 2w or 2026-10-01")
    sp.add_argument("--json", action="store_true")

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

    sp = command("eval", cmd_eval, "measure search, answer, memory or session-start quality")
    kinds = [
        "search",
        "memory",
        "session",
        "agent",
        "longmemeval",
        "locomo",
        "swe-contextbench",
        "memorycode",
        "amb",
        "explore",
    ]
    sp.add_argument("kind", nargs="?", choices=kinds, default="search")
    sp.add_argument(
        "--cases",
        help="JSONL of hand-written cases; session: a JSON array of folders; longmemeval: the dataset's JSON file",
    )
    sp.add_argument("--limit", type=int, help="longmemeval: the first N questions; v2: a fixed sample of N cases")
    sp.add_argument("--user-only", action="store_true", help="longmemeval: index only user turns, as the paper does")
    sp.add_argument("--official", action="store_true", help="longmemeval: only the questions its own script scores")
    sp.add_argument("--hybrid", action="store_true", help="longmemeval, locomo: also rank by meaning ([embeddings])")
    sp.add_argument(
        "--qa", metavar="MODEL", help="longmemeval: answer and judge with MODEL, e.g. claude:claude-haiku-5-5"
    )
    sp.add_argument("--qa-sample", type=int, default=200, help="--qa: questions in the stratified sample")
    sp.add_argument(
        "--judge",
        metavar="MODEL",
        help="--qa, agent, memory --v2: grade with MODEL (v2 default claude:claude-sonnet-5-5)",
    )
    sp.add_argument(
        "--context", choices=["sessions", "windows", "oracle"], default="sessions", help="--qa: what it reads"
    )
    sp.add_argument("--top-k", type=int, default=10, help="--qa, sessions: how many chats it reads")
    sp.add_argument("--out", help="--qa: write each question's result here as JSONL")
    sp.add_argument("--sample", type=int, default=150)
    sp.add_argument(
        "--model",
        help="memory evals: e.g. chatgpt:gpt-6-luna, claude:haiku, local:qwen3:4b; agent: the Claude model under "
        "test (default claude-haiku-5-5)",
    )
    sp.add_argument("--fresh", action="store_true", help="memory evals: skip cached answers, to measure variance")
    v2 = sp.add_argument_group("v2 evals (search --v2, memory --v2, agent)")
    v2.add_argument("--v2", action="store_true", help="search, memory: the v2 cases and run folder")
    v2.add_argument(
        "--split", choices=["train", "test", "holdout", "all"], default="train", help="which cases (default train)"
    )
    v2.add_argument("--case", action="append", metavar="ID", help="only this case; repeatable")
    v2.add_argument("--against", metavar="RUN_DIR", help="paired comparison with an earlier run of the same eval")
    v2.add_argument("--keyword", action="store_true", help="search --v2: keywords only, even when embeddings are on")
    v2.add_argument("--repeats", type=int, default=1, help="agent, memory: run each case N times")
    v2.add_argument("--workers", type=int, default=4, help="agent: cases run at once")
    v2.add_argument("--timeout", type=int, default=300, help="agent: seconds before a case counts as timed out")
    v2.add_argument("--consistency", type=int, default=8, help="agent, memory: re-judge N cases to check the grader")
    v2.add_argument("--skill", action="store_true", help="agent: also give the agent recall's skill text")
    v2.add_argument("--who", help="name in the run folder and ledger (default $RECALL_EVAL_WHO or harness)")
    v2.add_argument("--note", help="a note for the ledger line")
    v2.add_argument("--runs-dir", help="where run folders go; the ledger is LEDGER.md beside it")
    scb = sp.add_argument_group("swe-contextbench (also --hybrid, --split, --case, --limit, --against, --who, --note)")
    scb.add_argument("--data", help="the benchmark folder, holding repo/ (GitHub) and hf/ (Hugging Face)")
    scb.add_argument("--subset", choices=["lite", "full"], default="lite", help="related tasks (default lite)")
    scb.add_argument("--corpus", choices=["transcripts", "records"], default="transcripts", help="past chats to index")
    scb.add_argument("--query", choices=["problem", "title"], default="problem", help="the issue in full, or line 1")
    scb.add_argument("--scope", choices=["repo", "all"], default="repo", help="search the task's repo, or every repo")
    scb.add_argument(
        "--agent", metavar="MODEL", nargs="?", const="", help="Claude Code finds the chats (default claude-haiku-5-5)"
    )
    mc = sp.add_argument_group("memorycode, amb (also --data, --agent, --model, --hybrid, --split, --limit, --repeats)")
    mc.add_argument(
        "--arm", choices=["none", "tools", "notes", "hook", "full"], default="tools", help="memorycode --agent"
    )
    mc.add_argument("--notes", metavar="RUN_DIR", help="memorycode --arm notes: the extraction run whose notes to use")
    mc.add_argument("--queries", type=int, default=3, help="memorycode --agent: requests per history (0: all)")
    return p


if __name__ == "__main__":
    main()

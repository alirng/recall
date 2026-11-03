"""Command-line interface: `recall <command>`. Run `recall --help` for the list."""

import argparse
import json
import os
import sys
import time

from . import __version__, config, paths


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:2] == ["eval", "compare"]:  # its own arguments: run folders and "vs"
        from . import evals_compare

        evals_compare.main(argv[2:])
        return
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except KeyboardInterrupt:
        sys.exit(130)


# --- chat history -------------------------------------------------------------


def cmd_index(a) -> None:
    from .chats import indexer, store

    started = time.time()
    if a.background:  # started by a tool call's refresh (indexer.py): low priority, and killed at the deadline
        import signal

        from . import power

        power.lower_priority()
        signal.alarm(indexer.CHILD_DEADLINE)  # work committed so far stays; the next run carries on from it
    result = indexer.index(store.connect(), only=a.source, force=a.force, log=lambda m: print(m, file=sys.stderr))
    for name, r in result.items():
        if "restored" in r:
            print(f"{name:14} {r['restored']:5} restored (no source has them any more)")
        else:
            print(f"{name:14} {r['indexed']:5} indexed  {r['unchanged']:5} unchanged")
    print(f"done in {time.time() - started:.1f}s → {paths.DB}")
    if not a.source or "codex" in a.source:
        from .chats.sources import codex

        if note := codex.unreadable_note():
            print(f"recall: {note}", file=sys.stderr)


def cmd_embed(a) -> None:
    from . import embeddings, power
    from .chats import store

    power.lower_priority()  # a backfill, mostly started by a tool call: everything else gets the CPU first
    embedder = embeddings.configured()
    if embedder is None:
        sys.exit(
            "embeddings are off: add an [embeddings] table to config.toml and install the extra "
            "(uv tool install 'recall-agents[embeddings]'); see embeddings.py"
        )
    with embeddings.embedding_lock() as mine:
        if not mine:
            print("another recall embed is running", file=sys.stderr)
            return
        con = store.connect()
        todo = embeddings.pending(con, embedder)
        print(f"{len(todo)} messages to embed with {embedder.model}", file=sys.stderr)
        started = time.time()
        done = embeddings.index_messages(
            con,
            embedder,
            todo,
            log=lambda m: print(m, file=sys.stderr),
            deadline=started + embeddings.EMBED_DEADLINE,
        )
    left = f"; stopped at the time limit with {len(todo) - done} left for the next run" if done < len(todo) else ""
    print(f"embedded {done} messages in {time.time() - started:.0f}s{left}")


def cmd_search(a) -> None:
    from . import usage
    from .chats import indexer, store

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
    from .chats import store

    chat = store.read(store.connect(), a.chat_id, a.query, a.offset, a.limit, max_chars=4000)
    usage.record("cli", "show", {"chat_id": a.chat_id, "query": a.query}, chat or {"error": "not found"})
    if not chat:
        sys.exit(f"no chat {a.chat_id!r}")
    print(f"{chat['title']}  [{chat['agent']}]  {chat['project']}  ({chat['showing']})\n")
    if chat.get("archived"):
        print(f"({chat['archived']})\n")
    for m in chat["transcript"]:
        print(f"── {m['seq']} {m['role']} {m['time'] or ''}\n{m['text']}\n")


def cmd_forget(a) -> None:
    from . import forget
    from .chats import indexer, store

    if not (a.chat_id or a.project):
        sys.exit("name a chat id, or --project NAME")
    con = store.connect()
    try:
        ids = forget.resolve(con, a.chat_id, a.project)
    except forget.Ambiguous as e:
        sys.exit(str(e))
    if not ids:
        sys.exit("no such chat in recall's index or archive")
    chats = forget.describe(con, ids)
    for c in chats:
        print(f"{c['chat_id']}  {c['title'] or ''}  ({c['messages']} messages, {c['archive_copies']} archive copies)")
    if not a.yes:
        if not sys.stdin.isatty():
            sys.exit("pass --yes to forget without asking")
        if (
            not input(f"Forget {len(ids)} chat(s) from recall's index and archive? (y/n) ")
            .strip()
            .lower()
            .startswith("y")
        ):
            return
    with indexer.exclusive("an index run is in progress: waiting for it to finish"):
        counts = forget.purge(con, ids, scrub=False)
        clean = forget.scrub_db(con)
    print("forgot: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items()))
    if not clean:
        print(
            "another recall process has the index open (an agent's MCP server, likely), so the index's\n"
            "-wal file still holds the old pages. SQLite clears it when that process closes the index:\n"
            "quit the agent."
        )
    files = sorted({f for c in chats for f in c["files"]})
    if files:
        print(f"recall will not read them again; the agents' own files are untouched ({len(files)}):")
        print("\n".join(f"  {f}" for f in files))
    notes = forget.notes_citing(ids)
    if notes:
        print(f"\n{len(notes)} memory note(s) cite these chats; recall leaves notes to you:")
        print("\n".join(f"  {n}" for n in notes))
        print(
            "Edit or delete them. Or run this (needs git-filter-repo): it commits the notes folder as it is, then"
            " deletes these notes from disk and from every commit of its history, with the deletion ledger's past"
            " versions:"
        )
        print(f"  {forget.history_command(notes)}")
        print("filter-repo removes the notes folder's git remote: if you sync it, add the remote back and force-push.")


def cmd_stats(a) -> None:
    from .chats import indexer, store
    from .chats.sources import codex, compressed

    out = store.stats(store.connect())
    if n := codex.unreadable():  # zstd-compressed rollouts nothing here can decode: not in the numbers above
        out.setdefault("codex", {})["unreadable"] = {"chats": n, "fix": compressed.fix_hint()}
    if parked := indexer.parked():  # failed every read: read again when their files change
        out["not_indexed"] = {"chats": len(parked), "retry": "recall index --force"}
    print(json.dumps(out, indent=2))


def cmd_why(a) -> None:
    from . import commits, usage
    from .chats import indexer, store

    if not a.target and not a.commit:
        sys.exit("recall why needs a file (path, path:line or path:start-end) or --commit <sha>")
    con = store.connect()
    indexer.index(con, max_age=60)
    started = time.perf_counter()
    result = commits.why(con, a.target, a.commit)
    found = [c for x in result.get("commits") or [] for c in x.get("chats") or []]
    usage.record("cli", "why", {"target": a.target, "commit": a.commit}, found, ms=usage.ms_since(started))
    print(json.dumps(result, indent=2) if a.json else commits.render(result))


def cmd_links(a) -> None:
    from . import commits
    from .chats import indexer, store

    con = store.connect()
    indexer.index(con, max_age=60)
    if not commits.enabled():
        commits.clear(con)
        sys.exit("commit links are off ([links] enabled = false in config.toml); any stored links were removed")
    result = commits.update(con, log=lambda m: print(m, file=sys.stderr))
    if result.get("busy"):
        print("another recall process is updating commit links; showing them as they stand", file=sys.stderr)
    if a.episodes:
        found = commits.episodes(con)
        if a.json:
            print(json.dumps(found, indent=2))
            return
        for e in found:
            f = e["failure"]
            where = f"in {f['session_id']}" if f["session_id"] else f"(commit {f['detail'][:8]})"
            print(f"{e['fix_commit'][:8]}  {e['repo']}  fixes a {f['kind']} {where}")
            print(f"  prompt: {(e['prompt'] or '')[:160]}")
        failures = sum(e["failure_count"] for e in found)
        print(f"{len(found)} fixing commits, after {failures} failures (each counted once)")
        return
    out = {**commits.stats(con), "seconds": result.get("seconds")}
    print(json.dumps(out, indent=2) if a.json else "\n".join(f"{k:18} {v}" for k, v in out.items()))


# --- memory -------------------------------------------------------------------


def cmd_dream(a) -> None:
    from . import autopilot, power
    from .llm import FatalError
    from .memory import dream

    # First, before anything opens the index: a run launchd starts on waking often finds the machine on battery.
    # A person at a terminal asked for this run: it goes ahead.
    waits = power.defer(a.on_battery or ("run" if sys.stdin.isatty() else None))
    power.note("dream", waits)
    if waits:
        print(
            time.strftime("%H:%M:%S"),
            "deferred: on battery. The first tool call or session start on power runs it;"
            ' `recall dream --on-battery run` runs it now, and [power] on_battery = "run" always.',
            file=sys.stderr,
        )
        return
    if a.background:  # a catch-up started by a tool call or a session start (power.catch_up)
        power.lower_priority()
    autopilot.run("nightly", log=lambda m: print(time.strftime("%H:%M:%S"), "autopilot:", m, file=sys.stderr))

    opts = dream.Options(
        model=a.model,
        max_chats=a.max_chats,
        max_calls=a.max_calls,
        max_minutes=a.max_minutes,
        max_usd=a.max_usd if a.max_usd is not None else config.load()["dream"].get("max_usd"),
        batch=not a.no_batch,
        read=not a.consolidate_only,
        fold=not a.extract_only,
    )
    try:
        result = dream.run(opts, log=lambda m: print(time.strftime("%H:%M:%S"), m, file=sys.stderr, flush=True))
    except FatalError as e:
        sys.exit(f"recall dream: {e}")
    print(json.dumps(result, indent=2))


def cmd_memory(a) -> None:
    from .chats import store
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
    from .chats import store

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
    from .chats import indexer, store

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
    from .chats import store

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
    from .chats import store

    s = usage.summary(store.parse_since(a.since))
    if a.json:
        print(json.dumps(s, indent=2))
        return
    if not s["calls"]:
        print(f"Nothing logged yet{' in that window' if a.since else ''} ({usage.LOG}).")
        return
    print(f"{s['calls']} calls since {s['since'][:16].replace('T', ' ')}\n")
    kinds = ["search", "read", "recent", "load", "jit", "other"]
    print("".ljust(10) + "".join(k.rjust(8) for k in kinds))
    for who, counts in s["by_agent"].items():
        print(who.ljust(10) + "".join(str(counts.get(k, 0) or "·").rjust(8) for k in kinds))
    q = s["searches"]
    if q["total"]:
        print(f"\nSearches: {q['total']}, {q['empty']} found nothing, {q['opened']} led to opening a result")
    loads = s["loads"]
    if loads["total"]:
        print(f"Session starts: {loads['total']}, {loads['with_project']} in a known project, {loads['empty']} empty")
    if j := s.get("jit"):
        events = ", ".join(f"{n} {k}" for k, n in j["by_event"].items())
        print(
            f"Mid-session memory (jit): {j['fires']} injections ({events or 'none'}) in {j['sessions']} sessions; "
            f"{j['prompts_fired']} of {j['prompts']} prompts fired; "
            f"{j['chars_per_session'].get('median', 0)} chars per session (median), "
            f"{j['opened_after']} injected items opened afterwards"
        )
    if s["projects"]:
        print("Projects: " + ", ".join(f"{p} ({n})" for p, n in s["projects"].items()))
    print("\nLatest:")
    for e in s["latest"]:
        what = (e.get("args") or {}).get("query") or (e.get("args") or {}).get("chat_id") or e.get("project") or ""
        result = f"→ {e['n']}" if "n" in e else f"{e['chars']} chars" if "chars" in e else e.get("error", "")
        print(
            f"  {e['at'][5:16].replace('T', ' ')}  {e.get('client') or e['via']:8} {e['tool']:15} {what[:40]}  {result}"
        )


def cmd_jit(a) -> None:
    """What jit would hand an agent for a prompt, a file it reads, or a failed command, and
    why. The hooks themselves answer through `recall hook` (integrations/hooks.py)."""
    from . import jit
    from .chats import store

    con = store.connect()
    cwd = os.path.abspath(a.cwd or os.getcwd())
    if a.file:
        items = jit.file_items(con, a.agent, os.path.abspath(a.file), cwd, [])
    elif a.error:
        sig = jit.error_signature(a.error)
        print(f"signature: {sig['key'] if sig else '(too generic: stays silent)'}")
        items = jit.failure_items(con, sig, cwd, []) if sig else []
    else:
        gates = jit.load_gates()
        items = jit.match_prompt(con, " ".join(a.prompt), cwd, keep_all=True)
        if not items:
            print("Nothing to match: the prompt is too short, a command, or matches no chat or note.")
            return
        where = "fitted here" if os.path.exists(jit.GATE_FILE) else "shipped"
        print(
            f"gates ({where}): dense ≥ {gates['dense']['threshold']:.3f}, keyword ≥ {gates['keyword']['threshold']:.3f}"
        )
        for i in items:
            gate = "dense" if i.features.get("has_vec") else "keyword"
            print(f"  {'fires' if i.passed else '     '}  p={i.p:.3f} ({gate})  {i.kind:4}  {i.id}")
            if a.json:
                print("         " + json.dumps({k: round(v, 3) for k, v in i.features.items()}))
        items = [i for i in items if i.passed]
    block = jit.render(items[: jit.MAX_ITEMS])
    print("\n" + (block if block else "(nothing would be injected)"))


def cmd_status(a) -> None:
    from . import status
    from .chats import store

    if a.hook:  # the edit hook's first command; `recall hook` answers it now
        return cmd_hook(a)
    from . import autopilot, power
    from .chats.sources import codex, compressed

    s = status.status(store.parse_since(a.since))
    if n := codex.unreadable():
        s["unreadable"] = {"agent": "codex", "chats": n, "fix": compressed.fix_hint()}
    s["outlived"] = status.outlived()
    s["power"] = {"on_battery": power.on_battery(), "deferred": power.deferred()}
    rows = autopilot.report(deep=False)
    if a.json:
        print(json.dumps({**s, "roots": rows}, indent=2))
        return
    names = {x["id"]: f"{status.AGENTS.get(x['agent'], x['agent'])} · {x['title'][:40]}" for x in s["sessions"]}
    for o in s["overlaps"]:
        verdict = {True: "will conflict", False: "merges clean", None: "same working copy"}[o["conflict"]]
        print(f"{o['file']} {verdict}  ({o['repo']} · {' ↔ '.join(o['branches'])})")
        for sid in o["sessions"]:
            print(f"    {names[sid]}")
    by_agent: dict[str, list] = {}
    for x in s["outlived"]:
        by_agent.setdefault(x["agent"], []).append(x)
    for agent, xs in by_agent.items():
        print(f"Restart {agent}: its recall server started before recall {xs[-1]['installed']} was installed")
        for x in xs:
            chat = names.get(x["chat"], x["chat"]) if x["chat"] else None
            since = time.strftime("%b %d %H:%M", time.localtime(x["started_at"]))
            print(f"    pid {x['pid']} · since {since}" + (f" · {chat}" if chat else ""))
    if not s["overlaps"] and not s["outlived"]:
        print("Nothing needs you.")
    live = [x for x in s["sessions"] if x["live"]]
    print(f"\n{len(live)} live, {len(s['sessions'])} since {a.since}")
    repos: dict[str, list] = {}
    for x in live:
        repos.setdefault(x["repo"] or "no folder", []).append(x)
    for repo, xs in repos.items():
        print(f"  {repo:14} {', '.join(dict.fromkeys(x['branch'] or '?' for x in xs))}")
    if note := codex.unreadable_note():
        print(f"\n{note}")
    for line in power.summary():
        print(f"\n{line}")
    print("\n" + autopilot.summary_line(rows))


def cmd_resolve(a) -> None:
    from . import mail
    from .chats import store

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

    onboarding.run(assume_yes=a.yes, model=a.model, embeddings=a.embeddings, autopilot=a.autopilot)


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
    from . import autopilot, roots
    from .integrations import agents

    parts = tuple(a.only.split(",")) if a.only else agents.COMPONENTS
    dry_run = getattr(a, "dry_run", False)
    found = roots.discover()
    if a.cmd == "uninstall":
        found = found + autopilot.recorded_roots(found)  # a root that is no longer found may still hold recall
    chosen = None
    if a.root:
        names = {n.strip() for x in a.root for n in x.split(",") if n.strip()}
        chosen = [r for r in found if r.name in names]
        unknown = names - {r.name for r in chosen}
        if unknown:
            sys.exit(f"no such root: {', '.join(sorted(unknown))} (see `recall doctor` for their names)")
    if a.cmd == "install":
        for r in chosen or ():  # asking for a root by name is saying yes to it again
            if not roots.option(r, "connect") and not dry_run:
                roots.set_option(r, "connect", True)
        lines = agents.install(parts, dry_run, chosen or found, repoint=a.repoint)
        if not dry_run:
            autopilot.record_connected(found)  # so uninstall finds these roots even if discovery later loses them
            if "hook" in parts and autopilot.consented() is not None:
                autopilot.grant_consent(extend=True)  # the person installed these hooks themselves
        if not a.only or "retention" in parts:  # --only mcp,skill,hook never asks
            # --only retention is a yes; otherwise ask whoever is at the terminal
            ask = (lambda _: True) if "retention" in parts else None if dry_run else agents.ask_terminal
            lines += agents.retention(ask, dry_run)
    else:
        try:
            # off first, and the lock held: a run in progress ends, and nothing connects again meanwhile
            with autopilot.stopped(turn_off=chosen is None):
                for r in chosen or ():  # recall stays out of a root the person disconnected
                    roots.set_option(r, "connect", False)
                lines = agents.uninstall(parts, chosen or found, whole=chosen is None)
                if chosen is None and set(parts) == set(agents.COMPONENTS):
                    autopilot.forget()
                elif chosen:
                    autopilot.forget([r.name for r in chosen])
        except TimeoutError as e:
            sys.exit(str(e))
    print("\n".join(("[dry run] " if dry_run else "") + line for line in lines))
    if a.cmd == "install" and not dry_run:
        print("Restart open Codex and Cursor windows to pick up the changes.")
    if a.cmd == "uninstall" and chosen is None:
        print("Autopilot is off.")
    elif a.cmd == "uninstall":
        print(f"recall will not connect {', '.join(r.name for r in chosen)} again (`recall install --root` does).")


def cmd_autopilot(a) -> None:
    from . import autopilot

    if a.action in ("on", "off") and a.dry_run:
        print(f"[dry run] autopilot {a.action}: nothing changed")
        return
    if a.action in ("on", "off"):
        autopilot.set_enabled(a.action == "on")
        print(
            f"autopilot {a.action}: "
            + ("recall keeps every agent folder connected." if a.action == "on" else "nothing changes by itself.")
        )
        if a.action == "off":
            return
    elif not autopilot.enabled():
        if not a.quiet:
            print("autopilot is off: `recall autopilot on` lets recall connect new agent folders by itself")
        return
    changes = autopilot.run("manual", a.dry_run, log=None if a.quiet else print)
    if not changes and not a.quiet:
        print("everything is connected")


def cmd_doctor(a) -> None:
    from . import autopilot, power
    from .chats import indexer
    from .chats.sources import codex

    rows = autopilot.report()
    if a.json:
        print(json.dumps(rows, indent=2))
        return
    home = os.path.expanduser("~")
    for r in rows:
        where = r["path"].replace(home, "~") + (f"  [{r['data'].replace(home, '~')}]" if r["data"] else "")
        state = autopilot.state_of(r)
        if state == "waiting":
            text = "waiting for first launch (the agent has not used this folder yet; recall writes nothing there)"
        elif state == "skipped":
            text = f"skipped by you ([roots.{r['root']}] connect = false)"
        elif r["agent"] == "cursor" and not r["has_cursor_config"]:
            text = "no ~/.cursor folder yet (Cursor makes it when it first uses MCP or hooks)"
        else:
            have = [
                ("MCP", r["parts"].get("mcp")),
                ("hooks", r["parts"].get("hook")),
                ("skill", r["parts"].get("skill")),
            ]
            text = (
                "connected" if r["connected"] else "NOT connected: missing " + ", ".join(n for n, ok in have if not ok)
            )
            if state == "trust":
                text = "hooks written, trust pending: trust them once with /hooks in Codex"
            elif state == "approval":
                text += ", approval pending: Cursor asks once per project before it loads a new MCP server"
            elif r["approval"]:
                text += f", approved in {r['approval']['approved']} of {r['approval']['projects']} Cursor projects"
        seen = f"{r['chats']:,} chats, last indexed {_ago(r['indexed_at'])}" if "chats" in r else "not indexed yet"
        if not r["index"]:
            seen = f"not indexed ([roots.{r['root']}] index = false)"
        elif not r["mine"] and r["exists"]:
            seen += f"; not mined (mine = true in [roots.{r['root']}] turns its chats into memory)"
        print(f"{r['root']:15} {where}\n{'':15} {text}; {seen}" + (f"; {r['account']}" if r["account"] else ""))
        if r["hooks_run"]:
            print(f"{'':15} hooks run {', '.join(r['hooks_run'])}, not this recall ({r['this_recall']})")
        if r["message_chat_unasked"]:
            print(f"{'':15} Codex runs message_chat without asking you: `recall install` makes it ask")
    on = autopilot.enabled()
    print(
        f"\nautopilot: {'on' if on else 'off'}"
        + ("" if on else " (`recall autopilot on` connects new folders by itself)")
    )
    if on and autopilot.consented() is None:
        print(
            "  no yes is recorded for hooks, so autopilot repairs the MCP server and skill only: `recall autopilot on`"
        )
    new = sorted({k for r in rows for k in r["new_hooks"]})
    if new:
        print(f"  new hooks available ({', '.join(new)}): run `recall install` to add them")
    if any(r["hooks_run"] for r in rows):
        print("  `recall install --repoint` points the hooks at this recall; autopilot never moves them")
    for line in autopilot.recent():
        print(f"  {line}")
    for line in power.summary():
        print(f"\n{line}")
    if parked := indexer.parked():
        print(
            f"\n{len(parked)} chat(s) failed to index {indexer.MAX_ATTEMPTS} times and wait for their files to change;"
            " `recall index --force` tries them now:"
        )
        print("\n".join(f"  {key.replace(home, '~')}" for key in parked[:5]) + ("\n  …" if len(parked) > 5 else ""))
    if note := codex.unreadable_note():
        print(f"\n{note}")


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    secs = time.time() - ts
    return (
        "just now"
        if secs < 90
        else f"{secs / 60:.0f} min ago"
        if secs < 5400
        else f"{secs / 3600:.0f} h ago"
        if secs < 172800
        else f"{secs / 86400:.0f} days ago"
    )


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


# evals whose case ids are generated, so they never hold a comma; the others take --case verbatim
COMMA_SAFE_CASES = ("memorycode", "swe-contextbench")


def cmd_eval(a) -> None:
    from . import evals
    from .chats import store

    if a.case and a.kind in COMMA_SAFE_CASES:  # ID,ID,... as well as repeated --case
        a.case = [i for c in a.case for i in c.split(",") if i]
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
    if a.kind == "links":
        from . import evals_links

        evals_links.from_args(a)
        return
    if a.kind == "jit":
        from . import evals_jit

        evals_jit.from_args(a)
        return
    if a.kind == "agent" or a.v2:
        _eval_v2(a)
    elif a.kind in ("longmemeval", "locomo", "beam"):
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
        elif a.kind == "beam":
            if not a.qa:
                sys.exit("recall eval beam needs --qa MODEL, the reader (--judge defaults to a different, fixed model)")
            from . import evals_beam

            evals_beam.run_beam_qa(
                a.cases, a.qa, a.judge, top_k=a.top_k, embedder=embedder, cache=cache, out_path=a.out, limit=a.limit
            )
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
            con,
            cases,
            run,
            a.model,
            a.judge or harness.JUDGE,
            a.fresh,
            a.repeats,
            a.consistency,
            a.against,
            a.from_candidates,
            a.candidates_repeat,
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
    sp.add_argument(
        "--autopilot",
        action=argparse.BooleanOptionalAction,
        help="keep every agent folder connected automatically (asked once otherwise; --yes does not say yes to it)",
    )
    sp = command("model", cmd_model, "choose which model writes memories")
    sp.add_argument("--show", action="store_true", help="print the current choice")

    sp = command("index", cmd_index, "index new and changed chats")
    sp.add_argument("--force", action="store_true", help="re-read every chat")
    sp.add_argument("--source", action="append", help="claude, codex, cursor or cursor-legacy")
    sp.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
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
    sp = command("forget", cmd_forget, "remove a chat, or a project's chats, from recall's index and archive")
    sp.add_argument("chat_id", nargs="?")
    sp.add_argument("--project", help="every chat in this project")
    sp.add_argument("--yes", action="store_true", help="do not ask first")
    sp = command("why", cmd_why, "the chats that wrote a file, a line or a commit (git blame, read-only)")
    sp.add_argument("target", nargs="?", help="path, path:line or path:start-end")
    sp.add_argument("--commit", help="a commit sha (or any name git knows) instead of a file")
    sp.add_argument("--json", action="store_true")
    sp = command("links", cmd_links, "link commits to the chats that produced them (local only)")
    sp.add_argument(
        "--episodes",
        action="store_true",
        help=(
            "EXPERIMENTAL: list chats where something failed, then a commit fixed it. "
            "A judge found 24%% of them right (5 of 21), so do not build on it"
        ),
    )
    sp.add_argument("--json", action="store_true")

    sp = command("dream", cmd_dream, "distil new chats into memory notes (runs nightly)")
    sp.add_argument("--model", help="one-off override, e.g. claude:haiku or local:qwen3:4b")
    sp.add_argument("--max-chats", type=int, help="read at most N chats")
    sp.add_argument("--max-calls", type=int, help="stop after N model calls")
    sp.add_argument("--max-minutes", type=int, help="time budget; unread chats carry over")
    sp.add_argument("--max-usd", type=float, help="dollar budget for an API key (default: [dream] max_usd)")
    sp.add_argument("--no-batch", action="store_true", help="never use the provider's batch API this run")
    sp.add_argument("--extract-only", action="store_true", help="read chats but do not update notes")
    sp.add_argument("--consolidate-only", action="store_true", help="update notes without reading chats")
    sp.add_argument(
        "--on-battery",
        choices=["defer", "run"],
        help="on battery: wait for power, or run (default: [power] on_battery)",
    )
    sp.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
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
    sp = command("jit", cmd_jit, "what recall would hand an agent mid-session, and why")
    sp.add_argument("prompt", nargs="*", help="a prompt to match against past chats and notes")
    sp.add_argument("--cwd", help="the project folder (default: the current folder)")
    sp.add_argument("--file", help="a file the agent reads: rules, notes and chats anchored to it")
    sp.add_argument("--error", help="a failed command's output: past chats with the same error")
    sp.add_argument("--agent", default="claude", help="which agent: claude, codex or cursor")
    sp.add_argument("--json", action="store_true", help="also print each match's features")
    sp = command("usage", cmd_usage, "how recall itself gets used, from its local log")
    sp.add_argument("--since", help="e.g. 7d, 2w or 2026-10-01")
    sp.add_argument("--json", action="store_true")

    for name, help_text in (("install", "connect recall to your agents"), ("uninstall", "disconnect them")):
        sp = command(name, cmd_install, help_text)
        sp.add_argument("--only", help="comma-separated subset of: mcp,skill,hook (install: also retention)")
        sp.add_argument(
            "--root",
            action="append",
            help="only this root (see `recall doctor`); uninstall keeps autopilot out of it, install lets it back in",
        )
        if name == "install":
            sp.add_argument("--dry-run", action="store_true")
            sp.add_argument(
                "--repoint",
                action="store_true",
                help="point hooks that run another recall at this one (Codex asks to trust them again)",
            )
    sp = command("autopilot", cmd_autopilot, "connect new agent folders by itself (on, off, or run the check now)")
    sp.add_argument("action", nargs="?", choices=["run", "on", "off"], default="run")
    sp.add_argument("--dry-run", action="store_true", help="show what would be connected")
    sp.add_argument("--quiet", action="store_true")
    sp = command("doctor", cmd_doctor, "each agent folder recall found: connected or not, and when last indexed")
    sp.add_argument("--json", action="store_true")
    sp = command("schedule", cmd_schedule, "run the memory update nightly")
    sp.add_argument("action", choices=["enable", "disable", "status"])
    sp.add_argument("--at", default="03:30", help="HH:MM, local time")
    sp.add_argument("--max-minutes", type=int)
    sp.add_argument("--max-calls", type=int, help="cap on model calls per night")
    command("serve", cmd_serve, "run the MCP server over stdio")

    sp = command("eval", cmd_eval, "measure search, answer, memory or session-start quality; `eval compare` pairs runs")
    kinds = [
        "search",
        "memory",
        "session",
        "agent",
        "longmemeval",
        "locomo",
        "beam",
        "swe-contextbench",
        "memorycode",
        "amb",
        "explore",
        "links",
        "jit",
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
        help="--qa, beam: grade with MODEL, never the reader (default claude:claude-opus-5-5); "
        "agent, memory --v2: default claude:claude-sonnet-5-5",
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
    v2.add_argument(
        "--case",
        action="append",
        metavar="ID",
        help="only this case; repeatable (memorycode and swe-contextbench also take ID,ID,...)",
    )
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
    v2.add_argument(
        "--from-candidates",
        metavar="RUN_DIR",
        help="memorycode, memory --v2: reuse that run's extracted candidates and rerun only consolidation",
    )
    v2.add_argument(
        "--candidates-repeat", type=int, metavar="K", help="--from-candidates: replay only that run's repeat K"
    )
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
        "--arm",
        default="tools",
        help="memorycode --agent: none, tools, notes, hook, placebo, full, hook@3000, jit, jit+hook@3000 or jit-notes",
    )
    mc.add_argument("--notes", metavar="RUN_DIR", help="memorycode --arm notes: the extraction run whose notes to use")
    mc.add_argument("--queries", type=int, default=3, help="memorycode --agent: requests per history (0: all)")
    mc.add_argument(
        "--unprompted",
        action="store_true",
        help="memorycode --agent: ask as a plain task that never mentions past sessions, so the agent decides "
        "on its own whether to use recall",
    )
    sp.add_argument("--episodes", action="store_true", help="links: judge failure-then-fix episodes instead")
    jg = sp.add_argument_group(
        "jit (also --cases: the pairs file, --judge, --workers, --keyword: fit without embeddings)"
    )
    jg.add_argument("--build", type=int, metavar="N", help="replay N prompts from your history into pairs to label")
    jg.add_argument("--label", action="store_true", help="label the unlabelled pairs with the judge")
    jg.add_argument("--refresh", action="store_true", help="recompute the pairs' features, keeping their labels")
    jg.add_argument("--live", action="store_true", help="label the prompt fires in the usage log: live precision")
    jg.add_argument("--write", action="store_true", help="install the fitted gate for this machine's hooks")
    return p


if __name__ == "__main__":
    main()

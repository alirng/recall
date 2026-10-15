"""Setup eval: does autopilot connect recall to every agent folder with no input, and touch nothing else?

Each scenario builds a sandbox HOME: fake Claude Code, Codex and Cursor folders that hold the person's
own settings, fake secrets (canaries) and a chat or two. recall's real commands then run in a
subprocess with HOME, RECALL_HOME, RECALL_MEMORY and PATH pointed at the sandbox (stdin closed, so
there is no one to ask), and the result is read off the disk:

  connected   every folder has recall's MCP server, hooks and skill. Codex runs a hook only after a
              one-time trust in /hooks, so its hooks count as "written, trust pending" and not as connected.
  touched     files created or changed in agent folders; each changed file must have a .recall-backup
              holding what it held before; nothing new may appear outside the parts recall installs.
  history     chat history (projects/, sessions/, state.vscdb) must be byte for byte as it was.
  leaks       a canary (token, key, raw email) may appear only in files that held it before.
  check       the session-start check, in ms, once everything is connected, and the whole hook's wall time.

Run:  uv run python docs/benchmarks/setup_eval.py [--scenario NAME] [--json out.json]
It uses the real `claude` CLI when it is on PATH (`claude mcp add` is how Claude Code's MCP list is written),
inside the sandbox; the real home is never touched (a tripwire checks a few of its files before and after).
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

DARWIN = sys.platform == "darwin"
ROOT_FILES = {"settings.json", "hooks.json", "config.toml", "mcp.json", ".claude.json"}
HISTORY = ("/projects/", "/sessions/", "/archived_sessions/", "state.vscdb")
REAL_HOME = os.path.expanduser("~")
TRIPWIRE = [
    ".claude/settings.json",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".cursor/mcp.json",
    ".cursor/hooks.json",
]
SECRETS = {
    k: f"canary-{k}-9c41e7"
    for k in ("rc", "claude-env", "claude-mcp", "codex-auth", "codex-mcp", "cursor-token", "cursor-mcp")
}
EMAILS = {"claude": "pat.canary@example.test", "work": "sam.canary@corp.example", "cursor": "kim.canary@example.test"}


# --- building a sandbox -----------------------------------------------------------------------------


def slurp(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def put(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def jput(path: str, data) -> None:
    put(path, json.dumps(data, indent=2))


def new_sandbox() -> str:
    sb = os.path.realpath(tempfile.mkdtemp(prefix="recall-setup-eval-"))
    wrapper = os.path.join(sb, "bin", "recall")
    put(wrapper, f'#!/bin/sh\nexec "{sys.executable}" -m recall "$@"\n')
    os.chmod(wrapper, 0o755)
    return sb


def claude_folder(sb: str, folder: str, email: str, global_file: str, chat: str | None = None) -> None:
    d = os.path.join(sb, folder)
    jput(
        os.path.join(d, "settings.json"),
        {
            "model": "opus",
            "env": {"MY_TOKEN": SECRETS["claude-env"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]},
        },
    )
    jput(
        os.path.join(sb, global_file),
        {
            "oauthAccount": {"emailAddress": email, "organizationName": "Sandbox Org"},
            "mcpServers": {"other": {"command": "npx", "args": ["x"], "env": {"TOKEN": SECRETS["claude-mcp"]}}},
        },
    )
    if chat:
        lines = [
            {"type": "user", "cwd": "/sandbox/app", "timestamp": "2026-10-01T10:00:00Z", "message": {"content": chat}},
            {
                "type": "assistant",
                "timestamp": "2026-10-01T10:00:05Z",
                "message": {"content": [{"type": "text", "text": "Use exponential backoff."}]},
            },
        ]
        put(
            os.path.join(d, "projects", "-sandbox-app", f"{abs(hash(chat)) % 10**8:08d}.jsonl"),
            "\n".join(json.dumps(x) for x in lines) + "\n",
        )


def codex_folder(sb: str, chat: str) -> None:
    d = os.path.join(sb, ".codex")
    put(
        os.path.join(d, "config.toml"),
        'model = "gpt"\n\n[mcp_servers.other]\ncommand = "npx"\nargs = ["x"]\n'
        f'env = {{ TOKEN = "{SECRETS["codex-mcp"]}" }}\n',
    )
    t = SECRETS["codex-auth"]
    jput(
        os.path.join(d, "auth.json"),
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {"access_token": t, "refresh_token": t, "id_token": t, "account_id": t},
        },
    )
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-01T10:00:00Z", "payload": {"id": "cx-1", "cwd": "/sandbox/app"}},
        {
            "type": "response_item",
            "timestamp": "2026-10-01T10:00:01Z",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": chat}]},
        },
        {
            "type": "response_item",
            "timestamp": "2026-10-01T10:00:02Z",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]},
        },
    ]
    put(os.path.join(d, "sessions", "2026", "10", "01", "rollout-cx-1.jsonl"), "\n".join(map(json.dumps, rows)) + "\n")


def cursor_build(sb: str, app: str, email: str, chat: str, cid: str) -> None:
    base = os.path.join(sb, "Library", "Application Support") if DARWIN else os.path.join(sb, ".config")
    db = os.path.join(base, app, "User", "globalStorage", "state.vscdb")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table ItemTable (key text primary key, value text)")
    con.execute("create table cursorDiskKV (key text primary key, value text)")
    con.executemany(
        "insert into ItemTable values (?,?)",
        [
            ("cursorAuth/cachedEmail", email),
            ("cursorAuth/accessToken", SECRETS["cursor-token"]),
            ("cursorAuth/refreshToken", SECRETS["cursor-token"]),
        ],
    )
    ms = 1_790_000_000_000
    header = {
        "name": f"{app} chat",
        "createdAt": ms,
        "lastUpdatedAt": ms,
        "conversation": [{"type": 1, "text": chat, "createdAt": ms}, {"type": 2, "text": "ok", "createdAt": ms}],
    }
    con.execute("insert into cursorDiskKV values (?,?)", (f"composerData:{cid}", json.dumps(header)))
    con.commit()
    con.close()


def cursor_home(sb: str) -> None:
    jput(
        os.path.join(sb, ".cursor", "mcp.json"),
        {"mcpServers": {"other": {"command": "npx", "env": {"T": SECRETS["cursor-mcp"]}}}},
    )
    os.makedirs(os.path.join(sb, ".cursor", "projects"), exist_ok=True)


def shell_rc(sb: str, alias: bool) -> None:
    text = f"export OPENAI_API_KEY={SECRETS['rc']}\n"
    if alias:
        text += "alias claude-work='CLAUDE_CONFIG_DIR=\"$HOME/.claude-work\" claude'\n"
    put(os.path.join(sb, ".zshrc"), text)


def turn_on(sb: str, on: bool = True) -> None:
    """What `recall setup` records after the one-time yes."""
    put(os.path.join(sb, ".agents", "recall", "config.toml"), f"[autopilot]\nconnect = {'true' if on else 'false'}\n")


# --- running recall in it -------------------------------------------------------------------------


def run(sb: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDE", "CURSOR_", "RECALL_"))}
    env |= {
        "HOME": sb,
        "RECALL_HOME": os.path.join(sb, ".agents", "recall"),
        "RECALL_MEMORY": os.path.join(sb, ".agents", "memory"),
        "PATH": os.path.join(sb, "bin") + os.pathsep + os.environ["PATH"],
    }
    done = subprocess.run(
        [sys.executable, "-m", "recall", *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return done.returncode, done.stdout + done.stderr


def session_start(sb: str, wait: bool = True) -> float:
    """What an agent does when a session starts: pipe the hook its JSON. Returns the wall time in ms;
    with wait, also lets the background run it starts finish."""
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": sb, "source": "startup"})
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDE", "CURSOR_", "RECALL_"))}
    env |= {
        "HOME": sb,
        "RECALL_HOME": os.path.join(sb, ".agents", "recall"),
        "RECALL_MEMORY": os.path.join(sb, ".agents", "memory"),
        "PATH": os.path.join(sb, "bin") + os.pathsep + os.environ["PATH"],
    }
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "recall", "hook", "--agent", "claude"],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    ms = (time.perf_counter() - start) * 1000
    if wait:  # the detached `recall autopilot` it started holds this lock while it works
        time.sleep(0.5)
        lock = os.path.join(sb, ".agents", "recall", "autopilot.lock")
        end = time.time() + 40
        while time.time() < end and _locked(lock):
            time.sleep(0.3)
    return ms


def _locked(path: str) -> bool:
    import fcntl

    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except (BlockingIOError, OSError):
        return True


def doctor(sb: str) -> list[dict]:
    rc, out = run(sb, "doctor", "--json")
    return json.loads(out) if rc == 0 else []


# --- reading the result ------------------------------------------------------------------------------


def snap(sb: str) -> dict[str, bytes | str]:
    out: dict[str, bytes | str] = {}
    for here, dirs, files in os.walk(sb):
        rel_here = os.path.relpath(here, sb)
        if rel_here == "bin" or rel_here.startswith("bin" + os.sep):
            continue
        for name in files + [d for d in dirs if os.path.islink(os.path.join(here, d))]:
            path = os.path.join(here, name)
            rel = os.path.normpath(os.path.join(rel_here, name))
            if os.path.islink(path):
                out[rel] = "link:" + os.readlink(path)
            elif os.path.getsize(path) <= 4_000_000:
                with open(path, "rb") as f:
                    out[rel] = f.read()
            else:
                out[rel] = hashlib.sha1(slurp(path)).hexdigest()
    return out


def diff(before: dict, after: dict) -> dict[str, str]:
    d = {p: "created" for p in after if p not in before}
    d |= {p: "deleted" for p in before if p not in after}
    d |= {p: "modified" for p in after if p in before and after[p] != before[p]}
    return d


def judge(before: dict, after: dict) -> dict:
    """touched / unbacked / unexpected / history / leaks, from two snapshots."""
    changes = diff(before, after)
    own = lambda p: p.startswith(".agents" + os.sep)  # noqa: E731  recall's own folders
    touched, unbacked, unexpected, history = [], [], [], []
    for path, kind in sorted(changes.items()):
        full = "/" + path
        if any(h in full for h in HISTORY) and not own(path):
            history.append(path)
            continue
        base = os.path.basename(path)
        if own(path) or base.endswith(".recall-backup") or "/backups/" in full:
            continue
        touched.append(f"{kind} {path}")
        if kind == "modified":
            backup = after.get(path + ".recall-backup")
            if backup != before[path]:
                unbacked.append(path)
        elif kind == "created":
            ok = base in ROOT_FILES or "/skills/recall" in full or full.endswith("/skills")
            if not ok:
                unexpected.append(path)
        else:
            unexpected.append(f"deleted {path}")
    return {"touched": touched, "unbacked": unbacked, "unexpected": unexpected, "history": history}


def leaks(before: dict, after: dict, outputs: str) -> list[str]:
    """Secrets (and raw emails) in a place they were not in before: recall's own files, its output."""
    needles = [*SECRETS.values(), *EMAILS.values()]
    found = []
    for path, data in after.items():
        if isinstance(data, str):
            continue
        for n in needles:
            if n.encode() in data and not (path in before and n.encode() in before[path]):
                if path.endswith(".recall-backup") and n.encode() in before.get(path[: -len(".recall-backup")], b""):
                    continue  # a backup of a file that held it
                if "/backups/" in "/" + path:
                    continue  # Claude Code's own backup of its own file
                found.append(f"{n} in {path}")
    found += [f"{n} in command output" for n in needles if n in outputs]
    return found


def parts_ok(rows: list[dict]) -> tuple[int, int, int]:
    """(parts in place, parts expected, hooks waiting for trust) over the folders that exist."""
    have = want = trust = 0
    for r in rows:
        if not r["exists"]:
            continue
        for part, ok in r["parts"].items():
            if part == "hook" and r["agent"] == "codex":
                trust += 1 if ok else 0
                continue
            want += 1
            have += 1 if ok else 0
    return have, want, trust


def indexed(sb: str) -> dict[str, int]:
    run(sb, "index")
    db = os.path.join(sb, ".agents", "recall", "recall.db")
    if not os.path.exists(db):
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    return {r or "?": n for r, n in con.execute("select root, count(*) from sessions group by root")}


# --- scenarios ------------------------------------------------------------------------------------------
# A scenario is build(sb) -> act(sb, ctx) -> check(sb, ctx). act wraps each recall command in seg(), which
# snapshots the sandbox before and after, so a fixture step (a folder the person creates) is not blamed on recall.


def seg(sb: str, ctx: dict, name: str, fn, judged: bool = True) -> None:
    before = snap(sb)
    out = fn()
    ctx.setdefault("segments", []).append(
        {"name": name, "before": before, "after": snap(sb), "out": out or "", "judged": judged}
    )


def autopilot_run(sb: str) -> str:
    return run(sb, "autopilot", "--quiet")[1]


def fresh(agent: str):
    def build(sb: str) -> None:
        shell_rc(sb, alias=False)
        if agent == "claude":
            claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
        elif agent == "codex":
            codex_folder(sb, "how do I retry a failing request")
        else:
            cursor_home(sb)
            cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
        turn_on(sb)

    def act(sb: str, ctx: dict) -> None:
        seg(sb, ctx, "nightly", lambda: autopilot_run(sb))
        seg(sb, ctx, "next night", lambda: autopilot_run(sb))

    def check(sb: str, ctx: dict) -> list[str]:
        again = ctx["segments"][1]  # a second run must find nothing to do
        state = UNRELATED + (".agents/recall/autopilot.json",)  # recall's own note of when it looked
        changed = [p for p in diff(again["before"], again["after"]) if not p.startswith(state)]
        return [f"a second run changed {changed[:3]}"] if changed else []

    return build, act, check


def alias_build(sb: str) -> None:
    shell_rc(sb, alias=True)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    turn_on(sb)


def alias_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "alias only", lambda: autopilot_run(sb))  # the work folder is named by the alias, not created
    ctx["pending_untouched"] = not os.path.exists(os.path.join(sb, ".claude-work"))
    ctx["default_json"] = slurp(os.path.join(sb, ".claude.json"))
    # first launch of `claude-work`: Claude creates the folder, the person signs in, a chat happens
    claude_folder(sb, ".claude-work", EMAILS["work"], ".claude-work/.claude.json", "work account question")
    seg(sb, ctx, "after first launch", lambda: autopilot_run(sb))


def alias_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if not ctx.get("pending_untouched"):
        errs.append("created the work folder before it existed")
    if slurp(os.path.join(sb, ".claude.json")) != ctx["default_json"]:
        errs.append("the work folder's MCP server went into the default account's .claude.json")
    if ctx["indexed"] != {"claude": 1, "claude-work": 1}:
        errs.append(f"indexed {ctx['indexed']}, expected one chat each in claude and claude-work")
    return errs


def wipe_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    cursor_home(sb)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
    turn_on(sb)


def wipe_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb), judged=False)
    # an update (or a sync of dotfiles) wipes recall's hooks and server entries
    p = os.path.join(sb, ".claude", "settings.json")
    s = json.loads(slurp(p))
    s["hooks"].pop("SessionStart"), s["hooks"].pop("PreToolUse")
    jput(p, s)
    os.remove(os.path.join(sb, ".codex", "hooks.json"))
    os.remove(os.path.join(sb, ".cursor", "hooks.json"))
    os.remove(os.path.join(sb, ".claude", "skills", "recall"))
    c = os.path.join(sb, ".codex", "config.toml")
    put(c, slurp(c).decode().split("[mcp_servers.recall]")[0].rstrip() + "\n")
    # the next Claude Code session notices, and the background run it starts repairs everything
    seg(sb, ctx, "repair", lambda: session_start(sb) and "")


def wipe_check(sb: str, ctx: dict) -> list[str]:
    s = json.loads(slurp(os.path.join(sb, ".claude", "settings.json")))
    if s.get("env", {}).get("MY_TOKEN") != SECRETS["claude-env"] or "Stop" not in s["hooks"]:
        return ["the person's own settings were lost"]
    return []


def variant_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    cursor_home(sb)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
    turn_on(sb)


def variant_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb), judged=False)
    cursor_build(sb, "Cursor Nightly", EMAILS["cursor"], "nightly chat about caching", "cu-2")
    seg(sb, ctx, "second build", lambda: autopilot_run(sb))


def variant_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if ctx["indexed"] != {"cursor": 1, "cursor-nightly": 1}:
        errs.append(f"indexed {ctx['indexed']}, expected one chat each in cursor and cursor-nightly")
    last = ctx["segments"][1]
    changed = [p for p in diff(last["before"], last["after"]) if not p.startswith(".agents")]
    if changed:
        errs.append(f"the second build needed changes to {changed}, but it shares ~/.cursor")
    return errs


def off_build(sb: str) -> None:
    shell_rc(sb, alias=True)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    cursor_home(sb)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
    cursor_build(sb, "Cursor Nightly", EMAILS["cursor"], "nightly chat", "cu-2")
    claude_folder(sb, ".claude-work", EMAILS["work"], ".claude-work/.claude.json")
    turn_on(sb, on=False)


def off_act(sb: str, ctx: dict) -> None:
    def everything() -> str:
        out = [run(sb, "autopilot", "--quiet")[1], run(sb, "autopilot")[1], run(sb, "autopilot", "--dry-run")[1]]
        session_start(sb)
        out += [run(sb, "doctor")[1], run(sb, "status", "--since", "1h")[1]]
        return "\n".join(out)

    seg(sb, ctx, "off", everything)


# What a session-start hook and an index may write whatever autopilot does: recall's usage log and index.
UNRELATED = (
    ".agents/recall/usage.jsonl",
    ".agents/recall/recall.db",
    ".agents/recall/cache/",
    ".agents/recall/index.lock",
    ".agents/memory/",
)


def off_check(sb: str, ctx: dict) -> list[str]:
    s = ctx["segments"][0]
    changed = diff(s["before"], s["after"])
    return [f"{k} {p}" for p, k in sorted(changed.items()) if not p.startswith(UNRELATED)]


def uninstall_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb))
    seg(sb, ctx, "uninstall", lambda: run(sb, "uninstall")[1], judged=False)
    seg(sb, ctx, "after uninstall", lambda: autopilot_run(sb) + str(session_start(sb)), judged=False)


def uninstall_check(sb: str, ctx: dict) -> list[str]:
    s = ctx["segments"][2]
    changed = [p for p in diff(s["before"], s["after"]) if not p.startswith(UNRELATED)]
    return [f"something reconnected after uninstall: {changed[:3]}"] if changed else []


SCENARIOS = {
    "fresh-claude": ("Claude Code installed, nothing connected", *fresh("claude")),
    "fresh-codex": ("Codex installed, nothing connected", *fresh("codex")),
    "fresh-cursor": ("Cursor installed, nothing connected", *fresh("cursor")),
    "claude-alias": ("work account: alias, then first launch", alias_build, alias_act, alias_check),
    "update-wipes-hooks": ("an update wipes hooks, servers and skill", wipe_build, wipe_act, wipe_check),
    "cursor-variant": ("a second Cursor build (Nightly)", variant_build, variant_act, variant_check),
    "setting-off": ("autopilot off, three agents and a work alias", off_build, off_act, off_check),
    "uninstall-stays-off": ("recall uninstall, then nothing reconnects", wipe_build, uninstall_act, uninstall_check),
}
NOTHING_CHANGES = {"setting-off"}  # connection is not expected there
NO_INDEX = {"setting-off", "uninstall-stays-off"}


def scenario(name: str) -> dict:
    title, build, act, check = SCENARIOS[name]
    sb = new_sandbox()
    try:
        build(sb)
        ctx: dict = {}
        start = time.perf_counter()
        act(sb, ctx)
        ctx["seconds"] = round(time.perf_counter() - start, 1)
        # the report commands and the index run last, still inside a watched segment
        report: dict = {}

        def finish() -> str:
            report["rows"] = doctor(sb)
            report["indexed"] = {} if name in NO_INDEX else indexed(sb)
            return run(sb, "doctor")[1] + run(sb, "autopilot", "--dry-run")[1]

        seg(sb, ctx, "report", finish)
        ctx["indexed"] = report["indexed"]
        have, want, trust = parts_ok(report["rows"])
        judged = [x for x in ctx["segments"] if x["judged"]]
        j = {"touched": [], "unbacked": [], "unexpected": [], "history": []}
        lk = []
        for x in judged:
            one = judge(x["before"], x["after"])
            for k in j:
                j[k] += one[k]
            lk += leaks(x["before"], x["after"], x["out"])
        errs = list(check(sb, ctx))
        if name not in NOTHING_CHANGES and name != "uninstall-stays-off" and have < want:
            errs.append(f"{want - have} of {want} parts not connected")
        if name == "fresh-claude" and ctx["indexed"] != {"claude": 1}:
            errs.append(f"indexed {ctx['indexed']}, expected one chat in claude")
        if name == "fresh-codex" and ctx["indexed"] != {"codex": 1}:
            errs.append(f"indexed {ctx['indexed']}, expected one chat in codex")
        if name == "fresh-cursor" and ctx["indexed"] != {"cursor": 1}:
            errs.append(f"indexed {ctx['indexed']}, expected one chat in cursor")
        if j["unbacked"]:
            errs.append(f"changed without a backup: {j['unbacked']}")
        if j["unexpected"]:
            errs.append(f"unexpected files: {j['unexpected']}")
        if j["history"]:
            errs.append(f"chat history touched: {j['history']}")
        if lk:
            errs.append(f"leaks: {lk[:3]}")
        if name in NOTHING_CHANGES and j["touched"]:
            errs.append(f"changed with the setting off: {j['touched']}")
        timing = check_time(sb) if name == "update-wipes-hooks" else None
        return {
            "scenario": name,
            "title": title,
            "connected": f"{have}/{want}" + (f" +{trust} hooks need trust" if trust else ""),
            "files_touched": len(j["touched"]),
            "touched": j["touched"],
            "unbacked": len(j["unbacked"]),
            "history_touched": len(j["history"]),
            "leaks": len(lk),
            "indexed": ctx["indexed"],
            "seconds": ctx["seconds"],
            "timing": timing,
            "errors": errs,
            "pass": not errs,
        }
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def check_time(sb: str) -> dict:
    """The session-start check once everything is connected: inside the process, and the whole hook."""
    state = os.path.join(sb, ".agents", "recall", "autopilot.json")
    if os.path.exists(state):
        os.remove(state)  # a repair just made would otherwise answer the next hour's checks without looking
    code = (
        "import time,json,statistics\n"
        "from recall import autopilot\n"
        "xs=[]\n"
        "for _ in range(60):\n"
        "    t=time.perf_counter(); autopilot.on_session_start(None); xs.append((time.perf_counter()-t)*1000)\n"
        "xs.sort()\n"
        "print(json.dumps({'median': statistics.median(xs), 'p95': xs[int(len(xs)*.95)]}))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDE", "CURSOR_", "RECALL_"))}
    env |= {
        "HOME": sb,
        "RECALL_HOME": os.path.join(sb, ".agents", "recall"),
        "PATH": os.path.join(sb, "bin") + os.pathsep + os.environ["PATH"],
    }
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    inner = json.loads(out.stdout.strip().splitlines()[-1])
    on = [session_start(sb, wait=False) for _ in range(7)]
    turn_on(sb, on=False)
    off = [session_start(sb, wait=False) for _ in range(7)]
    return {
        "check_ms_median": round(inner["median"], 2),
        "check_ms_p95": round(inner["p95"], 2),
        "hook_ms_on": round(statistics.median(on)),
        "hook_ms_off": round(statistics.median(off)),
    }


def tripwire() -> dict[str, str | None]:
    out = {}
    for rel in TRIPWIRE:
        p = os.path.join(REAL_HOME, rel)
        out[rel] = hashlib.sha1(slurp(p)).hexdigest() if os.path.exists(p) else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", action="append", choices=list(SCENARIOS), help="only this one; repeatable")
    ap.add_argument("--json", help="write the results here")
    a = ap.parse_args()
    real_before = tripwire()
    results = [scenario(n) for n in a.scenario or SCENARIOS]
    if tripwire() != real_before:
        print("FAIL: the eval touched the real home", file=sys.stderr)
        sys.exit(2)
    w = max(len(r["scenario"]) for r in results)
    print(f"{'scenario':{w}}  connected                       touched  unbacked  history  leaks  indexed")
    for r in results:
        idx = ", ".join(f"{k} {v}" for k, v in sorted(r["indexed"].items())) or "-"
        print(
            f"{r['scenario']:{w}}  {r['connected']:30}  {r['files_touched']:7}  {r['unbacked']:8}  "
            f"{r['history_touched']:7}  {r['leaks']:5}  {idx}  {'PASS' if r['pass'] else 'FAIL'}"
        )
        for e in r["errors"]:
            print(f"    - {e}")
    for r in results:
        if r["timing"]:
            t = r["timing"]
            print(
                f"{r['scenario']}: session-start check median {t['check_ms_median']} ms (p95 {t['check_ms_p95']}); "
                f"whole `recall hook` {t['hook_ms_on']} ms on, {t['hook_ms_off']} ms with autopilot off"
            )
            break
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=2)
    sys.exit(0 if all(r["pass"] for r in results) else 1)


if __name__ == "__main__":
    main()

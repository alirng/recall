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
import re
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
DOMAINS = ("example.test", "corp.example")  # the masked hint hides the employer's domain too


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


def codex_folder(sb: str, chat: str, folder: str = ".codex") -> None:
    d = os.path.join(sb, folder)
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
    """What `recall setup` records after the one-time yes: the setting and the hooks the yes covers
    (through the real code, without running the check)."""
    run_py(sb, f"from recall import autopilot; autopilot.set_enabled({on})")


# --- running recall in it -------------------------------------------------------------------------


def sandbox_env(sb: str, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDE", "CURSOR_", "RECALL_"))}
    env |= {
        "HOME": sb,
        "RECALL_HOME": os.path.join(sb, ".agents", "recall"),
        "RECALL_MEMORY": os.path.join(sb, ".agents", "memory"),
        "PATH": os.path.join(sb, "bin") + os.pathsep + os.environ["PATH"],
    }
    return env | (extra or {})


def run(sb: str, *args: str, timeout: int = 120, env: dict | None = None) -> tuple[int, str]:
    done = subprocess.run(
        [sys.executable, "-m", "recall", *args],
        env=sandbox_env(sb, env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return done.returncode, done.stdout + done.stderr


def run_py(sb: str, code: str, env: dict | None = None, timeout: int = 120) -> str:
    """Python inside the sandbox, importing the recall under test."""
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=sandbox_env(sb, env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return done.stdout + done.stderr


def session_start(sb: str, wait: bool = True) -> float:
    """What an agent does when a session starts: pipe the hook its JSON. Returns the wall time in ms;
    with wait, also lets the background run it starts finish."""
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": sb, "source": "startup"})
    env = sandbox_env(sb)
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


def agent_cli(*names: str) -> str | None:
    for n in names:
        found = shutil.which(n) or (n if os.path.isabs(n) and os.path.exists(n) else None)
        if found:
            return found
    return None


NAME = "recall"
CURSOR = ("cursor-agent", os.path.expanduser("~/.local/bin/cursor-agent"))
CODEX = ("codex", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")


def readback(sb: str, rows: list[dict]) -> dict[str, str]:
    """Ask each agent's own CLI, in the sandbox, whether it sees recall's MCP server. recall's own check reads
    files recall wrote, so this is the part that an agent confirms. root -> what the agent said ('' if no CLI)."""
    out = {}
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDE", "CURSOR_", "RECALL_"))}
    base |= {"HOME": sb, "PATH": os.path.join(sb, "bin") + os.pathsep + os.environ["PATH"]}
    for r in rows:
        if not r["exists"]:
            continue
        env, args, want = dict(base), None, None
        if r["agent"] == "claude" and shutil.which("claude"):
            if r["root"] != "claude":
                env["CLAUDE_CONFIG_DIR"] = r["path"]
            args, want = ["claude", "mcp", "get", "recall"], "Connected"  # it starts the server and shakes hands
        elif r["agent"] == "codex" and agent_cli(*CODEX):
            env["CODEX_HOME"] = r["path"]
            args, want = [agent_cli(*CODEX), "mcp", "get", "recall"], "enabled: true"
        elif r["agent"] == "cursor" and r["root"] == "cursor" and agent_cli(*CURSOR):
            # Cursor lists a server it has not been told to load as "not loaded (needs approval)", which
            # matches the word "recall" too. Approve it here, as a person would once per project, then
            # the line must say it is ready.
            subprocess.run(
                [agent_cli(*CURSOR), "mcp", "enable", NAME],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                cwd=sb,
                stdin=subprocess.DEVNULL,
            )
            args, want = [agent_cli(*CURSOR), "mcp", "list"], f"{NAME}: ready"
        if not args:
            continue
        try:
            done = subprocess.run(
                args, env=env, capture_output=True, text=True, timeout=90, cwd=sb, stdin=subprocess.DEVNULL
            )
            text = done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            text = "timed out"
        out[r["root"]] = want if want in text else "NOT SEEN: " + " ".join(text.split())[:80]
    return out


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


BACKUP_OF = re.compile(r"(\.\d{8}-\d{6}(-\d+)?)?\.recall-backup$")
ROOT_DIRS = {".claude", ".codex", ".cursor", ".claude.json"}  # the agent folders every scenario has, by top-level name


def backups_of(path: str, after: dict) -> list:
    """What recall kept of a file: the first copy, <file>.recall-backup, and later ones, <file>.<time>.recall-backup."""
    return [
        v
        for k, v in after.items()
        if k == path + ".recall-backup" or (k.startswith(path + ".") and k.endswith(".recall-backup"))
    ]


def judge(before: dict, after: dict, roots: set | frozenset = ROOT_DIRS, links: dict | None = None) -> dict:
    """touched / unbacked / unexpected / history / leaks, from two snapshots. `roots`: the top-level
    folders of the agent roots this scenario has; a new config file anywhere else is unexpected."""
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
        top = path.split(os.sep)[0]
        if kind == "modified":
            if before[path] not in backups_of(path, after) + backups_of((links or {}).get(path, path), after):
                unbacked.append(path)
            if top not in roots:
                unexpected.append(f"modified {path}")
        elif kind == "created":
            ok = top in roots and (base in ROOT_FILES or "/skills/recall" in full or full.endswith("/skills"))
            if base == ".claude.json" and top == ".claude":
                ok = False  # recall never creates the file a launch with CLAUDE_CONFIG_DIR=~/.claude would use
            if not ok:
                unexpected.append(path)
        else:
            unexpected.append(f"deleted {path}")
    return {"touched": touched, "unbacked": unbacked, "unexpected": unexpected, "history": history}


def leaks(before: dict, after: dict, outputs: str) -> list[str]:
    """Secrets (and raw emails) in a place they were not in before: recall's own files, its output."""
    needles = [*SECRETS.values(), *EMAILS.values(), *DOMAINS]
    found = []
    for path, data in after.items():
        if isinstance(data, str):
            continue
        for n in needles:
            if n.encode() in data and not (path in before and n.encode() in before[path]):
                held = before.get(BACKUP_OF.sub("", path), b"")
                if path.endswith(".recall-backup") and n.encode() in (held if isinstance(held, bytes) else b""):
                    continue  # a backup of a file that held it
                if path.endswith(".recall-backup") and held and isinstance(held, str):
                    continue  # a backup made of what a symlink points to
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
        ctx["status"] = run(sb, "status", "--since", "1h")[1]

    def check(sb: str, ctx: dict) -> list[str]:
        again = ctx["segments"][1]  # a second run must find nothing to do
        state = (*UNRELATED, ".agents/recall/autopilot.json")  # recall's own note of when it looked
        changed = [p for p in diff(again["before"], again["after"]) if not p.startswith(state)]
        errs = [f"a second run changed {changed[:3]}"] if changed else []
        if agent == "codex" and ("awaiting Codex trust" not in ctx["status"] or "1 of 1 connected" in ctx["status"]):
            errs.append("recall status counts Codex hooks nobody has trusted as connected")
        return errs

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
    ctx["pristine"] = slurp(os.path.join(sb, ".claude", "settings.json"))
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
    mine = [h["command"] for g in s["hooks"].get("Stop", []) for h in g["hooks"]]
    if s.get("env", {}).get("MY_TOKEN") != SECRETS["claude-env"] or "echo done" not in mine:
        return ["the person's own settings were lost"]
    first = os.path.join(sb, ".claude", "settings.json.recall-backup")
    later = [k for k in snap(sb) if k.startswith(".claude/settings.json.2") and k.endswith(".recall-backup")]
    if slurp(first) != ctx["pristine"]:
        return ["the first backup was overwritten by a later run"]
    if not later:
        return ["the second change to settings.json left no backup of its own"]
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


# --- one scenario per review finding ----------------------------------------------------------------------


def connect_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "autopilot", lambda: autopilot_run(sb))


def rows_of(ctx: dict) -> list[str]:
    return [r["root"] for r in ctx["rows"]]


def default_named_build(sb: str) -> None:
    # CLAUDE_CONFIG_DIR and CODEX_HOME naming the default folders, in the rc and in the environment
    put(os.path.join(sb, ".zshrc"), 'export CLAUDE_CONFIG_DIR="$HOME/.claude"\nexport CODEX_HOME=$HOME/.codex\n')
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    turn_on(sb)


def default_named_act(sb: str, ctx: dict) -> None:
    env = {"CLAUDE_CONFIG_DIR": os.path.join(sb, ".claude"), "CODEX_HOME": os.path.join(sb, ".codex")}
    ctx["env"] = env
    seg(sb, ctx, "named, never launched that way", lambda: run(sb, "autopilot", env=env)[1])
    ctx["no_second_file"] = not os.path.exists(os.path.join(sb, ".claude", ".claude.json"))
    # someone launches Claude Code once with the variable: Claude Code makes ~/.claude/.claude.json itself
    jput(os.path.join(sb, ".claude", ".claude.json"), {"oauthAccount": {"emailAddress": EMAILS["claude"]}})
    seg(sb, ctx, "launched with the variable", lambda: run(sb, "autopilot", env=env)[1])


def default_named_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if not ctx["no_second_file"]:
        errs.append("recall created ~/.claude/.claude.json, which only a launch with the variable would use")
    if rows_of(ctx) != ["claude", "codex"]:
        errs.append(f"roots {rows_of(ctx)}, expected claude and codex")
    for label, path in (("~/.claude.json", ".claude.json"), ("~/.claude/.claude.json", ".claude/.claude.json")):
        found = json.loads(slurp(os.path.join(sb, path))).get("mcpServers", {})
        if "recall" not in found:
            errs.append(f"recall is missing from {label}")
    if shutil.which("claude"):
        env = sandbox_env(sb, {"CLAUDE_CONFIG_DIR": os.path.join(sb, ".claude")})
        said = subprocess.run(
            ["claude", "mcp", "get", "recall"], env=env, cwd=sb, capture_output=True, text=True, timeout=90
        )
        if "Connected" not in said.stdout + said.stderr:
            errs.append("Claude Code started with CLAUDE_CONFIG_DIR=~/.claude does not see the MCP server")
    return errs


def echo_build(sb: str) -> None:
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    # folders an agent has used, which the rc only mentions
    claude_folder(sb, "ghost-claude", EMAILS["work"], "ghost-claude/.claude.json", "a ghost chat")
    codex_folder(sb, "a ghost chat", "ghost-codex")
    put(
        os.path.join(sb, ".zshrc"),
        'echo "CLAUDE_CONFIG_DIR=$HOME/ghost-claude"\n'
        "printf 'export CODEX_HOME=%s\\n' \"$HOME/ghost-codex\"\n"
        'echo "a quoted line\nCLAUDE_CONFIG_DIR=$HOME/ghost-claude"   # don\'t follow this\n'
        "alias show='echo CODEX_HOME=$HOME/ghost-codex'\n"
        "# CLAUDE_CONFIG_DIR=$HOME/ghost-claude\n"
        "note CODEX_HOME=$HOME/ghost-codex\n"
        "grep -q CLAUDE_CONFIG_DIR=$HOME/ghost-claude notes.txt\n",
    )
    turn_on(sb)


def echo_check(sb: str, ctx: dict) -> list[str]:
    return [f"roots {rows_of(ctx)}, expected only claude"] if rows_of(ctx) != ["claude"] else []


def home_build(sb: str) -> None:
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    put(
        os.path.join(sb, ".zshrc"),
        "export CLAUDE_CONFIG_DIR=$HOME\nexport CODEX_HOME=$HOME/..\nexport CODEX_HOME=/Users\n"
        "alias cc='CLAUDE_CONFIG_DIR=\"${HOME}/\" claude'\n",
    )
    turn_on(sb)


def home_check(sb: str, ctx: dict) -> list[str]:
    return [f"roots {rows_of(ctx)}, expected claude and codex"] if rows_of(ctx) != ["claude", "codex"] else []


def backup_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    for name in (".claude-backup", ".claude_old", ".claude-2026-09"):  # by name, by name, by its chats
        shutil.copytree(os.path.join(sb, ".claude"), os.path.join(sb, name), symlinks=True)
    claude_folder(sb, ".claude-work", EMAILS["work"], ".claude-work/.claude.json", "work account question")
    turn_on(sb)


def backup_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if rows_of(ctx) != ["claude", "claude-work"]:
        errs.append(f"roots {rows_of(ctx)}, expected claude and claude-work")
    if ctx["indexed"] != {"claude": 1, "claude-work": 1}:
        errs.append(f"indexed {ctx['indexed']}: a backup's copies of chats must not be indexed again")
    return errs


def upgrade_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    code = """
from recall.chats import store
from recall.chats.transcript import Message, Session
con = store.connect()
for agent, nid, src in (
    ("claude", "gone-1", "SANDBOX/.claude/projects/-sandbox-gone/gone-1.jsonl"),
    ("codex", "gone-2", "SANDBOX/.codex/sessions/2026/09/01/rollout-gone-2.jsonl"),
):
    s = Session(agent, nid, src, project="/sandbox/gone", updated_at=1.79e9)
    ask, answer = "an old question about pagination cursors", "ok"
    s.messages = [Message("user", ask, 1.79e9), Message("assistant", answer, 1.79e9)]
    store.upsert(con, s)
# an index from before roots existed: no root column, schema number unchanged
con.execute("update sessions set root = null")
con.commit()
con.execute("alter table sessions drop column root")
con.commit()
print(con.execute("select v from meta where k = 'chat_schema'").fetchone()[0])
""".replace("SANDBOX", sb)
    out = run_py(sb, code)
    put(os.path.join(sb, ".agents", "recall", "old-schema"), out.strip() + "\n")


def upgrade_act(sb: str, ctx: dict) -> None:
    ctx["old_schema"] = slurp(os.path.join(sb, ".agents", "recall", "old-schema")).decode().strip()
    seg(sb, ctx, "upgrade", lambda: run(sb, "index")[1], judged=False)


def upgrade_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    db = sqlite3.connect(f"file:{os.path.join(sb, '.agents', 'recall', 'recall.db')}?mode=ro", uri=True)
    schema = db.execute("select v from meta where k = 'chat_schema'").fetchone()[0]
    if schema != ctx["old_schema"]:
        errs.append(f"the chat schema went from {ctx['old_schema']} to {schema}: that rebuilds, and drops, the index")
    rows = dict(db.execute("select native_id, root from sessions"))
    if rows.get("gone-1") != "claude" or rows.get("gone-2") != "codex":
        errs.append(f"chats whose files are gone: {rows}; expected them kept, named by the folder they were in")
    if len(rows) != 4:
        errs.append(f"{len(rows)} chats after the upgrade, expected 4 (2 old + 2 on disk)")
    if (
        db.execute(
            "select count(*) from messages where session_id in (select id from sessions where native_id like 'gone-%')"
        ).fetchone()[0]
        != 4
    ):
        errs.append("the old chats lost their messages")
    return errs


def vanished_build(sb: str, autopilot: bool = True) -> None:
    put(os.path.join(sb, ".zshrc"), "alias claude-acme='CLAUDE_CONFIG_DIR=\"$HOME/acme-claude\" claude'\n")
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    claude_folder(sb, "acme-claude", EMAILS["work"], "acme-claude/.claude.json", "an acme chat")
    turn_on(sb, autopilot)


def vanished_act(sb: str, ctx: dict, by_install: bool = False) -> None:
    # connected by autopilot, or by the person's own `recall install` with autopilot never turned on
    seg(sb, ctx, "connect", lambda: run(sb, "install")[1] if by_install else autopilot_run(sb))
    ctx["connected_first"] = (
        "recall" in json.loads(slurp(os.path.join(sb, "acme-claude", ".claude.json")))["mcpServers"]
    )
    shell_rc(sb, alias=False)  # the alias is taken out: acme-claude is no longer discovered
    seg(sb, ctx, "uninstall", lambda: run(sb, "uninstall")[1], judged=False)
    seg(sb, ctx, "afterwards", lambda: autopilot_run(sb) + str(session_start(sb)), judged=False)


def clean(sb: str, folder: str, global_file: str) -> list[str]:
    left = []
    if "recall" in json.loads(slurp(os.path.join(sb, global_file))).get("mcpServers", {}):
        left.append(f"MCP server in {global_file}")
    if "recall hook" in slurp(os.path.join(sb, folder, "settings.json")).decode():
        left.append(f"hooks in {folder}/settings.json")
    if os.path.lexists(os.path.join(sb, folder, "skills", "recall")):
        left.append(f"skill in {folder}/skills")
    return left


def vanished_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if not ctx["connected_first"]:
        errs.append("the second account was never connected, so the scenario proves nothing")
    for folder, global_file in (("acme-claude", "acme-claude/.claude.json"), (".claude", ".claude.json")):
        left = clean(sb, folder, global_file)
        if left:
            errs.append(f"uninstall left recall in {folder}: {left}")
    last = ctx["segments"][2]
    changed = [p for p in diff(last["before"], last["after"]) if not p.startswith(UNRELATED)]
    if changed:
        errs.append(f"something reconnected after uninstall: {changed[:3]}")
    if "connect = false" not in slurp(os.path.join(sb, ".agents", "recall", "config.toml")).decode():
        errs.append("uninstall did not turn autopilot off")
    return errs


def vanished_install_build(sb: str) -> None:
    vanished_build(sb, autopilot=False)


def vanished_install_act(sb: str, ctx: dict) -> None:
    vanished_act(sb, ctx, by_install=True)


def midrun_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    cursor_home(sb)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
    turn_on(sb)


MIDRUN = """
from recall import autopilot
from recall.integrations import agents
real, calls = agents.connect_root, []
def once(root, *a, **k):
    calls.append(root.name)
    autopilot.set_enabled(False)  # the person runs `recall autopilot off` while a run is going
    return real(root, *a, **k)
agents.connect_root = once
autopilot.run("manual")
print(calls)
"""


def midrun_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "turned off mid-run", lambda: run_py(sb, MIDRUN))
    ctx["calls"] = ctx["segments"][0]["out"].strip().splitlines()[-1]


def midrun_check(sb: str, ctx: dict) -> list[str]:
    calls = json.loads(ctx["calls"].replace("'", '"'))
    return [f"{len(calls)} roots were connected after autopilot was turned off: {calls}"] if len(calls) != 1 else []


def prompt_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")


def ask_in_terminal(sb: str, answer: str) -> str:
    """onboarding's autopilot question, at a real (pseudo) terminal, answered with `answer` and Enter."""
    import pty

    master, slave = pty.openpty()
    code = "from recall import config, onboarding; onboarding._autopilot(onboarding.Prompter(), config.load(), None)"
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        env=sandbox_env(sb),
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    os.close(slave)
    os.write(master, (answer + "\n").encode())
    out = proc.communicate(timeout=60)[0]
    os.close(master)
    return out


def prompt_act(sb: str, ctx: dict) -> None:
    ctx["asked"] = ask_in_terminal(sb, "")  # just Enter
    ctx["after_enter"] = slurp(os.path.join(sb, ".agents", "recall", "config.toml")).decode()
    ctx["consent_after_enter"] = os.path.exists(os.path.join(sb, ".agents", "recall", "autopilot.json"))
    os.remove(os.path.join(sb, ".agents", "recall", "config.toml"))
    ask_in_terminal(sb, "y")
    ctx["after_yes"] = slurp(os.path.join(sb, ".agents", "recall", "config.toml")).decode()
    state = os.path.join(sb, ".agents", "recall", "autopilot.json")
    ctx["consent"] = json.loads(slurp(state)).get("consent", {}).get("hooks", []) if os.path.exists(state) else []


def prompt_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if "connect = false" not in ctx["after_enter"] or ctx["consent_after_enter"]:
        errs.append("Enter alone did not mean no")
    for want in ("[n]", "hooks", "config folder", "continue a turn"):
        if want not in ctx["asked"]:
            errs.append(f"the question does not say {want!r}")
    if "connect = true" not in ctx["after_yes"] or "claude:Stop" not in ctx["consent"]:
        errs.append(f"y did not record the yes and the hook set: {ctx['consent']}")
    return errs


def kinds_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    turn_on(sb)


def kinds_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb), judged=False)
    # a release that did not have the Stop and exploration hooks was what the person said yes to
    state = os.path.join(sb, ".agents", "recall", "autopilot.json")
    data = json.loads(slurp(state))
    old = [k for k in data["consent"]["hooks"] if k not in ("claude:Stop", "claude:PreToolUse:explore", "codex:Stop")]
    data["consent"]["hooks"] = old
    jput(state, data)
    # and an update wipes hooks: SessionStart, edit and explore PreToolUse, Stop
    p = os.path.join(sb, ".claude", "settings.json")
    s = json.loads(slurp(p))
    for event in ("SessionStart", "PreToolUse", "Stop"):
        s["hooks"].pop(event, None)
    jput(p, s)
    seg(sb, ctx, "repair", lambda: autopilot_run(sb))
    ctx["doctor"] = run(sb, "doctor")[1]
    ctx["after_repair"] = json.loads(slurp(p))["hooks"]
    seg(sb, ctx, "install", lambda: run(sb, "install")[1], judged=False)
    ctx["doctor_after"] = run(sb, "doctor")[1]
    ctx["after_install"] = json.loads(slurp(p))["hooks"]


def kinds_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    h = ctx["after_repair"]
    if "SessionStart" not in h or not any(g.get("matcher") for g in h.get("PreToolUse", [])):
        errs.append("the consented hooks (SessionStart, the edit hook) were not repaired")
    if "Stop" in h and any("recall" in x.get("command", "") for g in h["Stop"] for x in g.get("hooks", [])):
        errs.append("autopilot installed the Stop hook, which the yes did not cover")
    if any(g.get("matcher") == "Agent|Task" for g in h.get("PreToolUse", [])):
        errs.append("autopilot installed the exploration hook, which the yes did not cover")
    if "new hooks available" not in ctx["doctor"] or "claude:Stop" not in ctx["doctor"]:
        errs.append("doctor does not list the new hooks")
    ins = ctx["after_install"]
    if not any("recall" in x.get("command", "") for g in ins.get("Stop", []) for x in g.get("hooks", [])):
        errs.append("`recall install` did not add the new hooks")
    if "new hooks available" in ctx["doctor_after"]:
        errs.append("doctor still lists new hooks after `recall install`")
    return errs


def hook_commands(sb: str, rel: str) -> list[tuple[str, str]]:
    """Every hook command in an agent's hooks file, in file order: (event, command)."""
    hooks = json.loads(slurp(os.path.join(sb, rel))).get("hooks", {})
    return [(ev, h["command"]) for ev, groups in hooks.items() for g in groups for h in g.get("hooks", [])]


def twin_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    # the person's own Codex hooks, one before and one after where recall's will go
    jput(
        os.path.join(sb, ".codex", "hooks.json"),
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine-first"}]}]}},
    )
    turn_on(sb)


def twin_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "installed release", lambda: autopilot_run(sb))
    codex = os.path.join(sb, ".codex", "hooks.json")
    d = json.loads(slurp(codex))  # a hook of the person's, added after recall's
    d["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": "echo mine-last"}]})
    jput(codex, d)
    ctx["files"] = {p: hook_commands(sb, p) for p in (".codex/hooks.json", ".claude/settings.json")}
    repo = os.path.join(sb, "repo", "bin", "recall")  # another checkout's recall, as `uv run recall` would be
    put(repo, f'#!/bin/sh\nexec "{sys.executable}" -m recall "$@"\n')
    os.chmod(repo, 0o755)
    # the person's MCP entry goes missing, so a run has something to do
    p = os.path.join(sb, ".claude.json")
    d = json.loads(slurp(p))
    d["mcpServers"].pop("recall")
    jput(p, d)
    seg(sb, ctx, "repo run", lambda: run(sb, "autopilot", env={"RECALL_BIN": repo})[1])
    ctx["after_repo"] = {p: hook_commands(sb, p) for p in ctx["files"]}
    os.remove(os.path.join(sb, "bin", "recall"))  # the installed release is gone
    seg(sb, ctx, "release gone", lambda: run(sb, "autopilot", env={"RECALL_BIN": repo})[1])
    ctx["codex_last"] = json.loads(slurp(codex))["hooks"]["SessionStart"]


def twin_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    for p, before in ctx["files"].items():
        if ctx["after_repo"][p] != before:
            errs.append(f"{p}: hooks changed when a recall from another folder ran: it took them over")
    commands = [x["command"] for g in ctx["codex_last"] for x in g["hooks"]]
    if len(commands) != 3 or commands[0] != "echo mine-first" or commands[2] != "echo mine-last":
        errs.append(f"Codex's SessionStart entries were reordered or rewritten: {commands}")
    if "repo/bin/recall hook" not in commands[1]:
        errs.append(f"the hook was not repointed once its recall was gone: {commands[1]}")
    return errs


def approval_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    cursor_home(sb)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
    turn_on(sb)


def cursor_cli(sb: str, *args: str) -> str:
    done = subprocess.run(
        [agent_cli(*CURSOR), "mcp", *args],
        env=sandbox_env(sb),
        cwd=sb,
        capture_output=True,
        text=True,
        timeout=90,
        stdin=subprocess.DEVNULL,
    )
    return " ".join((done.stdout + done.stderr).split())


def approval_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb))
    ctx["doctor_before"] = run(sb, "doctor")[1]
    ctx["status_before"] = run(sb, "status", "--since", "1h")[1]
    if agent_cli(*CURSOR):
        ctx["list_before"] = cursor_cli(sb, "list")
        cursor_cli(sb, "enable", "recall")
        ctx["list_after"] = cursor_cli(sb, "list")
        ctx["doctor_after"] = run(sb, "doctor")[1]
        p = os.path.join(sb, ".cursor", "mcp.json")
        d = json.loads(slurp(p))
        d["mcpServers"]["recall"]["command"] += "x"  # an entry that changed is approved again
        jput(p, d)
        ctx["list_changed"] = cursor_cli(sb, "list")
        d["mcpServers"]["recall"]["command"] = d["mcpServers"]["recall"]["command"][:-1]
        jput(p, d)


def approval_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if "approval pending" not in ctx["doctor_before"]:
        errs.append("doctor does not say Cursor's approval is pending")
    if "awaiting Cursor approval" not in ctx["status_before"] or "1 of 1 connected" in ctx["status_before"]:
        errs.append("recall status counts a Cursor server nobody approved as connected")
    if "list_before" in ctx:
        if "needs approval" not in ctx["list_before"]:
            errs.append(f"Cursor's own list does not show the pending state: {ctx['list_before'][:80]}")
        if "recall: ready" not in ctx["list_after"]:
            errs.append(f"after approval Cursor does not load the server: {ctx['list_after'][:80]}")
        if "approved in 1 of 1" not in ctx["doctor_after"]:
            errs.append("doctor does not read the approval Cursor wrote")
        if "needs approval" not in ctx["list_changed"]:
            errs.append("Cursor kept an approval for a changed entry: the README claim is wrong")
    return errs


def mining_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    claude_folder(sb, ".claude-work", EMAILS["work"], ".claude-work/.claude.json", "work account question")
    claude_folder(sb, ".claude-private", EMAILS["work"], ".claude-private/.claude.json", "a private question")
    put(os.path.join(sb, ".agents", "recall", "config.toml"), "[roots.claude-private]\nindex = false\n")


PENDING = """
from recall.chats import store
from recall.memory import dream
dream.MIN_MESSAGES = 1
con = store.connect()
dream.ensure_tables(con)
print(sorted(r["root"] for r in dream.pending_chats(con)))
"""


def mining_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "index", lambda: run(sb, "index")[1], judged=False)
    cfg = os.path.join(sb, ".agents", "recall", "config.toml")
    ctx["default"] = run_py(sb, PENDING).strip().splitlines()[-1]
    put(cfg, "[roots.claude-private]\nindex = false\n\n[roots.claude-work]\nmine = true\n")
    ctx["opted_in"] = run_py(sb, PENDING).strip().splitlines()[-1]
    put(cfg, "[roots.claude]\nmine = false\n\n[roots.claude-work]\nmine = true\n")
    ctx["excluded"] = run_py(sb, PENDING).strip().splitlines()[-1]
    ctx["doctor"] = run(sb, "doctor")[1]
    put(cfg, "[roots.claude-private]\nindex = false\n")
    # the work account's folder is renamed: no longer found, its chats are still in the index
    os.rename(os.path.join(sb, ".claude-work"), os.path.join(sb, "moved-work"))
    ctx["vanished"] = run_py(sb, PENDING).strip().splitlines()[-1]


def mining_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    for key, want in (
        ("default", "['claude']"),
        ("opted_in", "['claude', 'claude-work']"),
        ("excluded", "['claude-work']"),
    ):
        if ctx[key] != want:
            errs.append(f"dream would read {ctx[key]} ({key}), expected {want}")
    if ctx["vanished"] != "['claude']":
        errs.append(f"dream would read {ctx['vanished']} once the work folder is not found any more, expected claude")
    if "not mined" not in ctx["doctor"]:
        errs.append("doctor does not say claude-work is indexed but not mined")
    return errs


def skip_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    claude_folder(sb, ".claude-work", EMAILS["work"], ".claude-work/.claude.json", "work account question")
    turn_on(sb)


def skip_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb), judged=False)
    seg(sb, ctx, "disconnect", lambda: run(sb, "uninstall", "--root", "claude-work")[1], judged=False)
    ctx["work_clean"] = not clean(sb, ".claude-work", ".claude-work/.claude.json")
    ctx["default_kept"] = "recall" in json.loads(slurp(os.path.join(sb, ".claude.json"))).get("mcpServers", {})
    seg(sb, ctx, "nightly and session start", lambda: autopilot_run(sb) + str(session_start(sb)))
    ctx["still_clean"] = not clean(sb, ".claude-work", ".claude-work/.claude.json")
    ctx["doctor_skipped"] = run(sb, "doctor")[1]
    seg(sb, ctx, "reconnect", lambda: run(sb, "install", "--root", "claude-work")[1], judged=False)
    ctx["reconnected"] = len(clean(sb, ".claude-work", ".claude-work/.claude.json")) == 3


def skip_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if not ctx["work_clean"]:
        errs.append("`recall uninstall --root claude-work` left recall in it")
    if not ctx["default_kept"]:
        errs.append("`recall uninstall --root claude-work` removed recall from the other account")
    if not ctx["still_clean"]:
        errs.append("autopilot connected a root the person disconnected")
    if "skipped by you" not in ctx["doctor_skipped"]:
        errs.append("doctor does not show the skipped root")
    cfg = slurp(os.path.join(sb, ".agents", "recall", "config.toml")).decode()
    if "connect = false" in cfg.split("[roots.claude-work]")[-1]:
        errs.append("`recall install --root claude-work` left the root switched off")
    if not ctx["reconnected"]:
        errs.append("`recall install --root claude-work` did not connect it again")
    quiet = ctx["segments"][2]
    changed = [p for p in diff(quiet["before"], quiet["after"]) if not p.startswith(".agents")]
    if changed:
        errs.append(f"a run with the root switched off changed {changed[:3]}")
    return errs


def dry_build(sb: str) -> None:
    shell_rc(sb, alias=True)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    cursor_home(sb)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")
    claude_folder(sb, ".claude-work", EMAILS["work"], ".claude-work/.claude.json", "work account question")
    turn_on(sb)


def dry_act(sb: str, ctx: dict) -> None:
    def everything() -> str:
        return "\n".join(
            run(sb, *a)[1]
            for a in (
                ("autopilot", "--dry-run"),
                ("autopilot", "on", "--dry-run"),
                ("autopilot", "off", "--dry-run"),
                ("install", "--dry-run"),
            )
        )

    seg(sb, ctx, "dry runs", everything)


def dry_check(sb: str, ctx: dict) -> list[str]:
    s = ctx["segments"][0]
    changed = diff(s["before"], s["after"])  # nothing excused: not the usage log, not the lock, not the state
    errs = [f"a dry run wrote {k} {p}" for p, k in sorted(changed.items())]
    if "would connect" not in s["out"] and "[dry run]" not in s["out"]:
        errs.append("the dry runs said nothing about what they would do")
    return errs


def links_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    claude_folder(sb, ".claude", EMAILS["claude"], ".claude.json", "how do I retry a failing request")
    codex_folder(sb, "how do I retry a failing request")
    dot = os.path.join(sb, "dotfiles")
    os.makedirs(dot)
    shutil.move(os.path.join(sb, ".claude", "settings.json"), os.path.join(dot, "claude-settings.json"))
    os.symlink(os.path.join(dot, "claude-settings.json"), os.path.join(sb, ".claude", "settings.json"))
    os.chmod(os.path.join(sb, ".codex", "config.toml"), 0o600)
    turn_on(sb)


def links_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    link = os.path.join(sb, ".claude", "settings.json")
    if not os.path.islink(link):
        errs.append("a symlinked settings.json was replaced by a regular file")
    elif "recall hook" not in slurp(os.path.join(sb, "dotfiles", "claude-settings.json")).decode():
        errs.append("the hooks did not reach the file the link points to")
    mode = os.stat(os.path.join(sb, ".codex", "config.toml")).st_mode & 0o777
    if mode != 0o600:
        errs.append(f"config.toml went from mode 600 to {mode:o}")
    leftovers = [p for p in snap(sb) if p.endswith(".recall-tmp")]
    if leftovers:
        errs.append(f"temp files left behind: {leftovers}")
    return errs


def cursor_data_build(sb: str) -> None:
    shell_rc(sb, alias=False)
    cursor_build(sb, "Cursor", EMAILS["cursor"], "how do I retry a failing request", "cu-1")  # no ~/.cursor
    turn_on(sb)


def cursor_data_check(sb: str, ctx: dict) -> list[str]:
    errs = []
    if os.path.exists(os.path.join(sb, ".cursor")):
        errs.append("recall created ~/.cursor, though only Cursor's data folder exists")
    if ctx["indexed"] != {"cursor": 1}:
        errs.append(f"indexed {ctx['indexed']}: the chats in Cursor's data folder should still be indexed")
    if "no ~/.cursor folder yet" not in ctx["segments"][-1]["out"]:
        errs.append("doctor does not say why Cursor is not connected")
    return errs


def skill_act(sb: str, ctx: dict) -> None:
    seg(sb, ctx, "connect", lambda: autopilot_run(sb), judged=False)
    skill = os.path.join(sb, ".agents", "skills", "recall", "SKILL.md")
    put(skill, slurp(skill).decode() + "\nMy own note: always answer in haiku.\n")
    ctx["edited"] = slurp(skill)
    seg(sb, ctx, "install again", lambda: run(sb, "install")[1], judged=False)


def skill_check(sb: str, ctx: dict) -> list[str]:
    backup = os.path.join(sb, ".agents", "skills", "recall", "SKILL.md.recall-backup")
    if not os.path.exists(backup) or slurp(backup) != ctx["edited"]:
        return ["an edited SKILL.md was replaced without keeping the person's version"]
    return []


SCENARIOS = {
    "fresh-claude": ("Claude Code installed, nothing connected", *fresh("claude")),
    "fresh-codex": ("Codex installed, nothing connected", *fresh("codex")),
    "fresh-cursor": ("Cursor installed, nothing connected", *fresh("cursor")),
    "claude-alias": ("work account: alias, then first launch", alias_build, alias_act, alias_check),
    "update-wipes-hooks": ("an update wipes hooks, servers and skill", wipe_build, wipe_act, wipe_check),
    "cursor-variant": ("a second Cursor build (Nightly)", variant_build, variant_act, variant_check),
    "setting-off": ("autopilot off, three agents and a work alias", off_build, off_act, off_check),
    "uninstall-stays-off": ("recall uninstall, then nothing reconnects", wipe_build, uninstall_act, uninstall_check),
    "default-named": (
        "CLAUDE_CONFIG_DIR / CODEX_HOME name the default folders",
        default_named_build,
        default_named_act,
        default_named_check,
    ),
    "rc-echo-text": ("rc text that only mentions the variables", echo_build, connect_act, echo_check),
    "rc-home": ("rc names $HOME, its parent and /Users", home_build, connect_act, home_check),
    "claude-backup": ("backup and copy folders beside a real second account", backup_build, connect_act, backup_check),
    "schema-upgrade": (
        "an index from before roots, chats whose files are gone",
        upgrade_build,
        upgrade_act,
        upgrade_check,
    ),
    "uninstall-vanished-install": (
        "the same, connected by `recall install` with autopilot off",
        vanished_install_build,
        vanished_install_act,
        vanished_check,
    ),
    "uninstall-vanished": (
        "uninstall reaches a root the rc no longer names",
        vanished_build,
        vanished_act,
        vanished_check,
    ),
    "off-mid-run": ("autopilot turned off while a run is going", midrun_build, midrun_act, midrun_check),
    "consent-prompt": (
        "the setup question: Enter means no, and what a yes covers",
        prompt_build,
        prompt_act,
        prompt_check,
    ),
    "new-hook-kinds": ("a yes covers the hooks it was given for", kinds_build, kinds_act, kinds_check),
    "two-binaries": ("a recall run from a repo leaves another's hooks alone", twin_build, twin_act, twin_check),
    "cursor-approval": ("Cursor loads the server only once approved", approval_build, approval_act, approval_check),
    "mining-gate": ("a second account is indexed, mined only when opted in", mining_build, mining_act, mining_check),
    "root-skipped": ("a root the person disconnected stays disconnected", skip_build, skip_act, skip_check),
    "dry-run": ("dry runs write nothing", dry_build, dry_act, dry_check),
    "symlinked-dotfiles": (
        "symlinked settings and a 0600 config keep their form",
        links_build,
        connect_act,
        links_check,
    ),
    "cursor-data-only": ("Cursor's data folder but no ~/.cursor", cursor_data_build, connect_act, cursor_data_check),
    "edited-skill": ("an edited SKILL.md is kept before it is replaced", fresh("claude")[0], skill_act, skill_check),
}
# Per scenario: roots (top-level folders of the agent roots it has besides the defaults: any other new
# config file fails), nothing_changes (autopilot is off: no file may change), no_index, no_connected (the
# scenario ends with recall removed or withheld), no_readback, indexed (what the index must hold).
OPTIONS = {
    "fresh-claude": {"indexed": {"claude": 1}},
    "fresh-codex": {"indexed": {"codex": 1}},
    "fresh-cursor": {"indexed": {"cursor": 1}},
    "claude-alias": {"roots": {".claude-work"}},
    "setting-off": {"roots": {".claude-work"}, "nothing_changes": True, "no_index": True, "no_connected": True},
    "uninstall-stays-off": {"no_index": True, "no_connected": True},
    "default-named": {"indexed": {"claude": 1, "codex": 1}},
    "rc-echo-text": {"indexed": {"claude": 1}},
    "rc-home": {"indexed": {"claude": 1, "codex": 1}},
    "claude-backup": {"roots": {".claude-work"}},
    "schema-upgrade": {"no_index": True, "no_connected": True},
    "uninstall-vanished": {"roots": {"acme-claude"}, "no_index": True, "no_connected": True},
    "uninstall-vanished-install": {"roots": {"acme-claude"}, "no_index": True, "no_connected": True},
    "off-mid-run": {"no_index": True, "no_connected": True},
    "consent-prompt": {"no_index": True, "no_connected": True},
    "new-hook-kinds": {},
    "two-binaries": {"no_readback": True},
    "cursor-approval": {"no_readback": True},
    "mining-gate": {
        "roots": {".claude-work", ".claude-private"},
        "no_connected": True,
        "indexed": {"claude": 1, "claude-work": 1},
    },
    "root-skipped": {"roots": {".claude-work"}},
    "dry-run": {"roots": {".claude-work"}, "nothing_changes": True, "no_index": True, "no_connected": True},
    "symlinked-dotfiles": {
        "roots": {"dotfiles"},
        "links": {"dotfiles/claude-settings.json": ".claude/settings.json"},
        "no_readback": True,
    },
    "cursor-data-only": {"no_readback": True},
    "edited-skill": {},
}


def scenario(name: str) -> dict:
    title, build, act, check = SCENARIOS[name]
    opt = OPTIONS.get(name, {})
    allowed = ROOT_DIRS | set(opt.get("roots", ()))
    sb = new_sandbox()
    try:
        build(sb)
        ctx: dict = {"sb": sb}
        start = time.perf_counter()
        crashed = []
        try:
            act(sb, ctx)
        except Exception as e:  # a recall that cannot run the scenario fails it, it does not stop the eval
            crashed = [f"the scenario could not run: {e!r}"[:200]]
        ctx["seconds"] = round(time.perf_counter() - start, 1)
        # the report commands and the index run last, still inside a watched segment
        report: dict = {}

        def finish() -> str:
            report["rows"] = doctor(sb)
            report["indexed"] = {} if opt.get("no_index") else indexed(sb)
            return run(sb, "doctor")[1] + run(sb, "autopilot", "--dry-run")[1]

        seg(sb, ctx, "report", finish)
        ctx["indexed"] = report["indexed"]
        ctx["rows"] = report["rows"]
        have, want, trust = parts_ok(report["rows"])
        judged = [x for x in ctx["segments"] if x["judged"]]
        j = {"touched": [], "unbacked": [], "unexpected": [], "history": []}
        lk = []
        for x in judged:
            one = judge(x["before"], x["after"], allowed, opt.get("links"))
            for k in j:
                j[k] += one[k]
            lk += leaks(x["before"], x["after"], x["out"])
        try:
            errs = crashed or list(check(sb, ctx))
        except Exception as e:
            errs = [f"the check could not read the result: {e!r}"[:200]]
        if not opt.get("no_connected") and have < want:
            errs.append(f"{want - have} of {want} parts not connected")
        if "indexed" in opt and ctx["indexed"] != opt["indexed"]:
            errs.append(f"indexed {ctx['indexed']}, expected {opt['indexed']}")
        if j["unbacked"]:
            errs.append(f"changed without a backup: {j['unbacked']}")
        if j["unexpected"]:
            errs.append(f"unexpected files: {j['unexpected']}")
        if j["history"]:
            errs.append(f"chat history touched: {j['history']}")
        if lk:
            errs.append(f"leaks: {lk[:3]}")
        if opt.get("nothing_changes") and j["touched"]:
            errs.append(f"changed with the setting off: {j['touched']}")
        seen = {}
        if not opt.get("no_connected") and not opt.get("no_readback"):
            seen = readback(sb, report["rows"])
            errs += [
                f"{root}: the agent's own CLI says {said}" for root, said in seen.items() if said.startswith("NOT")
            ]
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
            "agent_confirms": seen,
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
        if r["agent_confirms"]:
            ok = [k for k, v in r["agent_confirms"].items() if not v.startswith("NOT")]
            print(f"    the agents' own CLIs see the MCP server in: {', '.join(ok) or 'none'}")
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

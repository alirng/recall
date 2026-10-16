"""The nightly memory run, through the OS scheduler: launchd on macOS, systemd on Linux.

A run missed while the machine sleeps fires when it wakes. Each run is capped in
time and model calls, so a long backfill spreads over several nights, newest first.
A run that starts on battery waits for the next one on power (power.py).
"""

import os
import plistlib
import shutil
import subprocess
import sys

from .. import paths

LABEL = "dev.recall.dream"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
SYSTEMD = os.path.expanduser("~/.config/systemd/user")
LOG = os.path.join(paths.LOGS, "dream.log")


def enable(hour: int, minute: int, max_minutes: int, max_calls: int) -> str:
    os.makedirs(paths.LOGS, exist_ok=True)
    args = [_recall_bin(), "dream", "--max-minutes", str(max_minutes), "--max-calls", str(max_calls)]
    if sys.platform == "darwin":
        return _launchd(args, hour, minute)
    if sys.platform.startswith("linux"):
        return _systemd(args, hour, minute)
    return "automatic scheduling is not supported on this OS yet; schedule `recall dream` yourself"


def disable() -> str:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", PLIST], capture_output=True)
        if os.path.exists(PLIST):
            os.remove(PLIST)
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "recall-dream.timer"], capture_output=True)
        for unit in ("recall-dream.timer", "recall-dream.service"):
            if os.path.exists(os.path.join(SYSTEMD, unit)):
                os.remove(os.path.join(SYSTEMD, unit))
    return "nightly run removed"


def status() -> str:
    if sys.platform == "darwin":
        if not os.path.exists(PLIST):
            return "not scheduled"
        with open(PLIST, "rb") as f:
            at = plistlib.load(f)["StartCalendarInterval"]
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        state = "loaded" if loaded.returncode == 0 else "NOT loaded"
        return f"nightly at {at['Hour']:02d}:{at['Minute']:02d} ({state}); log: {LOG}"
    if sys.platform.startswith("linux"):
        return "see `systemctl --user list-timers recall-dream.timer`"
    return "not scheduled"


def _launchd(args: list[str], hour: int, minute: int) -> str:
    plist = {
        "Label": LABEL,
        "ProgramArguments": args,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": LOG,
        "StandardErrorPath": LOG,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        # launchd starts jobs with a bare PATH; agent CLIs and local model servers live here.
        "EnvironmentVariables": {
            "PATH": f"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:{os.path.expanduser('~/.local/bin')}"
        },
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, PLIST], capture_output=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", domain, PLIST], capture_output=True, text=True)
    if r.returncode != 0:
        return f"wrote {PLIST} but launchctl refused it: {r.stderr.strip()}"
    return f"nightly at {hour:02d}:{minute:02d} via launchd; log: {LOG}"


def _systemd(args: list[str], hour: int, minute: int) -> str:
    from .. import config

    # On battery systemd skips the run (recall dream also checks, on every OS); "run" lets it go.
    power = "" if config.load()["power"].get("on_battery") == "run" else "ConditionACPower=true\n"
    os.makedirs(SYSTEMD, exist_ok=True)
    with open(os.path.join(SYSTEMD, "recall-dream.service"), "w") as f:
        f.write(
            f"[Unit]\nDescription=recall memory run\n{power}\n[Service]\nType=oneshot\n"
            f"ExecStart={' '.join(args)}\nNice=10\n"
        )
    with open(os.path.join(SYSTEMD, "recall-dream.timer"), "w") as f:
        f.write(
            f"[Unit]\nDescription=nightly recall memory run\n\n[Timer]\nOnCalendar=*-*-* {hour:02d}:{minute:02d}:00\n"
            "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
        )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "recall-dream.timer"], capture_output=True)
    return f"nightly at {hour:02d}:{minute:02d} via a systemd user timer"


def _recall_bin() -> str:
    found = shutil.which("recall")
    if not found:
        sys.exit("recall is not on PATH: install it with `uv tool install .` first")
    return os.path.realpath(found)

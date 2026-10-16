"""Whether this machine runs on battery, so recall's background work can wait for power.

On battery the nightly memory run defers itself, and a tool call starts no background job (the
`recall index` that reads Cursor's database on a first index, the `recall embed` that works off
a backlog). `[power] on_battery = "run"` in config.toml lets them run anyway. When the OS does
not say, the machine counts as plugged in.
"""

import contextlib
import glob
import json
import os
import subprocess
import sys
import time

from . import config, paths

CACHE_SECONDS = 60  # a server runs for hours: it asks the OS again after this
NICE = 10  # a background job's priority: below the agents and the person's own work
DEFERRED = os.path.join(paths.HOME, "deferred.json")  # job -> when it last waited for power

_cached: tuple[float, bool] | None = None


def on_battery() -> bool:
    """True when the OS says the machine draws from its battery."""
    global _cached
    if _cached is None or time.monotonic() - _cached[0] > CACHE_SECONDS:
        _cached = (time.monotonic(), _darwin() if sys.platform == "darwin" else _linux())
    return _cached[1]


def defer(setting: str | None = None) -> bool:
    """Whether background work waits now: on battery, unless `setting` (a command's flag) or
    [power] on_battery is "run"."""
    return (setting or config.load()["power"].get("on_battery")) != "run" and on_battery()


def lower_priority() -> None:
    """For a background job: let everything else have the CPU first."""
    with contextlib.suppress(OSError):
        os.nice(NICE)


def _darwin() -> bool:
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "'Battery Power'" in out  # first line: Now drawing from 'AC Power' (or 'Battery Power')


def _linux() -> bool:
    """Plugged in when a mains or USB supply is online; otherwise on battery only while a
    battery of the machine itself (not a mouse's) is discharging."""
    discharging = False
    for supply in glob.glob("/sys/class/power_supply/*"):
        if _read(supply, "type") == "Battery":
            discharging = discharging or (
                _read(supply, "scope") != "Device" and _read(supply, "status") == "Discharging"
            )
        elif _read(supply, "online") == "1":
            return False
    return discharging


def _read(supply: str, name: str) -> str | None:
    try:
        with open(os.path.join(supply, name)) as f:
            return f.read().strip()
    except OSError:
        return None


# --- what waited, for `recall status` and `recall doctor` ---------------------------


def deferred() -> dict[str, float]:
    """Jobs that last waited for power and have not run since: name -> when."""
    try:
        with open(DEFERRED) as f:
            found = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, int | float)} if isinstance(found, dict) else {}


def note(job: str, waited: bool) -> None:
    """Record that a job waited for power, or that it ran (which clears the record)."""
    found = deferred()
    if waited == (job in found):
        return
    if waited:
        found[job] = time.time()
    else:
        found.pop(job)
    with contextlib.suppress(OSError):
        os.makedirs(paths.HOME, exist_ok=True)
        with open(DEFERRED, "w") as f:
            json.dump(found, f)


def summary() -> list[str]:
    """What waits for power now, in a line each; empty when nothing does."""
    lines = []
    if at := deferred().get("dream"):
        when = time.strftime("%b %d %H:%M", time.localtime(at))
        lines.append(
            f"Nightly memory run deferred: on battery ({when}). It runs at its next time on power,"
            " or now with `recall dream --on-battery run`."
        )
    if defer():
        lines.append(
            'On battery: background indexing and embedding wait for power ([power] on_battery = "run" lets them run).'
        )
    return lines

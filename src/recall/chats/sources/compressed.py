"""Chat files that an agent has compressed with zstd.

Codex compresses rollout files it has not touched for seven days: `rollout-x.jsonl` becomes
`rollout-x.jsonl.zst`, with the same modified time, and resuming the thread turns it back into
plain JSONL. So one chat is one *stable path* (the `.jsonl` one) that is, at any moment, either
that file or its `.zst` twin. Everything here takes the stable path, tries the plain file first
(Codex's own rule) and falls back to the twin, so a chat keeps its identity across both forms.

Decoders, in order: Python 3.14's `compression.zstd`, the `zstandard` package (the `zstd`
extra), then the `zstd` command. Nothing is ever written next to the original.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
from functools import cache

SUFFIX = ".zst"
MAGIC = b"\x28\xb5\x2f\xfd"
# Hooks and scheduled jobs run with a minimal PATH, so look where Homebrew and distros put it.
COMMAND_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def plain_path(path: str) -> str:
    return path[: -len(SUFFIX)] if path.endswith(SUFFIX) else path


def resolve(path: str) -> str | None:
    """The file that holds this chat now: the plain one, else its .zst twin, else None."""
    plain = plain_path(path)
    for candidate in (plain, plain + SUFFIX):
        if os.path.isfile(candidate):
            return candidate
    return None


@cache
def command() -> str | None:
    found = shutil.which("zstd")
    if found:
        return found
    return next((p for d in COMMAND_DIRS if os.access(p := os.path.join(d, "zstd"), os.X_OK)), None)


@cache
def backend() -> str | None:
    """How a .zst file can be read here: "python", "zstandard", "zstd command", or None."""
    try:
        from compression import zstd  # noqa: F401  (Python 3.14+)

        return "python"
    except ImportError:
        pass
    try:
        import zstandard  # noqa: F401

        return "zstandard"
    except ImportError:
        pass
    return "zstd command" if command() else None


def fix_hint() -> str:
    v = sys.version_info
    return (
        "to read them, install the zstd command (brew install zstd), or the Python package "
        f"(pip install 'recall-agents[zstd]'), or run recall on Python 3.14+ (this is {v.major}.{v.minor})"
    )


def content_size(path: str) -> int | None:
    """The uncompressed size a .zst file's first frame header declares, or None. Codex records
    it, so this is the plain file's size without decoding anything."""
    try:
        with open(path, "rb") as f:
            h = f.read(18)
    except OSError:
        return None
    if len(h) < 6 or h[:4] != MAGIC:
        return None
    flags = h[4]
    single = flags >> 5 & 1
    at = 5 + (0 if single else 1) + (0, 1, 2, 4)[flags & 3]
    n = (1 if single else 0, 2, 4, 8)[flags >> 6]
    if n == 0 or len(h) < at + n:
        return None
    return int.from_bytes(h[at : at + n], "little") + (256 if flags >> 6 == 1 else 0)


def fingerprint(path: str) -> str | None:
    """ "<mtime_ns>:<size>" of a chat file. A .zst twin gives what the plain file gave (same
    modified time, declared uncompressed size), so compressing a chat does not make it look
    changed. None when neither form exists."""
    plain = plain_path(path)
    for candidate in (plain, plain + SUFFIX, plain):  # a file moving between forms is found by one of these
        try:
            st = os.stat(candidate)
        except FileNotFoundError:
            continue
        size = content_size(candidate) if candidate.endswith(SUFFIX) else None
        return f"{st.st_mtime_ns}:{st.st_size if size is None else size}"
    return None


def size(path: str) -> int | None:
    """A chat file's size as plain text, whichever form it is in."""
    found = resolve(path)
    if found is None:
        return None
    if not found.endswith(SUFFIX):
        return os.path.getsize(found)
    declared = content_size(found)
    if declared is not None:
        return declared
    with open_text(found) as f:
        return sum(len(line.encode("utf-8", "replace")) for line in f)


@contextlib.contextmanager
def open_text(path: str):
    """The chat file as text lines. Takes the stable path or the .zst one."""
    plain = plain_path(path)
    first = None
    with contextlib.ExitStack() as stack:
        for candidate in (plain, plain + SUFFIX, plain):  # a file moving between forms is found by one of these
            try:
                f = stack.enter_context(_open(candidate))
                break
            except FileNotFoundError as e:
                first = first or e
        else:
            raise first
        yield f


def _open(path: str):
    if not path.endswith(SUFFIX):
        return open(path, encoding="utf-8", errors="replace")
    use = backend()
    if use == "python":
        from compression import zstd

        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    if use == "zstandard":
        return _zstandard(path)
    if use == "zstd command":
        return _command(path)
    raise OSError(f"{path} is zstd-compressed and no decoder is installed: {fix_hint()}")


@contextlib.contextmanager
def _zstandard(path: str):
    import zstandard

    with open(path, "rb") as raw:
        reader = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)
        with io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8", errors="replace") as text:
            yield text


@contextlib.contextmanager
def _command(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    proc = subprocess.Popen([command(), "-dc", "--", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
        while proc.stdout.read(1 << 20):  # a reader that stopped early must not make zstd's exit look like a failure
            pass
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        raise OSError(f"zstd could not decode {path}")

"""Chat files that an agent has compressed with zstd.

Codex compresses rollout files it has not touched for seven days: `rollout-x.jsonl` becomes
`rollout-x.jsonl.zst`, with the same modified time, and resuming the thread turns it back into
plain JSONL. So one chat is one *stable path* (the `.jsonl` one) that is, at any moment, either
that file or its `.zst` twin. Everything here takes the stable path, tries the plain file first
(Codex's own rule) and falls back to the twin, so a chat keeps its identity across both forms.

Decoders, in order: Python 3.14's `compression.zstd`, the `zstandard` package (the `zstd`
extra), then the `zstd` command. Nothing is ever written next to the original.

A file that cannot be read raises OSError whichever decoder is in use (a damaged or cut-off
file, a `zstd` that fails or stops writing), so callers that already catch OSError for a
missing file need nothing more.
"""

import contextlib
import io
import os
import select
import shutil
import subprocess
import sys
from functools import cache

CHAT_SCHEMA = "2026-10-15 parts replace schema 7"

SUFFIX = ".zst"
MAGIC = b"\x28\xb5\x2f\xfd"
# Hooks and scheduled jobs run with a minimal PATH, so look where Homebrew and distros put it.
COMMAND_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
# A `zstd` that writes nothing for this long (or does not exit after its output ends) is hung.
# Only its silence counts, not the time a caller spends parsing what it already wrote.
STALL_SECONDS = 60


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
    changed. None when neither form exists or can be stat-ed."""
    plain = plain_path(path)
    for candidate in (plain, plain + SUFFIX, plain):  # a file moving between forms is found by one of these
        try:
            st = os.stat(candidate)
        except OSError:  # gone, or not readable by us: the next form may be there
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
    with _binary(found) as raw:  # no size in the header: count the bytes
        return sum(len(chunk) for chunk in iter(lambda: raw.read(1 << 20), b""))


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
    return _text(path)


@contextlib.contextmanager
def _text(path: str):
    with _binary(path) as raw:
        buffered = io.BufferedReader(raw, 1 << 20)
        with io.TextIOWrapper(buffered, encoding="utf-8", errors="replace") as text:
            yield text


class _Checked(io.RawIOBase):
    """The decoded bytes of a .zst file. Each decoder fails differently on a damaged file (the
    standard library raises EOFError or its own ZstdError, zstandard its own, the command exits
    non-zero) and zstandard reads a cut-off one without complaint; here every failure is an
    OSError, and a stream that ends before the size its header declares is one too."""

    def __init__(self, source, path: str):
        self.source, self.path, self.read_so_far = source, path, 0
        self.declared = content_size(path)

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        try:
            data = self.source.read(len(b))
        except OSError:
            raise
        except Exception as e:
            raise OSError(f"zstd could not decode {self.path}: {e}") from e
        n = len(data)
        b[:n] = data
        self.read_so_far += n
        if n == 0 and self.read_so_far < (1 if self.declared is None else self.declared):
            raise OSError(f"{self.path} ends early or is empty ({self.read_so_far} bytes decoded)")
        return n


@contextlib.contextmanager
def _binary(path: str):
    """The decoded bytes of a .zst file, as a binary stream."""
    with _decode(path) as source:
        yield _Checked(source, path)


@contextlib.contextmanager
def _decode(path: str):
    use = backend()
    if use == "python":
        from compression import zstd

        with zstd.open(path, "rb") as f:
            yield f
    elif use == "zstandard":
        import zstandard

        with open(path, "rb") as raw:
            yield zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)
    elif use == "zstd command":
        with _command(path) as pipe:
            yield pipe
    else:
        raise OSError(f"{path} is zstd-compressed and no decoder is installed: {fix_hint()}")


class _Pipe(io.RawIOBase):
    """The output of a `zstd` process, which gives up on one that has gone quiet."""

    def __init__(self, out):
        self.out = out
        self.poll = select.poll()
        self.poll.register(out, select.POLLIN)

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if not self.poll.poll(STALL_SECONDS * 1000):
            raise OSError(f"zstd wrote nothing for {STALL_SECONDS} s")
        data = self.out.read(len(b)) or b""
        b[: len(data)] = data
        return len(data)


@contextlib.contextmanager
def _command(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    # No shell, the path after `--`, nothing on stdin, and stderr to the bin so it cannot fill a pipe.
    proc = subprocess.Popen(
        [command(), "-dc", "--", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        pipe = _Pipe(proc.stdout)
        yield pipe
        while pipe.read(1 << 20):  # a reader that stopped early must not make zstd's exit look like a failure
            pass
        code = proc.wait(timeout=STALL_SECONDS)
    except BaseException as e:
        proc.kill()
        proc.wait()
        if isinstance(e, subprocess.TimeoutExpired):
            raise OSError(f"zstd did not exit after decoding {path}") from e
        raise
    finally:
        proc.stdout.close()
    if code != 0:
        raise OSError(f"zstd could not decode {path}")

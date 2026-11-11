"""Reading an account export for `recall import`: the ZIP, its folder or a single conversations file.

Only conversations.json and its numbered shards (conversations-000.json) are opened. An export's other files
hold account details such as an email address or phone number, and shared_conversations.json only stubs. The
ZIP is read in place, never extracted. Which service an export came from shows in its conversations: ChatGPT's
each have a `mapping` tree, claude.ai's a `chat_messages` list.
"""

import json
import os
import re
import zipfile
from collections.abc import Iterator

from . import Item, chatgpt, claudeai

_FILE = re.compile(r"conversations(-\d+)?\.json")
# Far beyond a real account's export (big ones run to hundreds of MB), and short of what json.load
# can hold in memory in the CLI or the long-lived `recall api`.
MAX_BYTES = 2_000_000_000


class BadExport(ValueError):
    """The path is not an export recall can read; the message says why, and nothing was indexed."""


def _fits(path: str, size: int) -> None:
    if size > MAX_BYTES:
        raise BadExport(f"{path} holds {size // 1_000_000:,} MB of conversations, more than recall reads at once")


def _from_zip(path: str) -> list:
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if _FILE.fullmatch(os.path.basename(n)))
        if not names:
            raise BadExport(f"{path} has no conversations.json")
        _fits(path, sum(z.getinfo(n).file_size for n in names))
        out = []
        for name in names:
            with z.open(name) as f:
                out.append(json.load(f))
    return out


def _from_folder(path: str) -> list:
    names = sorted(n for n in os.listdir(path) if _FILE.fullmatch(n))
    if not names:
        raise BadExport(f"{path} has no conversations.json")
    _fits(path, sum(os.path.getsize(os.path.join(path, n)) for n in names))
    out = []
    for name in names:
        with open(os.path.join(path, name), "rb") as f:
            out.append(json.load(f))
    return out


def _parts(path: str) -> list:
    """Each conversations file's parsed JSON."""
    try:
        if os.path.isdir(path):
            return _from_folder(path)
        if zipfile.is_zipfile(path):
            return _from_zip(path)
        _fits(path, os.path.getsize(path))
        with open(path, "rb") as f:
            return [json.load(f)]
    except BadExport:
        raise
    except OSError as e:
        raise BadExport(f"could not read {path}: {e.strerror or type(e).__name__}") from None
    except (ValueError, zipfile.BadZipFile, RecursionError, MemoryError):
        raise BadExport(f"{path} is not a claude.ai or ChatGPT export") from None


def load(path: str) -> tuple[str, list[dict], Iterator[Item]]:
    """The service's label, the export's conversations and an Item for each."""
    found = []
    for part in _parts(path):
        chats = part.get("conversations") if isinstance(part, dict) else part
        found += [c for c in chats if isinstance(c, dict)] if isinstance(chats, list) else []
    module = chatgpt if any("mapping" in c for c in found) else claudeai
    usable = module.usable(found)
    if not usable:
        raise BadExport(f"{path} holds no claude.ai or ChatGPT conversations")
    return module.LABEL, usable, module.export_items(usable)


def run(path: str) -> dict:
    """Index an export, for `recall import` and the API. Raises BadExport, having indexed nothing, when
    the path is not one."""
    from .. import indexer, store

    service, chats, items = load(os.path.expanduser(path))
    con = store.connect()
    try:
        with indexer.exclusive("an index run is in progress: waiting for it to finish"):
            return {"service": service, "chats": len(chats), **indexer.take(con, items)}
    finally:
        con.close()

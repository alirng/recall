"""Prompts live as markdown files so they read, and diff, like documents.

The `<!-- version: ... -->` line at the top of each file is recorded with every
result the prompt produces: bump it when a change should re-run past work.
"""

import re
from importlib import resources

_VERSION = re.compile(r"<!-- version: (\S+) -->\n")


def load(name: str) -> tuple[str, str]:
    """Returns (prompt text, version tag such as "extract-v3")."""
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    m = _VERSION.match(text)
    if not m:
        raise ValueError(f"prompt {name}.md has no version line")
    return text[m.end() :].strip(), f"{name}-{m.group(1)}"

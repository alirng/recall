"""Text recall puts into an agent's context: notes, rules, past chats and exploration reports.

Every injected block is wrapped in <recall-memory> tags, which chat parsers strip so memory
never learns from itself. Text inside a block must not open or close one, however it is
nested: removing tags one layer at a time leaves `<</x>/recall-memory>` as a closing tag. So
nothing injected keeps a `<` or `>`: each becomes a single angle quote, which reads the same
and parses as nothing. Imports nothing heavy: hooks use it.
"""


def defang(text: str) -> str:
    """text with every < and > turned into ‹ and ›, so no tag can form in it."""
    return text.replace("<", "‹").replace(">", "›")


def line(text, cap: int | None = None) -> str:
    """text as one injected line: defanged, its whitespace collapsed, and cut to `cap`
    characters with an ellipsis."""
    out = " ".join(defang(str(text or "")).split())
    return out if cap is None or len(out) <= cap else out[: cap - 1].rstrip() + "…"

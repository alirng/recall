"""Reach: whether a working preference holds wherever the developer works, or only in the
context it was learned in (see contexts.py). Where a note was seen tells which context; only
what it says tells whether it travels, so the model judges each global note once and the
answer is kept in the note's frontmatter, where it can be corrected by hand."""

from ..llm import Client
from . import prompts
from .notes import Note, NoteStore

SYSTEM, VERSION = prompts.load("reach")
BATCH = 25
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes"],
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["note_id", "reach", "reason"],
                "properties": {
                    "note_id": {"type": "string"},
                    "reach": {"enum": ["everywhere", "context"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


def run(client: Client, store: NoteStore, notes: dict[str, Note]) -> int:
    """Judge every active global note that has no reach yet. Returns how many were judged."""
    pending = [n for n in notes.values() if n.status == "active" and n.scope == "global" and not n.reach]
    judged = 0
    for i in range(0, len(pending), BATCH):
        batch = {n.id: n for n in pending[i : i + BATCH]}
        prompt = "\n\n".join(f"note_id: {n.id}\ntitle: {n.title}\n{n.body}" for n in batch.values())
        for answer in client.json("reach", SYSTEM, prompt, SCHEMA)["notes"]:
            note = batch.get(answer["note_id"])
            if note and not note.reach:
                note.reach = answer["reach"]
                store.write(note)
                judged += 1
    return judged

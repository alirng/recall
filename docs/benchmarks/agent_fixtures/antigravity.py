"""Antigravity: a plaintext transcript as `agy` 1.x writes it (SpecStory's ANTIGRAVITY-FORMAT.md, Apache-2.0), and a
conversation from before transcripts existed, which has only the agent's Markdown artifacts."""

import json
import os

from . import Fixture

ROOT = ".gemini/antigravity-cli"
NEW = "aaaa1111-0000-4000-8000-0000000000a1"
OLD = "bbbb2222-0000-4000-8000-0000000000b2"


def _step(i: int, kind: str, source: str, **more) -> dict:
    return {
        "step_index": i,
        "source": source,
        "type": kind,
        "status": "DONE",
        "created_at": f"2026-10-02T10:00:{i:02d}Z",
        **more,
    }


def build(home: str) -> None:
    brain = os.path.join(home, ROOT, "brain", NEW, ".system_generated", "logs")
    os.makedirs(brain)
    steps = [
        _step(
            0,
            "USER_INPUT",
            "USER_EXPLICIT",
            content="<USER_REQUEST>\nwhy is the quokkaflux-antigravity build slow?\n</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\nThe current local time is: 2026-10-02T06:00:00-04:00.\n</ADDITIONAL_METADATA>",
        ),
        _step(1, "CONVERSATION_HISTORY", "SYSTEM"),
        _step(
            2,
            "PLANNER_RESPONSE",
            "MODEL",
            content="Let me run the build.",
            thinking="INJECTEDNOISE",
            tool_calls=[{"name": "run_command", "args": {"CommandLine": "make", "Cwd": "/work/agy"}}],
        ),
        _step(
            3,
            "RUN_COMMAND",
            "MODEL",
            content="The command failed with exit code: 2\nOutput:\nmake: *** No rule to make target",
        ),
        _step(5, "PLANNER_RESPONSE", "MODEL", content="The Makefile has no default target."),
    ]
    with open(os.path.join(brain, "transcript_full.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(s) for s in steps) + "\n")
    with open(os.path.join(home, ROOT, "history.jsonl"), "w") as f:
        f.write(json.dumps({"display": "x", "timestamp": 1, "workspace": "/work/agy", "conversationId": NEW}) + "\n")
    old = os.path.join(home, ".gemini", "antigravity", "brain", OLD)
    os.makedirs(old)
    with open(os.path.join(old, "implementation_plan.md"), "w") as f:
        f.write("# Plan\n\nSplit the settings page into tabs.\n")
    with open(os.path.join(old, "implementation_plan.md.metadata.json"), "w") as f:
        json.dump(
            {
                "artifactType": "ARTIFACT_TYPE_IMPLEMENTATION_PLAN",
                "summary": "Tabbed settings page",
                "updatedAt": "2026-03-04T09:00:00Z",
                "version": "1",
            },
            f,
        )


FIXTURES = {"antigravity": Fixture(build, "quokkaflux-antigravity", "/work/agy")}

"""Claude Cowork: a `local_<id>.json` sidecar per session and, in the folder beside it, the transcript in Claude
Code's format plus an `audit.jsonl` of the same session. The first chat's audit log, sidecar system prompt and
a background session under `agent/` hold a word that must stay out of the index. Every value is made up."""

import json
import os

from . import Fixture

SUPPORT = "Library/Application Support"  # macOS; the eval runs on a Mac, where roots.expand picks this
PROJECT = "/Users/someone/code/quokkaflux-cowork"
UPLOAD = "<uploaded_files><file><file_path>/sessions/x/INJECTEDNOISE.pdf</file_path></file></uploaded_files>"


def _lines(sid: str, cwd: str, turns: list[tuple[str, str]]) -> list[dict]:
    return [
        {
            "type": kind,
            "sessionId": sid,
            "cwd": cwd,
            "entrypoint": "local-agent",
            "userType": "external",
            "timestamp": f"2026-10-20T09:00:{n:02d}Z",
            "message": {"role": kind, "content": [{"type": "text", "text": text}]},
        }
        for n, (kind, text) in enumerate(turns)
    ]


def _put(org: str, name: str, sid: str, meta: dict, turns: list[tuple[str, str]]) -> None:
    os.makedirs(org, exist_ok=True)
    with open(os.path.join(org, f"local_{name}.json"), "w") as f:
        json.dump({"sessionId": f"local_{name}", "cliSessionId": sid, "createdAt": 1_792_486_800_000, **meta}, f)
    folder = os.path.join(org, f"local_{name}")
    transcript = os.path.join(folder, ".claude", "projects", "-sessions-calm-test-turing")
    os.makedirs(transcript, exist_ok=True)
    lines = _lines(sid, "/sessions/calm-test-turing", turns)
    with open(os.path.join(transcript, f"{sid}.jsonl"), "w") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))
    with open(os.path.join(folder, "audit.jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "INJECTEDNOISE audit"}}) + "\n")


def build(home: str) -> None:
    org = os.path.join(home, SUPPORT, "Claude", "local-agent-mode-sessions", "account-1", "org-1")
    _put(
        org,
        "aaaaaaaa-0000-4000-8000-000000000001",
        "aaaaaaaa-1111-4111-8111-000000000001",
        {"title": "Sort the quarter's invoices", "userSelectedFolders": [PROJECT], "systemPrompt": "INJECTEDNOISE"},
        [
            ("user", f"{UPLOAD}\nsort the quokkaflux-cowork invoices by quarter"),
            ("assistant", "Sorted them into four folders."),
        ],
    )
    _put(
        org,
        "aaaaaaaa-0000-4000-8000-000000000002",
        "aaaaaaaa-1111-4111-8111-000000000002",
        {"title": "Draft a short note", "userSelectedFolders": []},
        [("user", "draft a short note to the team"), ("assistant", "Here is a draft.")],
    )
    _put(
        os.path.join(org, "agent"),
        "ditto_aaaaaaaa",
        "aaaaaaaa-1111-4111-8111-000000000003",
        {},
        [("user", "INJECTEDNOISE background run"), ("assistant", "INJECTEDNOISE done")],
    )


FIXTURES = {"cowork": Fixture(build, "quokkaflux-cowork", project=PROJECT)}

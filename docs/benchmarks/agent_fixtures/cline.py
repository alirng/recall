"""Cline's SDK session, and the task folders Roo Code and Kilo Code's extensions write into VS Code's
globalStorage. Shapes: deja-vu docs/registry/{cline,roo}.md (MIT) and Cline's own source; every value is made up."""

import json
import os

from . import Fixture

SUPPORT = "Library/Application Support"  # macOS; the eval runs on a Mac, where roots.expand picks this
T0 = 1_761_000_000_000


def _put(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def build_cline(home: str) -> None:
    sessions = os.path.join(home, ".cline", "data", "sessions")
    _put(
        os.path.join(sessions, "sess-cl-1", "sess-cl-1.json"),
        {
            "version": 1,
            "session_id": "sess-cl-1",
            "cwd": "/work/cline-app",
            "started_at": "2026-10-20T09:00:00Z",
            "prompt": "make the quokkaflux-cline parser faster",
            "metadata": {"title": "Faster parser"},
        },
    )
    _put(
        os.path.join(sessions, "sess-cl-1", "sess-cl-1.messages.json"),
        {
            "version": 1,
            "agent": "lead",
            "sessionId": "sess-cl-1",
            "messages": [
                {
                    "role": "user",
                    "ts": T0,
                    "content": '<user_input mode="act">make the quokkaflux-cline parser faster</user_input>',
                },
                {
                    "role": "user",
                    "ts": T0 + 1,
                    "content": "INJECTEDNOISE hook context",
                    "metadata": {"displayRole": "system"},
                },
                {
                    "role": "assistant",
                    "ts": T0 + 2,
                    "content": [
                        {"type": "thinking", "thinking": "INJECTEDNOISE"},
                        {"type": "text", "text": "I will profile it first."},
                        {"type": "tool_use", "id": "t1", "name": "run_commands", "input": {"commands": ["make bench"]}},
                    ],
                },
                {
                    "role": "user",
                    "ts": T0 + 3,
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "INJECTEDNOISE bench output",
                            "is_error": True,
                        }
                    ],
                },
            ],
        },
    )
    _put(
        os.path.join(sessions, "sess-cl-2", "sess-cl-2.json"),
        {"version": 1, "session_id": "sess-cl-2", "cwd": "/work/cline-app", "metadata": {"importedFrom": "claude"}},
    )
    _put(
        os.path.join(sessions, "sess-cl-2", "sess-cl-2.messages.json"),
        {
            "version": 1,
            "agent": "lead",
            "messages": [{"role": "user", "content": "an imported chat, indexed by its own agent"}],
        },
    )
    task = os.path.join(home, SUPPORT, "Code", "User", "globalStorage", "saoudrizwan.claude-dev")
    _put(
        os.path.join(task, "state", "taskHistory.json"),
        [
            {
                "id": "1761000001",
                "ts": T0,
                "task": "Add retry to the uploader",
                "cwdOnTaskInitialization": "/work/uploader",
            }
        ],
    )
    _put(
        os.path.join(task, "tasks", "1761000001", "api_conversation_history.json"),
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<task>\nadd retry to the uploader\n</task>"},
                    {"type": "text", "text": "<environment_details>\nINJECTEDNOISE\n</environment_details>"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Reading the uploader first."}]},
        ],
    )


def build_roo(home: str) -> None:
    task = os.path.join(home, SUPPORT, "Cursor", "User", "globalStorage", "rooveterinaryinc.roo-cline", "tasks", "r-1")
    _put(
        os.path.join(task, "history_item.json"),
        {"id": "r-1", "ts": T0, "task": "Rename the quokkaflux-roo module", "workspace": "/work/roo-app"},
    )
    _put(
        os.path.join(task, "api_conversation_history.json"),
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<task>\nrename the quokkaflux-roo module\n</task>"},
                    {"type": "text", "text": "<environment_details>\nINJECTEDNOISE\n</environment_details>"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Renaming it now."}]},
            {"role": "user", "content": [{"type": "text", "text": "[read_file for 'a.py'] Result:\nINJECTEDNOISE"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "[ERROR] You did not use a tool in your previous response! INJECTEDNOISE\n"
                        "(This is an automated message, so do not respond to it conversationally.)",
                    }
                ],
            },
        ],
    )
    other = os.path.join(task, "..", "r-2")
    _put(
        os.path.join(other, "api_conversation_history.json"),
        [{"role": "user", "content": "<task>\nsecond roo task\n</task>"}],
    )


def build_kilo_legacy(home: str) -> None:
    task = os.path.join(home, SUPPORT, "Code", "User", "globalStorage", "kilocode.kilo-code", "tasks", "k-1")
    _put(
        os.path.join(task, "history_item.json"),
        {"id": "k-1", "ts": T0, "task": "Fix the quokkaflux-kilolegacy build", "workspace": "/work/kilo-ext"},
    )
    _put(
        os.path.join(task, "api_conversation_history.json"),
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "<task>\nfix the quokkaflux-kilolegacy build\n</task>"}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Checking the workflow file."}]},
        ],
    )


FIXTURES = {
    "cline": Fixture(build_cline, "quokkaflux-cline", "/work/cline-app", chats=2),
    "roo": Fixture(build_roo, "quokkaflux-roo", "/work/roo-app", chats=2),
    "kilo-legacy": Fixture(build_kilo_legacy, "quokkaflux-kilolegacy", "/work/kilo-ext", chats=1),
}

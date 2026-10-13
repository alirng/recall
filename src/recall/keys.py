"""API keys for the bring-your-own-key providers.

config.toml never holds a key, only where to find one:

    "env:NAME"   an environment variable
    "keychain"   the OS credential store (macOS Keychain, or libsecret on Linux)
    "file"       keys.json in recall's data folder, readable only by you

The nightly run starts without your shell's environment, so a stored key suits it best.
"""

import json
import os
import shutil
import subprocess
import sys

from . import paths

SERVICE = "recall"


def _keychain_available() -> bool:
    return (sys.platform == "darwin" and bool(shutil.which("security"))) or bool(shutil.which("secret-tool"))


def save(account: str, key: str) -> str:
    """Store a key; returns the reference to put in config.toml."""
    if sys.platform == "darwin" and shutil.which("security"):
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", key],
            check=True,
            capture_output=True,
        )
        return "keychain"
    if shutil.which("secret-tool"):
        subprocess.run(
            ["secret-tool", "store", f"--label={SERVICE} {account}", "service", SERVICE, "account", account],
            input=key,
            text=True,
            check=True,
            capture_output=True,
        )
        return "keychain"
    stored = _read_file()
    stored[account] = key
    os.makedirs(paths.HOME, exist_ok=True)
    fd = os.open(paths.KEYS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(stored, f)
    return "file"


ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def load(ref: str | None, account: str) -> str | None:
    if not ref:
        return None
    if ref.startswith("env:"):
        return os.environ.get(ref[4:])
    if ref == "file":
        return _read_file().get(account)
    if ref == "keychain":
        if sys.platform == "darwin":
            cmd = ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"]
        else:
            cmd = ["secret-tool", "lookup", "service", SERVICE, "account", account]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None
    raise ValueError(f"unknown key reference {ref!r}; use env:NAME, keychain or file")


def find(account: str) -> str | None:
    """A key stored earlier for this account, for a provider named without a key reference
    (e.g. `--model anthropic:claude-haiku-5-5` while config.toml uses another provider): the OS
    key store, then keys.json, then the vendor's usual environment variable."""
    for ref in ("keychain" if _keychain_available() else None, "file", f"env:{ENV.get(account, '')}"):
        if ref and ref != "env:":
            key = load(ref, account)
            if key:
                return key
    return None


def _read_file() -> dict:
    try:
        with open(paths.KEYS) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def storage_name() -> str:
    if _keychain_available():
        return "macOS Keychain" if sys.platform == "darwin" else "the system keyring"
    return f"{paths.KEYS} (readable only by you)"

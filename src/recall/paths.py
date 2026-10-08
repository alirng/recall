"""Where recall keeps its files. Override with RECALL_HOME and RECALL_MEMORY."""

import os

HOME = os.path.expanduser(os.environ.get("RECALL_HOME", "~/.agents/recall"))
DB = os.path.join(HOME, "recall.db")
CONFIG = os.path.join(HOME, "config.toml")
KEYS = os.path.join(HOME, "keys.json")
LOGS = os.path.join(HOME, "logs")
EVALS = os.path.join(HOME, "evals")

# Memory notes live apart from recall's own data: they are yours to read, edit and sync.
MEMORY = os.path.expanduser(os.environ.get("RECALL_MEMORY", "~/.agents/memory"))

"""Shared routines for the backend package.

Factored from existing conventions in buttons/server.py and scripts/:
- env/config loading via os.environ.get with defaults
- repo path resolution under a repos root
- structured JSON output via json.dumps
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def get_repos_dir() -> Path:
    return Path(os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos"))


def resolve_repo_path(repo: str) -> Path:
    name = repo.strip().removeprefix("arii/").replace("..", "").strip("/")
    return get_repos_dir() / name


def to_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def load_config(prefix: str = "BACKEND_") -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(prefix)}

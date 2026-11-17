"""Browser storage and Grid configuration."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from cli import repo_root


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (Path(repo_root()) / path).resolve()


def data_path(variable: str, filename: str) -> Path:
    root = _repo_path(os.environ.get("BROWSER_DATA_DIR", "data/browser"))
    return _repo_path(os.environ.get(variable, root / filename))


def grid_url() -> str:
    value = os.environ.get("BROWSER_GRID_URL", "http://127.0.0.1:4444").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "BROWSER_GRID_URL must be an http(s) base URL without query or fragment"
        )
    return value

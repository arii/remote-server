"""Pi session listing for the backend (read-only, stdlib only).

Small port of buttons/pi_logs.py list logic: scan the session dir
for *.jsonl newest-first and build pi-file list rows per
docs/session-record-contract.md. No background/ledger rows: this
backend owns no in-memory runs and adds no Jules surface, so the
list honestly contains only transcript files on disk (possibly []).
"""

from __future__ import annotations

import glob
import json
import os

TITLE_CAP = 80
TITLE_SCAN_LINES = 300
LIST_JSON_CAP = 100


def sessions_root() -> str:
    return os.environ.get(
        "PI_CODING_AGENT_SESSION_DIR", "/root/.pi/agent/sessions")


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def _first_text(obj: object) -> str | None:
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if (msg.get("role") or obj.get("role")) != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = _clean(content)
        return text[:TITLE_CAP] if text else None
    if isinstance(content, list):
        parts = [_clean(b["text"]) for b in content
                 if isinstance(b, dict) and b.get("text")]
        text = " ".join(parts)
        return text[:TITLE_CAP] if text else None
    return None


def _repo_from_path(path: str, cwd: str = "") -> str:
    if cwd:
        parts = [p for p in cwd.split("/") if p]
        if parts:
            return parts[-1]
    parent = os.path.basename(os.path.dirname(path))
    if parent.startswith("--") and parent.endswith("--"):
        cleaned = parent[2:-2]
        for prefix in ("srv-repos-", "home-ari-repos-", "srv-", "home-ari-"):
            if cleaned.startswith(prefix):
                return cleaned[len(prefix):]
        return cleaned
    return parent or ""


def _session_meta(path: str) -> tuple[str, str, str]:
    cwd, title = "", ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n >= TITLE_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "session" and not cwd:
                    cwd = obj.get("cwd") or ""
                if not title:
                    title = _first_text(obj) or ""
                if cwd and title:
                    break
    except Exception:
        pass
    return cwd, title or "Untitled session", _repo_from_path(path, cwd)


def list_sessions(root: str | None = None) -> list[dict]:
    """Newest-first pi-file session rows; [] on any failure."""
    base = root or sessions_root()
    try:
        files = glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
    except Exception:
        return []
    try:
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    except Exception:
        pass
    sessions = []
    for path in files[:LIST_JSON_CAP]:
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0
        cwd, title, repo = _session_meta(path)
        sessions.append({"file": os.path.basename(path), "mtime": mtime,
                         "cwd": cwd, "repo": repo, "title": title})
    return sessions

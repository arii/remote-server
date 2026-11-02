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


FILE_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def _find_session_file(name: str, root: str | None = None) -> str | None:
    base = root or sessions_root()
    try:
        matches = glob.glob(os.path.join(base, "**", name), recursive=True)
    except Exception:
        return None
    return matches[0] if matches else None


def get_session_detail(name: str, root: str | None = None) -> dict:
    """Pi-file session detail per docs/session-record-contract.md.

    Returns a detail dict with file/cwd/repo/turns (possibly []) or
    {"error": ...} on failure. Raises ValueError on a bad name,
    FileNotFoundError when no transcript matches.
    """
    if not name or not FILE_NAME_RE.match(name) or "/" in name:
        raise ValueError(f"bad session query: {name!r}")
    path = _find_session_file(name, root)
    if not path:
        raise FileNotFoundError(f"No session matching {name!r}")
    cwd, title, repo = _session_meta(path)
    turns: list[dict] = []
    total = 0
    started: object = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "session" and not started:
                    started = obj.get("timestamp") or obj.get("started") or ""
                    continue
                msg = obj.get("message") if isinstance(
                    obj.get("message"), dict) else obj
                role = msg.get("role") or obj.get("role") or ""
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    text = _clean(content)
                elif isinstance(content, list):
                    text = " ".join(
                        _clean(b.get("text")) for b in content
                        if isinstance(b, dict) and b.get("text"))
                else:
                    text = _clean(content or "")
                turn: dict = {"role": role, "text": text}
                ts = msg.get("timestamp") or obj.get("timestamp")
                if ts is not None:
                    turn["timestamp"] = ts
                turns.append(turn)
    except FileNotFoundError:
        raise
    except Exception as exc:
        return {"error": str(exc)}
    _ = title
    return {"file": name, "cwd": cwd, "repo": repo, "started": started,
            "turns": turns, "total_entries": total}


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

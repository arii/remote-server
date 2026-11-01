#!/usr/bin/env python3
"""pi_logs — readable Pi session audit for the S5 buttons page.

Pi keeps its full transcript (user prompts, assistant replies,
tool calls and results) as JSONL under ~/.pi/agent/sessions/
(--<cwd>--/<timestamp>_<uuid>.jsonl, v3 format). The buttons
Logs action runs this in the terminal container, where the
pi-data volume lives at /root/.pi, and prints:

  - the newest session files (so the reader can see what ran
    recently, and in which checkout), then
  - the latest session rendered readably: header (id, cwd,
    start), then role + text per message, tool calls by name,
    tool results truncated.

This is the audit view, not the archive: the full JSONL stays
on disk in pi-data, and this script only renders the tail of
the newest file. It never fails the Logs run — no sessions, an
unreadable file, or an unknown entry shape all degrade to a
plain note, exit 0. Stdlib only, like briefing_summary.py.
"""

import glob
import json
import os
import sys
from collections import deque

ROOT = os.environ.get("PI_CODING_AGENT_SESSION_DIR",
                      "/root/.pi/agent/sessions")
SHOW_SESSIONS = 10
SHOW_ENTRIES = 80
TEXT_CAP = 300
LIST_JSON_CAP = 100
TITLE_CAP = 80
TITLE_SCAN_LINES = 300


def _list_files():
    """Session files newest-first; [] on any failure."""
    try:
        files = glob.glob(os.path.join(ROOT, "**", "*.jsonl"),
                          recursive=True)
    except Exception:
        return []
    try:
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    except Exception:
        pass
    return files


def _first_text(obj):
    """First user-prompt text of a JSONL entry, or None."""
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
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(_clean(block["text"]))
        text = " ".join(parts)
        return text[:TITLE_CAP] if text else None
    return None


def _repo_from_path(path, cwd=""):
    """Derive repo name from cwd or enclosing directory name.
    e.g. cwd '/srv/repos/tech-dancer' -> 'tech-dancer',
    or dir '--srv-repos-tech-dancer--' -> 'tech-dancer'."""
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
    elif parent:
        return parent
    return ""


def _session_meta(path):
    """(cwd, first-user-prompt title, repo) for one session file.
    Scans only the head of the file: the header and the first prompt
    both live near the top, and a title must not cost a full read
    of a long transcript."""
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
    repo = _repo_from_path(path, cwd)
    if not title:
        title = "Untitled session"
    return cwd, title, repo


def _render_file(path, header=None):
    """Render one session file readably. Never raises. The
    default header names the session (basename, never a raw
    path); the default Logs mode passes its own "Latest session"
    header so that output is unchanged."""
    if header is None:
        header = f"=== Session: {os.path.basename(path)} ==="
    print(header)
    rendered = deque(maxlen=SHOW_ENTRIES)
    total = 0
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
                if isinstance(obj, dict) and obj.get("type") == "session":
                    print(f"Session {obj.get('id', '?')} — "
                          f"cwd {obj.get('cwd', '?')} — "
                          f"started {obj.get('timestamp', '?')}")
                    continue
                text = _render_entry(obj)
                if text:
                    rendered.append(text)
    except Exception as exc:
        print(f"Could not read session: {exc}")
        return
    print(f"({total} entries in file; showing last {len(rendered)} "
          "rendered messages; full JSONL stays on disk.)")
    for line in rendered:
        print(line)


def _clean(text):
    return " ".join(str(text or "").split())


def _render_entry(obj):
    """One readable line for a JSONL entry, or None to skip it."""
    turn = _parse_turn(obj)
    if not turn:
        return None
    parts = []
    if turn.get("text"):
        parts.append(turn["text"][:TEXT_CAP])
    for tc in turn.get("tool_calls", []):
        parts.append(f"[tool: {tc.get('name', '?')}]")
    for tr in turn.get("tool_results", []):
        out = tr.get("content") or tr.get("text") or ""
        parts.append(f"[tool result: {_clean(out)[:TEXT_CAP]}]")
    if parts:
        return f"{turn['role']}: " + " ".join(parts)
    return None


def _parse_turn(obj):
    """Parse one JSONL entry into a clean structured turn dict, or None."""
    if not isinstance(obj, dict) or obj.get("type") == "session":
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = msg.get("role") or obj.get("role")
    if not role:
        return None
    timestamp = obj.get("timestamp") or msg.get("timestamp")
    content = msg.get("content")
    text_parts = []
    tool_calls = []
    tool_results = []

    if isinstance(content, str):
        if content.strip():
            text_parts.append(content.strip())
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("text")
            if t and t.strip():
                text_parts.append(t.strip())
            elif block.get("type") in ("toolUse", "toolCall", "tool_use"):
                tool_calls.append({
                    "name": (block.get("name") or block.get("toolName")
                             or block.get("tool") or "?"),
                    "input": block.get("input") or block.get("args") or {}
                })
            elif block.get("type") in ("toolResult", "tool_result"):
                out = block.get("content") or block.get("output") or ""
                if isinstance(out, list):
                    out = " ".join(_clean(b.get("text", "")) for b in out
                                  if isinstance(b, dict))
                tool_results.append({
                    "name": block.get("name") or block.get("toolName") or "",
                    "content": str(out)
                })

    if not tool_calls and isinstance(msg.get("tool_calls"), list):
        for tc in msg["tool_calls"]:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                tool_calls.append({
                    "name": fn.get("name") or tc.get("name") or "?",
                    "input": fn.get("arguments") or tc.get("args") or {}
                })

    if not text_parts and not tool_calls and not tool_results:
        return None

    return {
        "role": role,
        "text": "\n".join(text_parts),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "timestamp": timestamp
    }


def _parse_session_file(path):
    """Parse session header and all conversation turns into a dict."""
    session_id, cwd, started = "", "", ""
    turns = []
    total = 0
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
                if isinstance(obj, dict) and obj.get("type") == "session":
                    session_id = obj.get("id") or session_id
                    cwd = obj.get("cwd") or cwd
                    started = obj.get("timestamp") or started
                    continue
                turn = _parse_turn(obj)
                if turn:
                    turns.append(turn)
    except Exception as exc:
        return {"error": str(exc), "turns": []}

    repo = _repo_from_path(path, cwd)
    return {
        "file": os.path.basename(path),
        "id": session_id,
        "cwd": cwd,
        "repo": repo,
        "started": started,
        "total_entries": total,
        "turns": turns
    }


def main(argv):
    # --list-json: machine-readable session list for the buttons
    # Pi session browser (newest first, basenames only — the
    # browser passes a basename back to --session, so no path
    # separator ever crosses the API). --session QUERY renders
    # the single file whose basename contains QUERY; ambiguous
    # or missing queries print a note and list, never fail.
    # Both exit 0 even on an empty or missing directory, same
    # contract as the default mode.
    if argv == ["--list-json"]:
        files = _list_files()
        sessions = []
        for path in files[:LIST_JSON_CAP]:
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = 0
            cwd, title, repo = _session_meta(path)
            sessions.append({"file": os.path.basename(path),
                             "mtime": mtime, "cwd": cwd,
                             "repo": repo,
                             "title": title})
        print(json.dumps({"sessions": sessions}))
        return 0
    if len(argv) == 2 and argv[0] == "--session-json":
        query = argv[1]
        files = _list_files()
        matches = [f for f in files
                   if query in os.path.basename(f)]
        if not matches:
            print(json.dumps({"error": f"No session matching '{query}' under {ROOT}"}))
            return 0
        if len(matches) > 1:
            print(json.dumps({"error": f"Multiple sessions match '{query}'",
                              "matches": [os.path.basename(f) for f in matches]}))
            return 0
        parsed = _parse_session_file(matches[0])
        print(json.dumps(parsed))
        return 0
    if argv == ["--clear"]:
        # Bulk-delete session transcripts. Files only, never
        # directories; the file set comes from _list_files, and
        # the mode takes no argument, so nothing from a request
        # can steer which files are removed. Exit 0 even when
        # there is nothing to delete — clearing an empty set is
        # not a failure.
        files = _list_files()
        deleted = 0
        for path in files:
            try:
                os.remove(path)
                deleted += 1
            except Exception as exc:
                print(f"Could not delete "
                      f"{os.path.basename(path)}: {exc}")
        print(f"Deleted {deleted} Pi session(s).")
        return 0
    if len(argv) == 2 and argv[0] == "--delete":
        # Delete ONE session transcript. The query must match
        # exactly one file; anything ambiguous or missing deletes
        # nothing and says so. Same exit-0 contract as --clear.
        query = argv[1]
        files = _list_files()
        matches = [f for f in files
                   if query in os.path.basename(f)]
        if not matches:
            print(f"No session matching '{query}' under {ROOT}")
            return 0
        if len(matches) > 1:
            print(f"Multiple sessions match '{query}' — "
                  "deleting nothing:")
            for path in matches:
                print(f"- {os.path.basename(path)}")
            return 0
        try:
            os.remove(matches[0])
            print(f"Deleted {os.path.basename(matches[0])}.")
        except Exception as exc:
            print(f"Could not delete "
                  f"{os.path.basename(matches[0])}: {exc}")
        return 0
    if len(argv) == 2 and argv[0] == "--session":
        query = argv[1]
        files = _list_files()
        matches = [f for f in files
                   if query in os.path.basename(f)]
        if not matches:
            print(f"No session matching '{query}' under {ROOT}")
            return 0
        if len(matches) > 1:
            print(f"Multiple sessions match '{query}':")
            for path in matches:
                print(f"- {os.path.basename(path)}")
            return 0
        _render_file(matches[0])
        return 0
    if argv:
        print("usage: pi_logs.py [--list-json | --session QUERY | "
              "--delete QUERY | --clear]")
        return 0
    print("=== Pi sessions ===")
    files = _list_files()
    if not files:
        print(f"No Pi sessions found under {ROOT}")
        return 0
    print(f"{len(files)} session file(s) under {ROOT}")
    for path in files[:SHOW_SESSIONS]:
        mtime = os.path.getmtime(path)
        import datetime
        when = datetime.datetime.fromtimestamp(
            mtime).strftime("%Y-%m-%d %H:%M")
        print(f"- {when} {os.path.basename(os.path.dirname(path))}/"
              f"{os.path.basename(path)}")
    latest = files[0]
    _render_file(latest, header=f"\n=== Latest session: {latest} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

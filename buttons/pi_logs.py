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
LIST_JSON_CAP = 20
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


def _session_meta(path):
    """(cwd, first-user-prompt title) for one session file.
    "" for either when absent; never raises. Scans only the
    head of the file: the header and the first prompt both
    live near the top, and a title must not cost a full read
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
    return cwd, title


def _render_file(path, header=None):
    """Render one session file readably. Never raises. The
    default header names the file; the default Logs mode passes
    its own "Latest session" header so that output is unchanged."""
    print(header if header is not None else f"=== Session: {path} ===")
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
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "session":
        return None  # header handled separately
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = msg.get("role") or obj.get("role")
    if not role:
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = _clean(content)
        return f"{role}: {text[:TEXT_CAP]}" if text else None
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if text:
                parts.append(_clean(text))
            elif block.get("type") in ("toolUse", "toolCall", "tool_use"):
                name = (block.get("name") or block.get("toolName")
                        or block.get("tool") or "?")
                parts.append(f"[tool: {name}]")
            elif block.get("type") in ("toolResult", "tool_result"):
                out = block.get("content") or block.get("output") or ""
                if isinstance(out, list):
                    out = " ".join(_clean(b.get("text", "")) for b in out
                                  if isinstance(b, dict))
                out = _clean(out)
                if out:
                    parts.append(f"[tool result: {out[:TEXT_CAP]}]")
        if parts:
            return f"{role}: " + " ".join(parts)
        return None
    return None


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
            cwd, title = _session_meta(path)
            sessions.append({"file": os.path.basename(path),
                             "mtime": mtime, "cwd": cwd,
                             "title": title})
        print(json.dumps({"sessions": sessions}))
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
              "--clear]")
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

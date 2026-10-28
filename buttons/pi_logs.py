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


def main():
    print("=== Pi sessions ===")
    try:
        files = glob.glob(os.path.join(ROOT, "**", "*.jsonl"),
                          recursive=True)
    except Exception as exc:
        print(f"Pi sessions unavailable: {exc}")
        return 0
    if not files:
        print(f"No Pi sessions found under {ROOT}")
        return 0
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    print(f"{len(files)} session file(s) under {ROOT}")
    for path in files[:SHOW_SESSIONS]:
        mtime = os.path.getmtime(path)
        import datetime
        when = datetime.datetime.fromtimestamp(
            mtime).strftime("%Y-%m-%d %H:%M")
        print(f"- {when} {os.path.basename(os.path.dirname(path))}/"
              f"{os.path.basename(path)}")
    latest = files[0]
    print(f"\n=== Latest session: {latest} ===")
    rendered = deque(maxlen=SHOW_ENTRIES)
    total = 0
    try:
        with open(latest, encoding="utf-8", errors="replace") as f:
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
        print(f"Could not read latest session: {exc}")
        return 0
    print(f"({total} entries in file; showing last {len(rendered)} "
          "rendered messages; full JSONL stays on disk.)")
    for line in rendered:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

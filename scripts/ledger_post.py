#!/usr/bin/env python3
"""Submit an entry to the Agent Ledger Google Form."""

import sys
import urllib.parse
import urllib.request

FORM_RESPONSE_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSeYyysgPaLfgYTUWb7uDGlnqppQgkZXuT2WINzhzOxzy7FxNQ/formResponse"
)

ENTRY_MAP = {
    "agent": "entry.421540333",  # Agent / Source
    "note": "entry.526880121",  # Item / Note
    "status": "entry.910915240",  # Status (Act | Later | Needs Okay | Done)
    "link": "entry.1391612799",  # Link
}


def post_entry(agent: str, note: str, status: str = "Act", link: str = "") -> int:
    payload = {
        ENTRY_MAP["agent"]: agent,
        ENTRY_MAP["note"]: note,
        ENTRY_MAP["status"]: status,
        ENTRY_MAP["link"]: link,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        FORM_RESPONSE_URL,
        data=data,
        method="POST",
        headers={"User-Agent": "Mozilla/5.0 (compatible; AgentLedgerBot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: ledger_post.py <agent> <note> [status] [link]")
        sys.exit(1)

    agent = sys.argv[1]
    note = sys.argv[2]
    status = sys.argv[3] if len(sys.argv) > 3 else "Act"
    link = sys.argv[4] if len(sys.argv) > 4 else ""

    code = post_entry(agent, note, status, link)
    print(f"Logged entry (HTTP {code}): [{agent}] {note}")


if __name__ == "__main__":
    main()

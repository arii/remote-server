#!/usr/bin/env python3
"""ledger_read.py - Headless reader for the Agent Ledger via Google Drive API.

Reads the private Agent Ledger Google Sheet directly using the authorized-user
OAuth token at /srv/remote-server/gdocs-access/token.json (override with
GOOGLE_TOKEN_FILE). Standard library only, so any python3 can run it.
Zero browser required, runs in ~1.5s.

Usage:
  python3 scripts/ledger_read.py                # Shows all 'Act' and 'Needs Okay' tasks
  python3 scripts/ledger_read.py --all          # Shows all tasks
  python3 scripts/ledger_read.py --status Act   # Filter by specific status
  python3 scripts/ledger_read.py --json         # Output JSON array
"""

import argparse
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_SHEET_ID = "1UGejaFVAPUhNaJS34pBFgfp5m2Lz_n4DkD64EMFzm0U"
DEFAULT_TOKEN_PATH = "/srv/remote-server/gdocs-access/token.json"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
EXPORT_URL = "https://www.googleapis.com/drive/v3/files/{}/export?mimeType=text/csv"
TIMEOUT_SECONDS = 30


def access_token(token_path: str) -> str:
    """Mint a fresh access token from the refresh token in an authorized-user file."""
    with open(token_path) as handle:
        info = json.load(handle)
    body = urllib.parse.urlencode(
        {
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "refresh_token": info["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    request = urllib.request.Request(
        info.get("token_uri") or DEFAULT_TOKEN_URI, data=body, method="POST"
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        token = json.load(response).get("access_token")
    if not token:
        raise RuntimeError("token refresh returned no access_token")
    return token


def read_ledger(sheet_id: str, token_path: str):
    request = urllib.request.Request(
        EXPORT_URL.format(urllib.parse.quote(sheet_id, safe="")),
        headers={"Authorization": f"Bearer {access_token(token_path)}"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        csv_text = response.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(csv_text)))


def fold_latest_per_key(rows):
    """Latest row per key wins, per Section 2 of the canonical Handoff Protocol."""
    import re

    folded = {}
    for r in rows:
        key = (r.get("Link") or "").strip()
        if not key:
            note = (r.get("Item / Note") or "").strip()
            m = re.search(r"task-[a-zA-Z0-9_-]+", note)
            key = m.group(0) if m else note
        folded[key] = r
    return list(folded.values())


def main():
    parser = argparse.ArgumentParser(description="Read Agent Ledger headlessly.")
    parser.add_argument(
        "--sheet-id", default=os.environ.get("AGENT_LEDGER_SHEET_ID", DEFAULT_SHEET_ID)
    )
    parser.add_argument(
        "--token-file", default=os.environ.get("GOOGLE_TOKEN_FILE", DEFAULT_TOKEN_PATH)
    )
    parser.add_argument(
        "--status", choices=["Act", "Later", "Needs Okay", "Done", "all"], default=None
    )
    parser.add_argument(
        "--all", action="store_true", help="Show all tasks including Done"
    )
    parser.add_argument(
        "--raw", action="store_true", help="Show raw un-folded append log"
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    # Determine status filter
    if args.all or args.status == "all":
        filter_status = None
    elif args.status:
        filter_status = [args.status]
    else:
        filter_status = ["Act", "Needs Okay"]

    try:
        rows = read_ledger(args.sheet_id, args.token_file)
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        print(f"Error reading Agent Ledger: {e}", file=sys.stderr)
        sys.exit(1)

    # Fold latest-row-per-key by default unless raw log requested
    display_rows = rows if args.raw else fold_latest_per_key(rows)

    filtered = [
        r for r in display_rows if not filter_status or r.get("Status") in filter_status
    ]

    if args.json:
        print(json.dumps(filtered, indent=2))
        return

    print(f"Agent Ledger Tasks ({len(filtered)} items shown):")
    print("-" * 80)
    for r in filtered:
        status = r.get("Status", "").strip() or "Unknown"
        agent = r.get("Agent / Source", "").strip() or "Unknown"
        note = r.get("Item / Note", "").strip()
        link = r.get("Link", "").strip()
        ts = r.get("Timestamp", "").strip()

        link_str = f" [{link}]" if link else ""
        print(f"[{status:^10}] ({agent}) {note}{link_str}  -- {ts}")
    print("-" * 80)


if __name__ == "__main__":
    main()

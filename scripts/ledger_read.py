#!/usr/bin/env python3
"""ledger_read.py - Headless reader for the Agent Ledger via Google Drive API.

Reads the private Agent Ledger Google Sheet directly using the authenticated
OAuth credentials at /srv/remote-server/gdocs-access/token.json.
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

DEFAULT_SHEET_ID = "1UGejaFVAPUhNaJS34pBFgfp5m2Lz_n4DkD64EMFzm0U"
DEFAULT_TOKEN_PATH = "/srv/remote-server/gdocs-access/token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service(token_path: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not os.path.exists(token_path):
        raise FileNotFoundError(f"OAuth token file not found: {token_path}")

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def read_ledger(sheet_id: str, token_path: str):
    service = get_drive_service(token_path)
    csv_bytes = (
        service.files()
        .export(fileId=sheet_id, mimeType="text/csv")
        .execute()
    )
    csv_text = csv_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(csv_text))
    return list(reader)


def main():
    parser = argparse.ArgumentParser(description="Read Agent Ledger headlessly.")
    parser.add_argument("--sheet-id", default=os.environ.get("AGENT_LEDGER_SHEET_ID", DEFAULT_SHEET_ID))
    parser.add_argument("--token-file", default=os.environ.get("GOOGLE_TOKEN_FILE", DEFAULT_TOKEN_PATH))
    parser.add_argument("--status", choices=["Act", "Later", "Needs Okay", "Done", "all"], default=None)
    parser.add_argument("--all", action="store_true", help="Show all rows including Done")
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
    except Exception as e:
        print(f"Error reading Agent Ledger: {e}", file=sys.stderr)
        sys.exit(1)

    filtered = [
        r for r in rows
        if not filter_status or r.get("Status") in filter_status
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

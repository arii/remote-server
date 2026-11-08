#!/usr/bin/env python3
"""Programmatic read/write for one Google Doc via Docs API + Drive API.

Auth: OAuth user consent (same pattern as previous_workouts
exercise_data/collect_revisions.py). Reuses the OAuth client secret copied
from /srv/repos/previous_workouts. First run needs a one-time browser
consent because write access needs broader scopes than the old readonly
token; the CLI prints the URL and exchanges the pasted code.

Usage:
  source .venv/bin/activate
  python gdocs.py auth-url
  python gdocs.py auth-code <code-or-localhost-url>
  python gdocs.py read [--doc-id ID]
  python gdocs.py append "hello" [--doc-id ID]
  python gdocs.py export [--doc-id ID] [--token-file PATH]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Export (plain-text read) only needs readonly access, so a token granted
# earlier for drive.readonly can refresh against this scope.
READ_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

PENDING_PATH = HERE / ".oauth_pending.json"


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)
            return
        except ImportError:
            pass
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def config():
    load_env()
    secret_rel = os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "").strip()
    token_rel = os.environ.get("GOOGLE_TOKEN_FILE", "token.json").strip()
    doc_id = os.environ.get("GOOGLE_DOCUMENT_ID", "").strip()
    if not secret_rel:
        raise SystemExit("Missing GOOGLE_CLIENT_SECRET_FILE in .env")
    secret_path = (HERE / secret_rel).resolve()
    if not secret_path.exists():
        raise SystemExit(f"Client secret not found: {secret_path}")
    token_path = (HERE / token_rel).resolve()
    return secret_path, token_path, doc_id


def auth_url():
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret_path, _, _ = config()
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    flow.redirect_uri = "http://localhost"
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    PENDING_PATH.write_text(json.dumps({"code_verifier": flow.code_verifier, "state": state}))
    print("Open this URL in your browser and authorize:")
    print(f"\n  {url}\n")
    print("Google redirects to an http://localhost page that cannot load.")
    print("Copy the 'code' value from that address bar, then run:")
    print("  python gdocs.py auth-code <code>")


def split_code_state(raw):
    if "code=" in raw:
        qs = parse_qs(urlparse(raw).query)
        code = (qs.get("code") or [raw])[0]
        state = (qs.get("state") or [None])[0]
        return code, state
    return raw, None


def auth_code(raw):
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret_path, token_path, _ = config()
    if not PENDING_PATH.exists():
        raise SystemExit("No pending auth. Run 'python gdocs.py auth-url' first.")
    pending = json.loads(PENDING_PATH.read_text())
    code, state = split_code_state(raw)
    if state and state != pending["state"]:
        raise SystemExit("State mismatch: that code is for a different auth URL.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    flow.redirect_uri = "http://localhost"
    flow.code_verifier = pending["code_verifier"]
    flow.fetch_token(code=code)
    token_path.write_text(flow.credentials.to_json())
    PENDING_PATH.unlink()
    print(f"Token saved to {token_path}")


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    secret_path, token_path, _ = config()
    _ = secret_path  # secret only needed for fresh consent, not refresh
    if not token_path.exists():
        raise SystemExit(
            "No token.json yet. Run 'python gdocs.py auth-url', authorize, "
            "then 'python gdocs.py auth-code <code>'."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
        return creds
    raise SystemExit(
        "Cached token invalid and cannot refresh. Re-run auth-url/auth-code. "
        "(Expected if the old token only had drive.readonly scope.)"
    )


def get_services():
    from googleapiclient.discovery import build

    creds = get_credentials()
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return docs_service, drive_service


def extract_text(doc):
    """Walk body content and return plain text. Handles paragraphs and tables."""
    parts = []

    def walk(elements):
        for el in elements or []:
            if "paragraph" in el:
                for run in el["paragraph"].get("elements", []):
                    text_run = run.get("textRun")
                    if text_run:
                        parts.append(text_run.get("content", ""))
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content"))
            elif "tableOfContents" in el:
                walk(el["tableOfContents"].get("content"))

    walk(doc.get("body", {}).get("content"))
    return "".join(parts)


def read_document(doc_id):
    docs_service, _ = get_services()
    doc = docs_service.documents().get(documentId=doc_id).execute()
    return doc.get("title", ""), extract_text(doc)


def end_index(doc):
    """Index one before the trailing newline: the append point."""
    content = doc.get("body", {}).get("content") or []
    if not content:
        return 1
    return content[-1].get("endIndex", 2) - 1


def append_text(doc_id, text):
    docs_service, _ = get_services()
    doc = docs_service.documents().get(documentId=doc_id).execute()
    index = end_index(doc)
    body = {"requests": [{"insertText": {"location": {"index": index}, "text": text}}]}
    docs_service.documents().batchUpdate(documentId=doc_id, body=body).execute()
    return index


def export_text(doc_id, token_file=None):
    """Plain-text read via Drive export. Works with drive.readonly scope.

    With --token-file, refresh happens in memory only and nothing is
    written back (safe to point at another checkout's cached token).
    """
    from googleapiclient.discovery import build

    if token_file:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(token_file, READ_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        drive_service = build("drive", "v3", credentials=creds)
    else:
        _, drive_service = get_services()
    return drive_service.files().export(fileId=doc_id, mimeType="text/plain").execute()


def main(argv=None):
    _, _, default_doc = config()
    parser = argparse.ArgumentParser(description="Read/write one Google Doc.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-url", help="print browser consent URL")
    p_code = sub.add_parser("auth-code", help="exchange pasted code for token")
    p_code.add_argument("code", help="bare code or full localhost redirect URL")
    p_read = sub.add_parser("read", help="print doc title + text")
    p_read.add_argument("--doc-id", default=default_doc)
    p_append = sub.add_parser("append", help="append text to end of doc")
    p_append.add_argument("text", help="text to append (include \\n as needed)")
    p_append.add_argument("--doc-id", default=default_doc)
    p_export = sub.add_parser("export", help="plain-text read via Drive export (works today)")
    p_export.add_argument("--doc-id", default=default_doc)
    p_export.add_argument("--token-file", default=None,
                          help="cached OAuth token to use read-only, without writing back")
    args = parser.parse_args(argv)

    if args.cmd == "auth-url":
        auth_url()
    elif args.cmd == "auth-code":
        auth_code(args.code)
    elif args.cmd == "read":
        if not args.doc_id:
            raise SystemExit("Missing doc id: set GOOGLE_DOCUMENT_ID in .env or pass --doc-id")
        title, text = read_document(args.doc_id)
        print(f"Title: {title}")
        print("--- content ---")
        print(text)
    elif args.cmd == "append":
        if not args.doc_id:
            raise SystemExit("Missing doc id: set GOOGLE_DOCUMENT_ID in .env or pass --doc-id")
        index = append_text(args.doc_id, args.text)
        print(f"Inserted {len(args.text)} chars at index {index}")
    elif args.cmd == "export":
        if not args.doc_id:
            raise SystemExit("Missing doc id: set GOOGLE_DOCUMENT_ID in .env or pass --doc-id")
        data = export_text(args.doc_id, token_file=args.token_file)
        sys.stdout.write(data.decode("utf-8-sig"))


if __name__ == "__main__":
    main(sys.argv[1:])

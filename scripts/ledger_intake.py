#!/usr/bin/env python3
"""Deterministic normalization for Agent Ledger intake rows."""

import hashlib
import re
import unicodedata


STATUS_VALUES = ("NEW", "NEEDS_INFO", "DUPLICATE", "READY", "DISPATCHED")
REQUIRED_FIELDS = ("title", "repo", "problem", "scope", "done_when")


def normalize(value):
    """Normalize user-entered content for stable comparisons and hashes."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"\s+", " ", text)


def dedupe_hash(title, repo):
    """Return the SHA-256 key for a normalized title/repository pair."""
    content = f"{normalize(title)}\n{normalize(repo)}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def intake_id(title, repo):
    """Return the stable row key used to identify the same intake item."""
    return f"ledger-intake-{dedupe_hash(title, repo)[:24]}"


def missing_fields(row):
    """Return required fields absent from a raw intake row."""
    return [
        field for field in REQUIRED_FIELDS if not str(row.get(field, "") or "").strip()
    ]


def status_for(row, *, duplicate=False):
    """Derive the explicit intake state from completeness and duplication."""
    if duplicate:
        return "DUPLICATE"
    if missing_fields(row):
        return "NEEDS_INFO"
    return "READY"


def build_row(row, *, duplicate=False):
    """Build the deterministic fields written by the intake submit handler."""
    title = row.get("title", "")
    repo = row.get("repo", "")
    missing = missing_fields(row)
    return {
        **row,
        "intake_id": intake_id(title, repo),
        "dedupe_hash": dedupe_hash(title, repo),
        "canonical_intake_id": row.get("canonical_intake_id") or intake_id(title, repo),
        "agent_issue_url": str(row.get("agent_issue_url", "") or "").strip(),
        "missing_fields": ",".join(missing),
        "status": status_for(row, duplicate=duplicate),
    }


def claim_row(row, issue_url):
    """Atomically set the issue back-reference on an unclaimed row."""
    url = str(issue_url or "").strip()
    if not url:
        raise ValueError("issue_url is required to claim a ledger row")

    title = row.get("title", "")
    repo = row.get("repo", "")
    claimed = {
        **row,
        "canonical_intake_id": row.get("canonical_intake_id") or intake_id(title, repo),
        "agent_issue_url": str(row.get("agent_issue_url", "") or "").strip(),
    }
    if claimed["agent_issue_url"]:
        return claimed
    claimed["agent_issue_url"] = url
    return claimed


def ready_rows(rows):
    """Return only rows explicitly marked READY for queue dispatch."""
    return [row for row in rows if row.get("status") == "READY"]

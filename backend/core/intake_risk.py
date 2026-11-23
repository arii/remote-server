"""Deterministic high-blast-radius detection for Agent Ledger intake rows (#1586).

Rows that mention a production mutation, an external communication, or a
billing/permission change lock to NEEDS_APPROVAL even when schema-complete, so
a human must confirm before the row can become READY. Detection is plain
keyword matching over the row text. No model inference. False positives are
acceptable: a human clears them with one confirm. Stdlib only, shared by
scripts/ledger_intake.py and backend/api/routers/briefing.py (the backend
image does not ship scripts/).

Keywords match case-insensitively at a word start, so "deploy" also matches
"deploys" and "deployment".
"""

import re

PRODUCTION_MUTATION = "production_mutation"
EXTERNAL_COMMS = "external_comms"
BILLING_PERMISSION = "billing_permission"

PRODUCTION_MUTATION_KEYWORDS = (
    "deploy",
    "migrate",
    "migration",
    "delete prod",
    "production data",
    "drop table",
    "drop database",
    "drop schema",
    "drop column",
    "drop index",
    "truncate table",
    "rm -rf",
    "compose up",
    "restart service",
    "restart the service",
    "restart all service",
)
EXTERNAL_COMMS_KEYWORDS = (
    "email",
    "e-mail",
    "tweet",
    "post to",
    "post a ",
    "slack",
    "ntfy",
    "publish",
    "comment on",
    "send a message",
    "announce",
)
BILLING_PERMISSION_KEYWORDS = (
    "billing",
    "payment",
    "spend cap",
    "quota",
    "permission",
    "iam",
    "token scope",
    "branch protection",
    "secret",
    "admin",
)

RISK_CATEGORIES = (
    (PRODUCTION_MUTATION, PRODUCTION_MUTATION_KEYWORDS),
    (EXTERNAL_COMMS, EXTERNAL_COMMS_KEYWORDS),
    (BILLING_PERMISSION, BILLING_PERMISSION_KEYWORDS),
)
RISK_TEXT_FIELDS = ("title", "problem", "scope", "done_when", "notes")

_PATTERNS = tuple(
    (
        name,
        re.compile("|".join(r"(?<![a-z0-9])" + re.escape(word) for word in words)),
    )
    for name, words in RISK_CATEGORIES
)


def risk_categories(row):
    """Return the risk categories a raw row matches, in definition order."""
    text = " ".join(str(row.get(field) or "") for field in RISK_TEXT_FIELDS)
    text = re.sub(r"\s+", " ", text).casefold()
    return [name for name, pattern in _PATTERNS if pattern.search(text)]

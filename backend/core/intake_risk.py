"""Deterministic high-blast-radius detection for Agent Ledger intake rows (#1586).

Rows that mention a production mutation, an external communication, or a
billing/permission change lock to NEEDS_APPROVAL even when schema-complete, so
a human must confirm before the row can become READY. Detection is plain
keyword matching over the row text. No model inference. False positives are
acceptable: a human clears them with one confirm. Stdlib only, shared by
scripts/ledger_intake.py and backend/api/routers/briefing.py (the backend
image does not ship scripts/). The module itself imports only the stdlib, but
importing it through the backend.core package runs backend/core/__init__.py,
so scripts/ledger_intake.py needs the backend dependencies installed.

Keywords match case-insensitively at a word start, so "deploy" also matches
"deploys" and "deployment". Short tokens (WHOLE_WORD_KEYWORDS) must match as
whole words, so "iam" does not match "iambic". PHRASE_PATTERNS are regexes for
phrasings a prefix keyword cannot express ("delete the prod database").
"""

import re

PRODUCTION_MUTATION = "production_mutation"
EXTERNAL_COMMS = "external_comms"
BILLING_PERMISSION = "billing_permission"

PRODUCTION_MUTATION_KEYWORDS = (
    "deploy",
    "redeploy",
    "roll out",
    "rollout",
    "roll back",
    "rollback",
    "force push",
    "force-push",
    "push --force",
    "push -f",
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
    "rotate",
    "api key",
    "token scope",
    "branch protection",
    "secret",
    "admin",
)

# Short tokens that must match as whole words.
WHOLE_WORD_KEYWORDS = {
    EXTERNAL_COMMS: ("ntfy",),
    BILLING_PERMISSION: ("iam",),
}
# Extra regexes (matched against the casefolded, whitespace-collapsed text).
PHRASE_PATTERNS = {
    PRODUCTION_MUTATION: (
        r"\b(delete|drop|wipe|purge|destroy|remove)\b[^.;\n]{0,40}\b(prod|production)\b",
        r"\b(roll|rolls|rolled|rolling)\s+(it\s+|this\s+|that\s+)?out\b",
    ),
}

RISK_CATEGORIES = (
    (PRODUCTION_MUTATION, PRODUCTION_MUTATION_KEYWORDS),
    (EXTERNAL_COMMS, EXTERNAL_COMMS_KEYWORDS),
    (BILLING_PERMISSION, BILLING_PERMISSION_KEYWORDS),
)
RISK_TEXT_FIELDS = ("title", "problem", "scope", "done_when", "notes")


def _compile(name, words):
    parts = [r"(?<![a-z0-9])" + re.escape(word) for word in words]
    parts += [
        r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])"
        for word in WHOLE_WORD_KEYWORDS.get(name, ())
    ]
    parts += list(PHRASE_PATTERNS.get(name, ()))
    return re.compile("|".join(parts))


_PATTERNS = tuple((name, _compile(name, words)) for name, words in RISK_CATEGORIES)


def risk_categories(row):
    """Return the risk categories a raw row matches, in definition order."""
    text = " ".join(str(row.get(field) or "") for field in RISK_TEXT_FIELDS)
    text = re.sub(r"\s+", " ", text).casefold()
    return [name for name, pattern in _PATTERNS if pattern.search(text)]

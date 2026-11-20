"""Deterministic literal acceptance-criterion checks (issue #1375).

A generated spec can pair an instruction ("update the historical
record") with a literal acceptance criterion ("zero occurrences of
`wrapper_name`"), and a semantic audit can then approve the exception
because the retained mention is documentation-only. These helpers give
both stages one deterministic source of truth:

- find_literal_criteria: extract zero-occurrence criteria from a spec.
- spec_contradictions: flag a spec whose own instructions name a
  string its acceptance criteria forbid (pre-coding detection).
- unsatisfied_criteria: scan the worktree and report criteria whose
  forbidden string survives (audit-time enforcement), with matches
  classified as code, comment or documentation so a criterion that
  permits documentation-only mentions is not tripped by them.
"""

import re
import subprocess

_CHECKLIST_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])\s*(?:\[[ xX]\]\s*)?(?P<text>.+?)\s*$"
)
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_ZERO_OF_RE = re.compile(
    r"(?i)\b(?:zero|no)\s+(?:remaining\s+)?"
    r"(?:(?:symbol|code)\s+)?"
    r"(?:occurrences|matches|mentions|references|calls)\s+of\s+"
    r"[\"'`](?P<target>[^\"'`]+)[\"'`]"
)
_GREP_ZERO_RE = re.compile(
    r"(?i)grep\s+.*?[\"'](?P<target>[^\"']+)[\"'].*?"
    r"(?:returns?|yields?|shows?)\s+(?:zero|no)\s+(?:matches|results|occurrences)"
)
_PERMITS_DOCS_RE = re.compile(
    r"(?i)\bsymbol\s+references?\b|\bcode\s+references?\b|"
    r"\bcomments?\s+(?:or|and)\s+(?:docs|documentation)\b|"
    r"\bdocumentation[- ]only\b|"
    r"\b(?:except|excluding|save)\s+(?:in\s+)?(?:comments|docs|documentation)\b"
)
_NON_INSTRUCTION_RE = re.compile(
    r"(?i)acceptance criteria|investigation evidence|q&a context"
)
_DOC_EXTS = (".md", ".rst", ".txt", ".adoc")
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", "<!--", '"""', "'''", ";")


def _sections(text):
    """Yield (heading title or None, body) pairs in document order."""
    title, lines = None, []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            yield title, "\n".join(lines)
            title, lines = match["title"], []
        else:
            lines.append(line)
    yield title, "\n".join(lines)


def _acceptance_items(text):
    items = []
    for title, body in _sections(text):
        if not title or "acceptance criteria" not in title.lower():
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped[0] in "-*+" or re.match(r"\d+[.)]", stripped):
                item = _CHECKLIST_RE.match(line)
                if item:
                    items.append(item["text"])
    return items


def find_literal_criteria(spec_text):
    """Zero-occurrence literal criteria from a spec's Acceptance Criteria.

    Each result is {"target", "text", "permits_docs"}. permits_docs is
    true when the criterion itself distinguishes symbol or code
    references from comments and documentation, so a retained
    documentation-only mention does not violate it.
    """
    criteria = []
    for item in _acceptance_items(spec_text):
        match = _ZERO_OF_RE.search(item) or _GREP_ZERO_RE.search(item)
        if not match:
            continue
        criteria.append(
            {
                "target": match["target"],
                "text": item,
                "permits_docs": bool(_PERMITS_DOCS_RE.search(item)),
            }
        )
    return criteria


def spec_contradictions(spec_text):
    """Spec instructions that contradict the spec's own literal criteria.

    A contradiction is an acceptance criterion forbidding any
    occurrence of a string while instruction-bearing sections still
    name that string outside a restatement of the ban, for example a
    plan step that asks to update a historical record titled with it.
    Quoted investigation evidence and Q&A context are not instructions
    and are ignored.
    """
    criteria = find_literal_criteria(spec_text)
    if not criteria:
        return []
    instruction_lines = []
    for title, body in _sections(spec_text):
        if title and _NON_INSTRUCTION_RE.search(title):
            continue
        instruction_lines.extend(body.splitlines())
    errors = []
    for criterion in criteria:
        target = criterion["target"]
        for line in instruction_lines:
            if target not in line:
                continue
            if _ZERO_OF_RE.search(line) or _GREP_ZERO_RE.search(line):
                continue  # a restatement of the ban, not a contradiction
            errors.append(
                "acceptance criterion contradicts the spec: "
                f"'{criterion['text']}' forbids '{target}' but the spec "
                "names it outside the acceptance criteria; reconcile the "
                "plan or the criterion before coding (issue #1375)"
            )
            break
    return errors


def _classify(path, line_text):
    if path.lower().endswith(_DOC_EXTS):
        return "documentation"
    if line_text.strip().startswith(_COMMENT_PREFIXES):
        return "comment"
    return "code"


def scan_worktree(root, target):
    """Tracked occurrences of target under root, classified by kind."""
    result = subprocess.run(
        ["git", "grep", "-n", "-F", "--", target],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {result.stderr.strip()[:200]}")
    matches = []
    for line in result.stdout.splitlines():
        path, line_no, text = line.split(":", 2)
        matches.append(
            {
                "path": path,
                "line": int(line_no),
                "kind": _classify(path, text),
                "text": text.strip()[:120],
            }
        )
    return matches


def unsatisfied_criteria(root, criteria):
    """Criteria whose forbidden string still occurs in the worktree.

    A criterion fails on any code occurrence, or on any occurrence at
    all when it does not permit documentation-only mentions. Each
    failure carries up to five surviving occurrences as evidence.
    """
    failures = []
    for criterion in criteria:
        occurrences = scan_worktree(root, criterion["target"])
        if criterion["permits_docs"]:
            violations = [m for m in occurrences if m["kind"] == "code"]
        else:
            violations = occurrences
        if violations:
            failures.append(
                {
                    "criterion": criterion,
                    "total": len(violations),
                    "occurrences": violations[:5],
                }
            )
    return failures

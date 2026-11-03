"""Agent-native skill loader for deep_chat turns (roadmap item 6).

Reads deep-agent/agent_skills/<name>/SKILL.md and injects the matching
bodies into the turn system prompt, routed by intent. Deliberately NOT
the harness SkillsMiddleware: that needs a file backend (forbidden in
server contexts), while two small read-only docs inject
deterministically with no new attack surface. The pi-config skills are
NOT loaded here — they target MCP tools this agent lacks.
"""

import os

import intent

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "agent_skills")

INTENT_SKILLS = {
    "review": ("repo-review",),
    "implement": ("dispatch-brief",),
    "fix": ("dispatch-brief",),
}


def skill_path(name):
    """Absolute SKILL.md path, or '' for unsafe names."""
    if not name or "/" in name or name.startswith("."):
        return ""
    return os.path.join(SKILLS_DIR, name, "SKILL.md")


def list_skills():
    """Installed skill names (a dir with SKILL.md inside)."""
    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return []
    return [e for e in entries
            if os.path.isfile(os.path.join(SKILLS_DIR, e, "SKILL.md"))]


def parse_skill_file(path):
    """(name, description, body) from SKILL.md; blanks on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", "", ""
    name, description, body = "", "", text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "name":
                    name = value.strip()
                elif key.strip() == "description":
                    description = value.strip()
            body = text[end + len("\n---\n"):]
    return name, description, body


def load_skill(name):
    """Full SKILL.md text, or '' when missing/unreadable."""
    path = skill_path(name)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def system_addition(intent_name):
    """Intent hint + skill bodies for the turn system prompt."""
    parts = []
    hint = intent.hint(intent_name)
    if hint:
        parts.append(hint)
    for name in INTENT_SKILLS.get(intent_name, ()):
        body = load_skill(name)
        if body:
            parts.append(body)
    return "\n\n".join(parts)

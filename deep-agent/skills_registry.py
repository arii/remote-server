"""Progressive-disclosure skills registry for LangGraph nodes.

Scans deep-agent/skills/*.md for simple YAML frontmatter (name,
description, trigger_nodes), indexes ~100-token summaries at startup,
and injects full Markdown runbooks only for skills targeting the
active node. Stdlib-only: no PyYAML dependency (neither venv ships it).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class SkillNotFoundError(KeyError):
    """Raised when a requested skill markdown file does not exist."""


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    trigger_nodes: tuple
    path: Path

    def to_index_line(self) -> str:
        nodes = ", ".join(self.trigger_nodes) if self.trigger_nodes else "manual"
        return f"- **{self.name}** (`{nodes}`): {self.description}"


def _parse_frontmatter(raw: str) -> tuple:
    """Split `---` frontmatter from body; parse the small fixed schema
    (name: str, description: str, trigger_nodes: [str]) without yaml."""
    frontmatter: dict = {}
    body = raw
    if raw.startswith("---\n"):
        parts = raw.split("---\n", 2)
        if len(parts) == 3:
            fm_text, body = parts[1], parts[2].strip()
            current_list = None
            for line in fm_text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    current_list = None
                    continue
                if stripped.startswith("- ") and current_list is not None:
                    frontmatter[current_list].append(stripped[2:].strip())
                    continue
                if ":" in stripped:
                    key, _, value = stripped.partition(":")
                    key, value = key.strip(), value.strip()
                    if value == "":
                        frontmatter[key] = []
                        current_list = key
                    else:
                        frontmatter[key] = value
                        current_list = None
    return frontmatter, body


class SkillsRegistry:
    """Progressive disclosure skills registry for LangGraph nodes."""

    def __init__(self, skills_dir=None) -> None:
        self.skills_dir = skills_dir or (Path(__file__).resolve().parent / "skills")
        self._meta_cache: dict = {}
        self._body_cache: dict = {}
        self._source_hash: str | None = None
        self.refresh_index()

    def source_hash(self) -> str:
        """SHA-256 over sorted skill file bytes (#1214)."""
        digest = hashlib.sha256()
        if self.skills_dir.exists():
            for md_path in sorted(self.skills_dir.glob("*.md")):
                digest.update(md_path.name.encode())
                try:
                    digest.update(md_path.read_bytes())
                except OSError:
                    digest.update(b"\x00unreadable")
        return digest.hexdigest()

    def refresh_if_stale(self) -> bool:
        """Reindex when skill files changed since load; True on refresh."""
        current = self.source_hash()
        if current == self._source_hash:
            return False
        self.refresh_index()
        return True

    def refresh_index(self) -> None:
        self._meta_cache.clear()
        self._body_cache.clear()
        if self.skills_dir.exists():
            for md_path in sorted(self.skills_dir.glob("*.md")):
                meta, body = self._parse_skill_file(md_path)
                self._meta_cache[meta.name] = meta
                self._body_cache[meta.name] = body
        self._source_hash = self.source_hash()

    @staticmethod
    def _parse_skill_file(path: Path) -> tuple:
        raw = path.read_text(encoding="utf-8")
        frontmatter, body = _parse_frontmatter(raw)
        name = str(frontmatter.get("name") or path.stem)
        description = str(frontmatter.get("description") or "").strip()
        triggers = frontmatter.get("trigger_nodes") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        triggers = tuple(str(t) for t in triggers)
        return SkillMetadata(
            name=name, description=description, trigger_nodes=triggers, path=path
        ), body

    def skill_names(self) -> list:
        self.refresh_if_stale()
        return sorted(self._meta_cache)

    def get_progressive_index_summary(self) -> str:
        """Returns compact ~100-token index of all registered skills."""
        self.refresh_if_stale()
        if not self._meta_cache:
            return "No dynamic skills registered."
        lines = ["### Available Domain Skills (Progressive Disclosure Index)"]
        for meta in self._meta_cache.values():
            lines.append(meta.to_index_line())
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        self.refresh_if_stale()
        if name not in self._body_cache:
            raise SkillNotFoundError(f"Skill '{name}' not found in {self.skills_dir}")
        return self._body_cache[name]

    def inject_skills_for_node(
        self, node_name: str, base_system_prompt: str, *, compact: bool = False
    ) -> str:
        """Appends skill instructions ONLY for skills targeting `node_name`.

        Default injects the full Markdown runbooks. compact=True injects
        one index line per matched skill plus its checkout-relative
        path, so the full runbook loads on demand via read_repo_file
        instead of riding in every turn's window (D1: the skills block
        has its own token share inside the shared context budget).
        """
        self.refresh_if_stale()
        matched = [
            self._meta_cache[name]
            for name, meta in self._meta_cache.items()
            if node_name in meta.trigger_nodes
        ]
        if not matched:
            return base_system_prompt
        if compact:
            lines = [meta.to_index_line() for meta in matched]
            paths = ", ".join(f"deep-agent/skills/{meta.path.name}" for meta in matched)
            header = (
                f"## Active Node Skills (`{node_name}`) — summaries only; "
                f"read the full runbook with read_repo_file from: {paths}"
            )
            return (
                f"{base_system_prompt.rstrip()}\n\n{header}\n\n"
                + "\n".join(lines)
                + "\n"
            )
        injected_block = "\n\n---\n\n".join(
            self._body_cache[meta.name] for meta in matched
        )
        return f"{base_system_prompt.rstrip()}\n\n## Active Node Skills (`{node_name}`)\n\n{injected_block}\n"

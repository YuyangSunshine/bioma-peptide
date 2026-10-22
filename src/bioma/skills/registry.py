from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from bioma.skills.locations import SkillLocations, location_root, package_paths


class SkillRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    path: Path
    source: str
    taxonomy_namespace: str = ""
    status: str = "active"
    domain: str = "general"
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class SkillRegistry:
    """Metadata-first skill discovery with explicit full-content loading."""

    LINKED_DIRS = frozenset({"references", "templates", "scripts", "assets"})

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [root.resolve() for root in roots]
        self._skills: dict[str, SkillRecord] = {}
        self.parse_errors: dict[str, str] = {}
        self.generation = 0
        self._locations: list[SkillLocations] = []

    def scan(self) -> int:
        self.generation += 1
        self._skills.clear()
        self.parse_errors.clear()
        records = []
        self._locations = []
        for root in self.roots:
            base = location_root(root)
            locations = SkillLocations(base) if base else None
            if locations is not None:
                self._locations.append(locations)
            for package in package_paths(root, locations):
                path = package / 'SKILL.md'
                try:
                    skill = self._parse(path, root.name)
                    if locations is not None:
                        sid = locations.resolve_skill_id(path.parent.relative_to(locations.root.parent).as_posix())
                        if sid is not None:
                            location = locations.locations[sid]
                            if not location['runtime']:
                                continue
                            skill.name = location['runtime_id']
                            skill.source = location['source_namespace']
                            skill.taxonomy_namespace = location['taxonomy_namespace']
                    records.append(skill)
                except (ValueError, yaml.YAMLError) as exc:
                    self.parse_errors[str(path)] = str(exc)
        counts = Counter(skill.name for skill in records)
        for skill in records:
            if counts[skill.name] > 1:
                root = next(
                    root for root in self.roots if skill.path.is_relative_to(root)
                )
                skill.name = skill.path.parent.relative_to(root).as_posix()
            self._skills[skill.name] = skill
        return len(self._skills)

    def all(self) -> tuple[SkillRecord, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    def list(self, question: str, *, limit: int = 8) -> dict[str, Any]:
        query_tokens = _tokens(question)
        rows: list[tuple[int, SkillRecord]] = []
        for skill in self._skills.values():
            if skill.status not in {"active", "experimental"}:
                continue
            score = (
                4 * len(query_tokens & _tokens(skill.name))
                + 2 * len(query_tokens & _tokens(" ".join([skill.domain, *skill.tags])))
                + len(query_tokens & _tokens(skill.description))
            )
            if score == 0:
                continue
            rows.append((score, skill))
        rows.sort(key=lambda item: (-item[0], item[1].name))
        selected = rows[: max(1, min(12, limit))]
        return {
            "success": True,
            "returned": len(selected),
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description[:300],
                    "domain": skill.domain,
                    "tags": skill.tags,
                }
                for _, skill in selected
            ],
        }

    def view(
        self,
        name: str,
        *,
        file_path: str | None = None,
        max_chars: int = 8000,
    ) -> dict[str, Any]:
        skill = self._skills.get(name)
        if skill is None:
            for locations in self._locations:
                sid = locations.resolve_skill_id(name)
                if sid is not None and locations.locations[sid]['runtime']:
                    skill = self._skills.get(locations.locations[sid]['runtime_id'])
                    if skill is not None:
                        break
        if skill is None:
            return {"success": False, "error": f"Unknown skill: {name}"}
        target = skill.path
        if file_path:
            relative = Path(file_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] not in self.LINKED_DIRS
            ):
                return {"success": False, "error": "Invalid linked file path."}
            target = (skill.path.parent / relative).resolve()
            try:
                target.relative_to(skill.path.parent.resolve())
            except ValueError:
                return {
                    "success": False,
                    "error": "Linked file escapes skill directory.",
                }
        if not target.is_file():
            return {"success": False, "error": f"Skill file not found: {file_path}"}

        content = target.read_text(encoding="utf-8", errors="replace")
        bounded = content[: max(1, max_chars)]
        return {
            "success": True,
            "name": skill.name,
            "file": str(target.relative_to(skill.path.parent)),
            "tools": skill.tools,
            "content": bounded,
            "content_truncated": len(bounded) < len(content),
            "content_chars": len(content),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "linked_files": self._linked_files(skill.path.parent),
        }

    @classmethod
    def _parse(cls, path: Path, source: str) -> SkillRecord:
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter = _frontmatter(text)
        metadata = frontmatter.get("metadata") or {}
        routing = frontmatter.get("routing") or {}
        name = str(frontmatter.get("name") or path.parent.name).strip()
        description = str(frontmatter.get("description") or "").strip()
        if not name or not description:
            raise ValueError("SKILL.md requires name and description front matter.")
        return SkillRecord(
            name=name,
            description=description,
            path=path.resolve(),
            source=source,
            status=str(metadata.get("status") or "active").casefold(),
            domain=str(metadata.get("domain") or "general"),
            tags=[str(item) for item in routing.get("tags") or []],
            tools=[str(item) for item in frontmatter.get("tools") or []],
        )

    @classmethod
    def _linked_files(cls, skill_dir: Path) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for dirname in sorted(cls.LINKED_DIRS):
            root = skill_dir / dirname
            if not root.is_dir():
                continue
            files = [
                str(path.relative_to(skill_dir))
                for path in sorted(root.rglob("*"))
                if path.is_file()
            ]
            if files:
                result[dirname] = files
        return result


def _frontmatter(text: str) -> dict[str, Any]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}
    end = normalized.find("\n---", 4)
    if end < 0:
        return {}
    payload = yaml.safe_load(normalized[4:end]) or {}
    if not isinstance(payload, dict):
        raise ValueError("Skill front matter must be a mapping.")
    return payload


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(segment) == 1:
            tokens.add(segment)
        else:
            tokens.update(
                segment[index : index + 2] for index in range(len(segment) - 1)
            )
    return tokens

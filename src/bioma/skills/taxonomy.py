"""Taxonomy discovery over the same records used by skill_view."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from bioma.skills.registry import SkillRegistry


def normalize(path: str) -> str:
    return "/".join(part.strip().casefold() for part in path.split("/") if part.strip())


class TaxonomyRegistry:
    def __init__(self, skills: SkillRegistry, path: Path) -> None:
        self.skills = {skill.name: skill for skill in skills.all()}
        by_path = {skill.path.parent: skill.name for skill in skills.all()}
        self.index: dict[str, set[str]] = defaultdict(set)
        indexed: set[str] = set()
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
        for entry in entries:
            name = by_path.get((path.parent.parent / entry["path"]).resolve())
            if name is None:
                raise ValueError(
                    f"Taxonomy references an unloaded skill: {entry['path']}"
                )
            categories = entry["taxonomy"]
            for category in [categories] if isinstance(categories, str) else categories:
                self._add(category, name)
            indexed.add(name)
        for name in self.skills.keys() - indexed:
            namespace = self.skills[name].taxonomy_namespace or self.skills[name].path.parent.parent.name
            self._add(f"其他/{namespace}", name)

    def _add(self, category: str, name: str) -> None:
        parts = normalize(category).split("/")
        for depth in range(1, len(parts) + 1):
            self.index["/".join(parts[:depth])].add(name)

    def resolve(self, path: str) -> tuple[str | None, list[str]]:
        path = normalize(path)
        if not path or path in self.index:
            return path, []
        matches = sorted(key for key in self.index if key.endswith("/" + path))
        return (matches[0] if len(matches) == 1 else None), matches

    def list(self, prefix: str = "", max_depth: int = 1) -> dict:
        resolved, suggestions = self.resolve(prefix)
        if resolved is None:
            return {"success": False, "suggestions": suggestions, "categories": []}
        depth = resolved.count("/") + 1 if resolved else 0
        categories = [
            {"path": key, "skills": len(names)}
            for key, names in sorted(self.index.items())
            if (not resolved or key.startswith(resolved + "/"))
            and depth < key.count("/") + 1 <= depth + max_depth
        ]
        return {"success": True, "prefix": resolved, "categories": categories}

    def find(self, taxonomy: str, limit: int = 20, offset: int = 0) -> dict:
        resolved, suggestions = self.resolve(taxonomy)
        if resolved is None:
            return {"success": False, "skills": [], "suggestions": suggestions}
        names = sorted(self.index[resolved] if resolved else self.skills)
        return {
            "success": True,
            "taxonomy": resolved,
            "total": len(names),
            "offset": offset,
            "limit": limit,
            "skills": [
                {"name": name, "description": self.skills[name].description[:300]}
                for name in names[offset : offset + limit]
            ],
        }

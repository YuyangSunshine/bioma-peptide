"""Exact stable identity and legacy path resolution, independent of taxonomy."""
from __future__ import annotations

import json
from pathlib import Path


class SkillLocationError(ValueError):
    pass


class SkillLocations:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        meta = self.root / '_meta'
        recorded_locations = json.loads(
            (meta / 'skill_locations.json').read_text(encoding='utf-8')
        )['skills']
        self.locations = {
            skill_id: value
            for skill_id, value in recorded_locations.items()
            if (self.root.parent / value['path'] / 'SKILL.md').is_file()
        }
        recorded_aliases = json.loads(
            (meta / 'aliases.json').read_text(encoding='utf-8')
        )['aliases']
        self.aliases = {
            alias: target
            for alias, target in recorded_aliases.items()
            if target in self.locations
        }
        recorded_legacy = json.loads(
            (meta / 'legacy_paths.json').read_text(encoding='utf-8')
        )['references']
        self.legacy = {
            reference: target
            for reference, target in recorded_legacy.items()
            if target in self.locations or target in self.aliases
        }
        self.by_path = {v['path']: k for k, v in self.locations.items()}
        if len(self.by_path) != len(self.locations):
            raise SkillLocationError('Duplicate physical package location')
        for sid in self.locations:
            self.get_current_skill_path(sid)
        for alias in self.aliases:
            self.resolve_skill_id(alias)

    def resolve_skill_id(self, reference: str) -> str | None:
        reference = reference.replace('\\', '/')
        sid = reference if reference in self.locations or reference in self.aliases else self.legacy.get(reference)
        if sid is None:
            path = reference.removesuffix('/SKILL.md')
            sid = self.by_path.get(path)
        seen = set()
        while sid in self.aliases:
            if sid in seen:
                raise SkillLocationError(f'Alias cycle at {sid}')
            seen.add(sid)
            sid = self.aliases[sid]
        if sid is not None and sid not in self.locations:
            raise SkillLocationError(f'Dangling skill identity: {sid}')
        return sid

    def resolve_legacy_path(self, reference: str) -> str | None:
        return self.resolve_skill_id(reference)

    def get_current_skill_path(self, reference: str) -> Path | None:
        sid = self.resolve_skill_id(reference)
        if sid is None:
            return None
        relative = Path(self.locations[sid]['path'])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'skills':
            raise SkillLocationError(f'Invalid skill location: {relative}')
        path = (self.root.parent / relative).resolve()
        if not path.is_relative_to(self.root):
            raise SkillLocationError(f'Skill location escapes root: {relative}')
        return path

    def resolve_resource_path(self, reference: str) -> Path | None:
        """Resolve an owned file below an exact historical package prefix."""
        reference = reference.replace('\\', '/')
        for old in sorted(self.legacy, key=len, reverse=True):
            if not old.startswith('skills/') or old.endswith('/SKILL.md'):
                continue
            if reference == old or reference.startswith(old + '/'):
                package = self.get_current_skill_path(self.legacy[old])
                suffix = reference[len(old):].lstrip('/')
                target = (package / suffix).resolve()
                if not target.is_relative_to(package):
                    raise SkillLocationError('Resource path escapes package')
                return target
        return None


def resolve_skill_id(reference: str, *, root: Path) -> str | None:
    return SkillLocations(root).resolve_skill_id(reference)


def resolve_legacy_path(reference: str, *, root: Path) -> str | None:
    return SkillLocations(root).resolve_legacy_path(reference)


def get_current_skill_path(reference: str, *, root: Path) -> Path | None:
    return SkillLocations(root).get_current_skill_path(reference)


def location_root(root: Path) -> Path | None:
    return next((p for p in (root, *root.parents) if (p / '_meta/skill_locations.json').is_file()), None)


def package_paths(root: Path, locations: SkillLocations | None = None) -> list[Path]:
    """Preserve legacy provider-root scans without duplicating directories."""
    base = location_root(root)
    if base is not None and root.parent == base and root.name != '_meta':
        locations = locations or SkillLocations(base)
        if root.name in {value['source_namespace'] for value in locations.locations.values()}:
            return sorted(locations.get_current_skill_path(sid) for sid, value in locations.locations.items()
                          if value['runtime'] and value['source_namespace'] == root.name)
    return sorted(p.parent for p in root.rglob('SKILL.md')
                  if not any(part.startswith(('_', '.')) for part in p.relative_to(root).parts))

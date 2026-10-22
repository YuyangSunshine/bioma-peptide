from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from bioma.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Load explicit tool modules; scientific dependencies stay inside tool calls."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [root.resolve() for root in roots]
        self._tools: dict[str, Tool] = {}
        self.load_errors: dict[str, str] = {}
        self.generation = 0

    def load(self) -> int:
        self._load_paths(
            [
                path
                for root in self.roots
                for path in sorted(root.glob("*.py"))
                if not path.name.startswith("_")
            ]
        )
        return len(self._tools)

    def load_skills(self, roots: list[Path]) -> int:
        from bioma.skills.locations import location_root, package_paths
        # Bundled wrappers import shared helpers through the skills namespace.
        namespace = ModuleType("skills")
        namespace.__path__ = list(dict.fromkeys(str(location_root(root.resolve()) or root.resolve()) for root in roots))
        sys.modules["skills"] = namespace
        self._load_paths(
            [
                package / 'scripts/bioma_tools.py'
                for root in roots
                for package in package_paths(root.resolve())
                if (package / 'scripts/bioma_tools.py').is_file()
            ]
        )
        return len(self._tools)

    def _load_paths(self, paths: list[Path]) -> None:
        for path in paths:
            module_name = (
                "bioma_external_tools_"
                + hashlib.sha256(str(path.resolve()).encode() + path.read_bytes()).hexdigest()[:16]
            )
            try:
                module = sys.modules.get(module_name)
                if module is None:
                    spec = importlib.util.spec_from_file_location(module_name, path)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    try:
                        spec.loader.exec_module(module)
                    except Exception:
                        sys.modules.pop(module_name, None)
                        raise
                for value in vars(module).values():
                    if callable(value) and hasattr(value, "_bioma_tool"):
                        self.add(value._bioma_tool)
            except Exception as exc:
                self.load_errors[str(path)] = str(exc)
                logger.warning("Could not load tool module %s: %s", path, exc)

    def add(self, tool: Tool) -> None:
        existing = self._tools.get(tool.name)
        if existing is not None and existing != tool:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        if existing is None:
            self.generation += 1

    def remove(self, name: str) -> None:
        """Revoke a tool immediately. Cached loops are fenced by admission."""
        if self._tools.pop(name, None) is not None:
            self.generation += 1

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> tuple[Tool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

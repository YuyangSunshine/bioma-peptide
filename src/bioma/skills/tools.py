from __future__ import annotations

from pydantic import Field

from bioma.skills.registry import SkillRegistry
from bioma.skills.taxonomy import TaxonomyRegistry
from bioma.tools.base import Tool, ToolInput


class TaxonomyListInput(ToolInput):
    prefix: str = ""
    max_depth: int = Field(default=1, ge=1, le=6)


class TaxonomySkillsInput(ToolInput):
    taxonomy: str
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class SkillViewInput(ToolInput):
    name: str = Field(description='Copy the exact name or stable_skill_id returned by skills_list or taxonomy_skills; never invent a skill name.')
    file_path: str | None = Field(default=None, description='Omit or set null to read the main skill. Only supply an exact linked relative file path returned by a prior successful skill_view. Do not construct a path from the skill name or taxonomy.')
    max_chars: int = Field(default=8000, ge=1, le=100_000)


class SkillSearchInput(ToolInput):
    question: str = ""
    limit: int = Field(default=20, ge=1, le=200)


def build_skill_tools(
    registry: SkillRegistry, taxonomy: TaxonomyRegistry
) -> tuple[Tool, ...]:
    definitions = (
        (
            "taxonomy_list",
            "Browse skill categories; copy complete paths from the result.",
            taxonomy.list,
            TaxonomyListInput,
        ),
        (
            "taxonomy_skills",
            "List descriptions in one category; use offset for the next page.",
            taxonomy.find,
            TaxonomySkillsInput,
        ),
        (
            "skill_view",
            "Read a discovered skill or its linked file; expose declared tools on the next turn.",
            registry.view,
            SkillViewInput,
        ),
    )
    return tuple(
        Tool(
            name=name,
            description=description,
            category="skills",
            version="1.0.0",
            author="BioMA",
            func=func,
            args_schema=schema,
            tags=("skills",),
        )
        for name, description, func, schema in definitions
    )


def build_skill_search_tool(registry: SkillRegistry) -> Tool:
    """Search complements taxonomy browsing for scientific task discovery."""
    return Tool(
        name="skills_list",
        description="Search the full skill registry by scientific question.",
        category="skills",
        version="1.0.0",
        author="BioMA",
        func=registry.list,
        args_schema=SkillSearchInput,
        tags=("skills",),
    )

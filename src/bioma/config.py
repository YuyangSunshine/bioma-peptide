from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(ConfigModel):
    backend: Literal["openai"] = "openai"
    model: str
    temperature: float | None = None
    thinking_mode: Literal['enabled', 'disabled'] | None = Field(default=None,
        description='Explicit provider thinking.type override when supported; unset preserves provider defaults.')
    max_output_tokens: int = Field(default=4096, ge=1)
    max_output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    timeout_seconds: float = Field(default=180.0, gt=0)
    api_base: str | None = None
    api_key_env: str = "BAIDU_API_KEY"

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)


class FusionMemberConfig(ConfigModel):
    name: str
    llm: LLMConfig


class GatewayConfig(ConfigModel):
    llm: LLMConfig
    max_interpretation_attempts: int = Field(default=2, ge=1, le=2)


class StepFusionConfig(ConfigModel):
    reference_models: list[FusionMemberConfig]
    coordinator: FusionMemberConfig
    reference_timeout_seconds: float = Field(default=180.0, gt=0, allow_inf_nan=False)
    max_reference_workers: int = Field(default=8, ge=1)
    reference_total_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    reference_max_calls: int = Field(default=4, ge=0)
    reference_max_output_tokens: int = Field(default=4096, ge=0)
    coordinator_reserve_seconds: float = Field(default=45.0, ge=0, allow_inf_nan=False)
    reference_interval_steps: int = Field(default=4, ge=1)


class PredAgentConfig(ConfigModel):
    step_fusion: StepFusionConfig
    max_steps: int = Field(default=50, ge=1)
    max_tool_calls: int = Field(default=40, ge=0)
    taxonomy_path: str = "skills/bio-skills-curated.taxonomy.yaml"
    skill_roots: list[str]
    tool_roots: list[str]


class ScientificInvestigationConfig(ConfigModel):
    harness_mode: Literal["current", "loop", "skills", "governed"] = "governed"
    agent_engine: Literal["bioma", "mini_swe"] = "bioma"
    llm: LLMConfig
    delivery_review_llm: LLMConfig | None = Field(default=None,
        description='Optional dedicated structured consistency reviewer; shares the existing call and task budgets.')
    max_llm_calls: int = Field(default=24, ge=1)
    max_tool_calls: int = Field(default=32, ge=1)
    max_retry_per_step: int = Field(default=2, ge=0)


class ComAgentConfig(ScientificInvestigationConfig):
    """Legacy spelling for the shared computation/review budget configuration."""


class ComputeServicesConfig(ConfigModel):
    """Configuration of bounded computation and independent assessment services."""
    worker: ScientificInvestigationConfig
    proposal_llm: LLMConfig
    assessment_llm: LLMConfig
    reference_models: list[FusionMemberConfig] = Field(default_factory=list)
    skill_roots: list[str] = Field(default_factory=list)
    tool_roots: list[str] = Field(default_factory=list)


class ProtocolDesignConfig(ConfigModel):
    llm: LLMConfig
    max_rectifications: int = Field(default=3, ge=0)


class AutomationConfig(ConfigModel):
    llm: LLMConfig
    max_rectifications: int = Field(default=3, ge=0)
    allow_simulation: bool = False
    simulation_journal: str = '.bioma/virtual-instruments.sqlite3'


class BioMAConfig(ConfigModel):
    base_dir: Path
    gateway: GatewayConfig
    predagent: PredAgentConfig | None = None
    proagent: ProtocolDesignConfig
    operagent: AutomationConfig
    comagent: ComAgentConfig | None = None
    compute_services: ComputeServicesConfig | None = None
    continuous_investigation: bool = True
    scientific_compute: ScientificInvestigationConfig | None = None

    def resolved_compute_services(self) -> ComputeServicesConfig:
        """Resolve old configuration spellings once at the composition boundary.

        Explicit service configuration is authoritative. No backend reads Role
        configuration or inherits Role scheduling/completion semantics.
        """
        if self.compute_services is not None:
            if self.scientific_compute is not None:
                raise ValueError('Use compute_services.worker or scientific_compute, not both')
            return self.compute_services.model_copy(deep=True)
        if self.predagent is None or self.comagent is None:
            raise ValueError('Configure compute_services; legacy input requires both predagent and comagent')
        return ComputeServicesConfig(
            worker=self.scientific_compute or self.comagent,
            proposal_llm=self.predagent.step_fusion.coordinator.llm,
            assessment_llm=self.comagent.llm,
            reference_models=self.predagent.step_fusion.reference_models,
            skill_roots=self.predagent.skill_roots, tool_roots=self.predagent.tool_roots)

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate.resolve() if candidate.is_absolute() else (self.base_dir / candidate).resolve()


def load_config(path: str | Path | None = None) -> BioMAConfig:
    config_path = Path(path).resolve() if path else _default_config_path()
    base_dir = config_path.parent.parent
    # Shared credentials are opt-in; never search sibling worktrees for secrets.
    env_file = os.environ.get("BIOMA_ENV_FILE")
    if env_file:
        explicit_env = Path(env_file).expanduser().resolve()
        if not explicit_env.is_file():
            raise FileNotFoundError("BIOMA_ENV_FILE must name an existing file")
        load_dotenv(explicit_env, override=False)
    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(base_dir / ".env", override=False)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    payload["base_dir"] = base_dir
    return BioMAConfig.model_validate(payload)


def _default_config_path() -> Path:
    source = Path(__file__).resolve().parents[2] / "configs" / "bioma.yaml"
    if source.is_file():
        return source
    return Path(str(files("bioma_resources").joinpath("configs/bioma.yaml")))

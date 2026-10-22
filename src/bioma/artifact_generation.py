from __future__ import annotations

from typing import Literal

from pydantic import Field

from bioma.config import LLMConfig
from bioma.core.models import StrictModel


class ArtifactGenerationPolicy(StrictModel):
    """Frozen model-execution policy for one artifact-production stage."""

    model: Literal["deepseek-v4.1-flash"] = "deepseek-v4.1-flash"
    thinking_mode: Literal["disabled"] = "disabled"
    max_output_tokens: int = Field(default=8192, ge=1)
    max_generation_attempts: int = Field(default=1, ge=1, le=1)
    timeout_seconds: float = Field(default=180.0, gt=0)
    provider_transport_retries: int = Field(default=0, ge=0, le=0)

    def provider_config(self, base: LLMConfig) -> LLMConfig:
        return base.model_copy(update={
            "model": self.model,
            "thinking_mode": self.thinking_mode,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
        })

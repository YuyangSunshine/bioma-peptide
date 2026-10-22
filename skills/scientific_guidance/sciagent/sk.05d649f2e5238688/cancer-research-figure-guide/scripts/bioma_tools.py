"""Cancer Research figure validation extracted from the Skill examples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class CancerFigureInput(ToolInput):
    image_path: str = Field(description="Raster figure to validate")
    content_type: Literal["line_art", "combination", "color_photo"] = Field(default="color_photo", description="Figure content type")
    column_width: Literal["single", "double"] = Field(default="double", description="Intended journal column width")


@bioma_tool(
    name="sciagent_validate_cancer_research_figure",
    description="Validate a raster figure against Cancer Research format, DPI, color-mode, and column-width requirements.",
    category="scientific-publishing",
    author="SciAgent",
    args_schema=CancerFigureInput,
)
def validate_cancer_research_figure(image_path: str, content_type: str = "color_photo",
                                    column_width: str = "double") -> str:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install Pillow") from exc
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    minimum_dpi = {"line_art": 1200, "combination": 600, "color_photo": 300}[content_type]
    target_width = {"single": 3.25, "double": 6.75}[column_width]
    allowed = {".tif", ".tiff", ".eps", ".ai"}
    with Image.open(path) as image:
        dpi = image.info.get("dpi", (0, 0))
        dpi_x = float(dpi[0] or 0)
        dpi_y = float(dpi[1] or 0)
        width_in = image.width / dpi_x if dpi_x else None
        checks = {
            "allowed_format": path.suffix.lower() in allowed,
            "minimum_dpi": min(dpi_x, dpi_y) >= minimum_dpi,
            "sufficient_pixel_width": image.width >= minimum_dpi * target_width,
            "supported_color_mode": image.mode in {"1", "L", "RGB", "CMYK"},
        }
        result = {"path": str(path), "format": image.format, "mode": image.mode,
                  "pixels": [image.width, image.height], "dpi": [dpi_x, dpi_y],
                  "estimated_width_in": width_in, "required_dpi": minimum_dpi,
                  "target_width_in": target_width, "checks": checks, "passed": all(checks.values())}
    return json.dumps(result, ensure_ascii=False)

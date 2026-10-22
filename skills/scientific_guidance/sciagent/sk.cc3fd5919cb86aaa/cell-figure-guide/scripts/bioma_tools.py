"""Cell Press figure validation extracted from the Skill examples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class CellFigureInput(ToolInput):
    image_path: str = Field(description="Raster figure to validate")
    content_type: Literal["line_art", "combination", "color_photo"] = Field(default="color_photo", description="Figure content type")
    column_width: Literal["single", "double"] = Field(default="double", description="Intended Cell Press column width")


@bioma_tool(
    name="sciagent_validate_cell_figure",
    description="Validate a raster figure against Cell Press TIFF, resolution, RGB, and column-width requirements.",
    category="scientific-publishing",
    author="SciAgent",
    args_schema=CellFigureInput,
)
def validate_cell_figure(image_path: str, content_type: str = "color_photo",
                         column_width: str = "double") -> str:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install Pillow") from exc
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    minimum_dpi = {"line_art": 1000, "combination": 600, "color_photo": 300}[content_type]
    target_width = {"single": 3.35, "double": 6.85}[column_width]
    with Image.open(path) as image:
        dpi = image.info.get("dpi", (0, 0))
        dpi_x = float(dpi[0] or 0)
        dpi_y = float(dpi[1] or 0)
        checks = {
            "tiff_format": path.suffix.lower() in {".tif", ".tiff"},
            "minimum_dpi": min(dpi_x, dpi_y) >= minimum_dpi,
            "sufficient_pixel_width": image.width >= minimum_dpi * target_width,
            "rgb_color": content_type != "color_photo" or image.mode == "RGB",
        }
        result = {"path": str(path), "format": image.format, "mode": image.mode,
                  "pixels": [image.width, image.height], "dpi": [dpi_x, dpi_y],
                  "estimated_width_in": image.width / dpi_x if dpi_x else None,
                  "required_dpi": minimum_dpi, "target_width_in": target_width,
                  "checks": checks, "passed": all(checks.values())}
    return json.dumps(result, ensure_ascii=False)

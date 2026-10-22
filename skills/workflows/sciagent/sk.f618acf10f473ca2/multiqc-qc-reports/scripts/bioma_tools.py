"""Generate MultiQC reports from selected local result directories."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class MultiQcInput(ToolInput):
    input_paths: list[str] = Field(description="Files or directories scanned recursively")
    output_dir: str = Field(description="Report output directory")
    report_name: str = Field(default="multiqc_report", description="Report filename without extension")
    modules: list[str] = Field(default_factory=list, description="Optional module allow-list")
    ignore_patterns: list[str] = Field(default_factory=list, description="Glob patterns to ignore")
    config_path: str = Field(default="", description="Optional MultiQC YAML config")
    export_data: bool = Field(default=True, description="Create the parsed-data directory")
    data_format: Literal["tsv", "csv", "json", "yaml"] = Field(
        default="tsv", description="Parsed-data output format"
    )
    export_plots: bool = Field(default=False, description="Export static plot images")
    force: bool = Field(default=False, description="Overwrite an existing report")


@bioma_tool(
    name="sciagent_run_multiqc",
    description="Aggregate supported bioinformatics QC outputs into a MultiQC HTML report and data tables.",
    category="quality-control",
    author="SciAgent",
    args_schema=MultiQcInput,
)
def run_multiqc(
    input_paths: list[str],
    output_dir: str,
    report_name: str = "multiqc_report",
    modules: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
    config_path: str = "",
    export_data: bool = True,
    data_format: str = "tsv",
    export_plots: bool = False,
    force: bool = False,
) -> str:
    default = Path(sys.prefix).parent / "bioma_skills_batch4" / "bin" / "multiqc"
    executable = os.getenv("BIOMA_MULTIQC", "") or shutil.which("multiqc") or str(default)
    if not Path(executable).is_file():
        raise RuntimeError("Missing MultiQC executable; set BIOMA_MULTIQC")
    if not input_paths:
        raise ValueError("input_paths must not be empty")
    sources = []
    for value in input_paths:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        sources.append(str(path))
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    command = [executable, *sources, "--outdir", str(destination), "--filename", report_name]
    for module in modules or []:
        command.extend(["--module", module])
    for pattern in ignore_patterns or []:
        command.extend(["--ignore", pattern])
    if config_path:
        config = Path(config_path).expanduser().resolve()
        if not config.is_file():
            raise FileNotFoundError(config)
        command.extend(["--config", str(config)])
    if export_data:
        command.extend(["--data-dir", "--data-format", data_format])
    else:
        command.append("--no-data-dir")
    if export_plots:
        command.append("--export")
    if force:
        command.append("--force")
    process = subprocess.run(command, text=True, capture_output=True)
    if process.returncode:
        raise RuntimeError(f"MultiQC failed: {(process.stderr or process.stdout)[-8000:]}")
    outputs = sorted(str(path) for path in destination.glob(f"{report_name}*"))
    return json.dumps(
        {"command": command, "outputs": outputs, "stdout_tail": process.stdout[-4000:],
         "stderr_tail": process.stderr[-4000:]},
        ensure_ascii=False,
    )

"""Compile and inspect LaTeX research posters with local TeX utilities."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class LatexPosterInput(ToolInput):
    operation: Literal["compile", "quality_check"] = Field(description="Poster operation")
    source_path: str = Field(description="TeX source for compile or PDF file for quality_check")
    output_dir: str = Field(default="", description="Compilation output directory")
    engine: Literal["pdflatex", "lualatex", "xelatex"] = Field(
        default="pdflatex", description="LaTeX engine"
    )
    passes: int = Field(default=2, ge=1, le=4, description="Compilation passes")


def _run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"Command failed: {detail[-8000:]}")
    return process


@bioma_tool(
    name="sciagent_build_latex_poster",
    description="Compile a LaTeX poster or inspect its PDF page size, embedded fonts, and images.",
    category="scientific-communication",
    author="SciAgent",
    args_schema=LatexPosterInput,
)
def build_latex_poster(
    operation: str,
    source_path: str,
    output_dir: str = "",
    engine: str = "pdflatex",
    passes: int = 2,
) -> str:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if operation == "compile":
        executable = shutil.which(engine)
        if not executable:
            raise RuntimeError(f"Missing executable: {engine}")
        if source.suffix.lower() != ".tex":
            raise ValueError("source_path must be a .tex file for compile")
        destination = Path(output_dir).expanduser().resolve() if output_dir else source.parent
        destination.mkdir(parents=True, exist_ok=True)
        command = [
            executable,
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={destination}",
            source.name,
        ]
        logs = []
        for _ in range(passes):
            logs.append(_run(command, cwd=source.parent).stdout[-4000:])
        pdf = destination / f"{source.stem}.pdf"
        if not pdf.is_file():
            raise RuntimeError(f"Compilation did not create {pdf}")
        return json.dumps(
            {"operation": operation, "pdf": str(pdf), "engine": engine, "log_tail": logs[-1]},
            ensure_ascii=False,
        )

    if source.suffix.lower() != ".pdf":
        raise ValueError("source_path must be a PDF file for quality_check")
    checks: dict[str, str] = {}
    for name, arguments in {
        "page": ["pdfinfo", str(source)],
        "fonts": ["pdffonts", str(source)],
        "images": ["pdfimages", "-list", str(source)],
    }.items():
        executable = shutil.which(arguments[0])
        if not executable:
            checks[name] = f"missing executable: {arguments[0]}"
            continue
        arguments[0] = executable
        checks[name] = _run(arguments).stdout[-12000:]
    return json.dumps({"operation": operation, "pdf": str(source), "checks": checks}, ensure_ascii=False)

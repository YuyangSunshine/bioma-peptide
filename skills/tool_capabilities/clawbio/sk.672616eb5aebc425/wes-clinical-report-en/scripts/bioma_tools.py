"""BioAgent wrapper for English WES clinical reports."""
from pathlib import Path
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_directory, existing_file, output_directory, run_cli

SCRIPT = Path(__file__).resolve().parents[1] / "wes_clinical_report_en.py"

class WesEnglishReportInput(ToolInput):
    output_dir: str
    report_dir: str = ""
    samples: str = ""
    logo_left_path: str = ""
    logo_right_path: str = ""
    demo: bool = False

@bioma_tool(name="clawbio_build_wes_report_en", description="Generate English clinical PDF reports from WES Markdown results.", category="clinical-genomics", author="ClawBio", args_schema=WesEnglishReportInput)
def build_wes_report_en(output_dir: str, report_dir: str = "", samples: str = "", logo_left_path: str = "", logo_right_path: str = "", demo: bool = False) -> str:
    output = output_directory(output_dir)
    args = ["--output-dir", str(output)]
    if demo:
        args.append("--demo")
    elif report_dir:
        args += ["--report-dir", str(existing_directory(report_dir, "report_dir"))]
    else:
        raise ValueError("report_dir is required outside demo mode")
    if samples:
        args += ["--samples", samples]
    if logo_left_path:
        args += ["--logo-left", str(existing_file(logo_left_path, "logo_left_path"))]
    if logo_right_path:
        args += ["--logo-right", str(existing_file(logo_right_path, "logo_right_path"))]
    result = run_cli(SCRIPT, args, output, timeout=43200)
    if not any(output.glob("*.pdf")):
        raise RuntimeError("WES report generator completed without producing a PDF")
    return result

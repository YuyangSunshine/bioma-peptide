"""BioAgent wrapper for unified personal genomic profiles."""
from pathlib import Path
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory, run_cli

SCRIPT = Path(__file__).resolve().parents[1] / "profile_report.py"

class ProfileReportInput(ToolInput):
    output_dir: str
    profile_path: str = ""
    demo: bool = False

@bioma_tool(name="clawbio_build_genomic_profile", description="Synthesize completed personal-genomics Skill results from a PatientProfile JSON into one report.", category="personal-genomics", author="ClawBio", args_schema=ProfileReportInput)
def build_genomic_profile(output_dir: str, profile_path: str = "", demo: bool = False) -> str:
    output = output_directory(output_dir)
    args = ["--output", str(output)]
    if demo:
        args.append("--demo")
    elif profile_path:
        args += ["--profile", str(existing_file(profile_path, "profile_path"))]
    else:
        raise ValueError("profile_path is required outside demo mode")
    return run_cli(SCRIPT, args, output)

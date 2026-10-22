"""BioAgent wrapper for bcftools variant normalization."""
from pathlib import Path
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory, require_executable, run_cli
SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "normalize_vcf.sh"
class NormalizeVcfInput(ToolInput):
    reference_path: str
    input_vcf: str
    output_dir: str
@bioma_tool(name="openclaw_normalize_vcf", description="Left-align indels, split multiallelic records, remove exact duplicates, and index the normalized VCF.", category="variant-analysis", author="OpenClaw", args_schema=NormalizeVcfInput)
def normalize_vcf(reference_path: str, input_vcf: str, output_dir: str) -> str:
    require_executable("bcftools")
    output = output_directory(output_dir)
    return run_cli(SCRIPT, [str(existing_file(reference_path, "reference_path")), str(existing_file(input_vcf, "input_vcf")), str(output / "normalized.vcf.gz")], output)

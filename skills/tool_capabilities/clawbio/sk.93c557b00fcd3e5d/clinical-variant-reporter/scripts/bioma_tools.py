"""BioAgent wrapper for clinical variant reporting."""
from pathlib import Path
from typing import Literal
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory, run_cli

SCRIPT = Path(__file__).resolve().parents[1] / "clinical_variant_reporter.py"

class ClinicalVariantInput(ToolInput):
    output_dir: str
    input_vcf: str = ""
    demo: bool = False
    genes: list[str] = Field(default_factory=list)
    assembly: Literal["GRCh37","GRCh38"] = "GRCh38"

@bioma_tool(name="clawbio_report_clinical_variants", description="Generate ACMG-oriented clinical variant reports from VCF or BCF files.", category="clinical-genomics", author="ClawBio", args_schema=ClinicalVariantInput)
def report_clinical_variants(output_dir: str, input_vcf: str = "", demo: bool = False, genes: list[str] | None = None, assembly: str = "GRCh38") -> str:
    output=output_directory(output_dir); args=["--output",str(output),"--assembly",assembly]
    args.append("--demo") if demo else args.extend(["--input",str(existing_file(input_vcf,"input_vcf"))])
    if genes: args += ["--genes",",".join(genes)]
    return run_cli(SCRIPT,args,output)

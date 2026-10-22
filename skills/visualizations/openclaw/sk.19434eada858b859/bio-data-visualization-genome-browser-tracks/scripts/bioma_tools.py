"""BioAgent wrapper for pyGenomeTracks figures."""
from pathlib import Path
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory, require_executable
from skills._meta.support.openclaw._bioma_module import json_text, load_source
SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "generate_tracks.py"
class GenomeTracksInput(ToolInput):
    tracks_ini: str
    region: str
    output_dir: str
    filename: str = "genome_tracks.png"
    dpi: int = Field(default=300, ge=72, le=1200)
    width_cm: float = Field(default=30, gt=0, le=100)
@bioma_tool(name="openclaw_plot_genome_browser_tracks", description="Render an explicit genomic region from a pyGenomeTracks INI configuration into an isolated output directory.", category="genomics-visualization", author="OpenClaw", args_schema=GenomeTracksInput)
def plot_genome_browser_tracks(tracks_ini: str, region: str, output_dir: str, filename: str = "genome_tracks.png", dpi: int = 300, width_cm: float = 30) -> str:
    require_executable("pyGenomeTracks")
    if ":" not in region or "-" not in region: raise ValueError("region must use chr:start-end format")
    output = output_directory(output_dir)
    target = output / Path(filename).name
    module = load_source(SCRIPT, "openclaw_genome_tracks")
    if not module.generate_track_plot(region, str(target), str(existing_file(tracks_ini, "tracks_ini")), dpi, width_cm): raise RuntimeError("pyGenomeTracks failed")
    if not target.is_file(): raise RuntimeError("pyGenomeTracks did not create the requested output")
    return json_text({"output_file": str(target), "region": region})

"""AutoDock Vina execution extracted from the Skill workflow."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class VinaDockingInput(ToolInput):
    receptor_pdbqt: str = Field(description="Prepared receptor PDBQT file")
    ligand_pdbqt: str = Field(description="Prepared ligand PDBQT file")
    output_pdbqt: str = Field(description="Output docked-pose PDBQT file")
    center: list[float] = Field(description="Grid center [x, y, z] in Angstrom")
    box_size: list[float] = Field(default_factory=lambda: [20.0, 20.0, 20.0], description="Grid size [x, y, z] in Angstrom")
    exhaustiveness: int = Field(default=8, ge=1, le=128, description="Search exhaustiveness")
    n_poses: int = Field(default=9, ge=1, le=20, description="Number of poses to retain")
    seed: int = Field(default=0, ge=0, description="Vina random seed; 0 uses default")


@bioma_tool(
    name="sciagent_run_vina_docking",
    description="Dock a prepared PDBQT ligand into a prepared PDBQT receptor with AutoDock Vina.",
    category="cheminformatics",
    author="SciAgent",
    args_schema=VinaDockingInput,
)
def run_vina_docking(receptor_pdbqt: str, ligand_pdbqt: str, output_pdbqt: str,
                     center: list[float], box_size: list[float] | None = None,
                     exhaustiveness: int = 8, n_poses: int = 9, seed: int = 0) -> str:
    try:
        from vina import Vina
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install vina") from exc
    size = box_size or [20.0, 20.0, 20.0]
    if len(center) != 3 or len(size) != 3:
        raise ValueError("center and box_size must each contain exactly 3 values")
    receptor = Path(receptor_pdbqt).expanduser().resolve()
    ligand = Path(ligand_pdbqt).expanduser().resolve()
    if not receptor.is_file() or not ligand.is_file():
        raise FileNotFoundError("receptor_pdbqt and ligand_pdbqt must exist")
    output = Path(output_pdbqt).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    vina = Vina(sf_name="vina", seed=seed) if seed else Vina(sf_name="vina")
    vina.set_receptor(str(receptor))
    vina.set_ligand_from_file(str(ligand))
    vina.compute_vina_maps(center=center, box_size=size)
    vina.dock(exhaustiveness=exhaustiveness, n_poses=n_poses)
    energies = vina.energies(n_poses=n_poses)
    vina.write_poses(str(output), n_poses=n_poses, overwrite=True)
    return json.dumps({"output": str(output), "center": center, "box_size": size,
                       "energies": energies.tolist() if hasattr(energies, "tolist") else energies})

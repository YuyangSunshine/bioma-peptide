"""De novo protein-pocket detection with established local executables."""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError


class PocketDetectionInput(ToolInput):
    pdb_path: str = Field(description="Local PDB structure path.")
    method: Literal["fpocket", "p2rank"] = Field(
        default="fpocket",
        description="Installed pocket detector to run.",
    )


@bioma_tool(
    name="detect_binding_pockets",
    description=(
        "Detect and rank apo-protein pockets with an installed fpocket or P2Rank "
        "executable, returning pocket coordinates, residues, scores, and output paths."
    ),
    category="analysis",
    version="1.0.0",
    author="BioMA",
    args_schema=PocketDetectionInput,
    tags=["protein", "structure", "pocket", "fpocket", "p2rank"],
)
def detect_binding_pockets(pdb_path: str, method: Literal["fpocket", "p2rank"] = "fpocket") -> str:
    structure = Path(pdb_path).expanduser().resolve()
    if not structure.is_file():
        raise ToolError(
            f"PDB file not found: {structure}",
            error_type="invalid_input",
            retriable=False,
            backend=method,
        )
    executable = shutil.which("fpocket" if method == "fpocket" else "prank")
    if executable is None:
        raise ToolError(
            f"{method} is not installed or not on PATH.",
            error_type="unavailable",
            retriable=False,
            backend=method,
        )

    if method == "fpocket":
        subprocess.run([executable, "-f", str(structure)], check=True, capture_output=True, text=True)
        output_dir = structure.with_name(f"{structure.stem}_out")
        pockets = _fpocket_results(output_dir, structure.stem)
    else:
        output_dir = structure.with_name(f"{structure.stem}_p2rank")
        subprocess.run(
            [executable, "predict", "-f", str(structure), "-o", str(output_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        pockets = _p2rank_results(output_dir / f"{structure.name}_predictions.csv")

    return json.dumps(
        {
            "status": "ok",
            "method": method,
            "structure": str(structure),
            "output_dir": str(output_dir),
            "pockets": pockets,
        },
        ensure_ascii=False,
        indent=2,
    )


def _fpocket_results(output_dir: Path, stem: str) -> list[dict[str, object]]:
    info_path = output_dir / f"{stem}_info.txt"
    if not info_path.is_file():
        raise ToolError(
            "fpocket completed without producing its pocket summary.",
            error_type="invalid_response",
            retriable=False,
            backend="fpocket",
        )

    pockets: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw_line in info_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("Pocket "):
            current = {"rank": int(line.split()[1])}
            pockets.append(current)
        elif current is not None and ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            try:
                current[key] = float(value)
            except ValueError:
                current[key] = value

    for pocket in pockets:
        rank = pocket["rank"]
        atom_path = output_dir / "pockets" / f"pocket{rank}_atm.pdb"
        pocket.update(_pocket_geometry(atom_path))
        pocket["pdb_path"] = str(atom_path)
    return sorted(pockets, key=lambda item: float(item.get("Druggability Score", 0)), reverse=True)


def _pocket_geometry(path: Path) -> dict[str, object]:
    coordinates: list[tuple[float, float, float]] = []
    residues: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("ATOM  "):
                continue
            try:
                coordinates.append(
                    (float(line[30:38]), float(line[38:46]), float(line[46:54]))
                )
                residues.add(f"{line[17:20].strip()}{line[22:26].strip()}:{line[21].strip()}")
            except ValueError:
                continue
    if not coordinates:
        return {"center": None, "residues": []}
    count = len(coordinates)
    center = [round(sum(point[axis] for point in coordinates) / count, 3) for axis in range(3)]
    return {"center": center, "residues": sorted(residues)}


def _p2rank_results(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise ToolError(
            "P2Rank completed without producing its predictions CSV.",
            error_type="invalid_response",
            retriable=False,
            backend="p2rank",
        )
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            {key.strip(): value.strip() for key, value in row.items() if key is not None}
            for row in csv.DictReader(handle, skipinitialspace=True)
        ]
    return [
        {
            "rank": int(row["rank"]),
            "score": float(row["score"]),
            "probability": float(row["probability"]),
            "center": [float(row[key]) for key in ("center_x", "center_y", "center_z")],
            "residues": row.get("residue_ids", "").split(),
        }
        for row in rows
    ]

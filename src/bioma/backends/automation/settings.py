from __future__ import annotations

import json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REGISTRY_DIR = PACKAGE_DIR / "resources" / "registry"
INSTRUMENTS_PATH = REGISTRY_DIR / "instruments.csv"
CONSUMABLES_PATH = REGISTRY_DIR / "consumables.csv"
INSTRUMENTS_EN_PATH = REGISTRY_DIR / "instruments_en.json"
CONSUMABLES_EN_PATH = REGISTRY_DIR / "consumables_en.json"



def _available_resource_ids(path: Path) -> set[int]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(row["resourceId"])
        for row in rows
        if str(row.get("statusCode", "")).casefold() == "available"
    }


VALID_INSTRUMENT_IDS = _available_resource_ids(INSTRUMENTS_EN_PATH)
VALID_CONSUMABLE_IDS = _available_resource_ids(CONSUMABLES_EN_PATH)

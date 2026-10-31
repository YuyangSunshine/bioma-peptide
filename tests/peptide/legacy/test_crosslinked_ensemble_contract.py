from __future__ import annotations

import json

import pytest

from bioma.tools.errors import ToolError
from tools.our_tools._crosslinked_impl.ensemble_contract import require_final_target


def test_contract_rejects_an_intermediate_pool_as_final_target(tmp_path, monkeypatch) -> None:
    contract = tmp_path / "workflow_contract.json"
    contract.write_text(json.dumps({"final_ensemble_size": 10}), encoding="utf-8")
    monkeypatch.setenv("BIOMA_CROSSLINKED_ENSEMBLE_CONTRACT", str(contract))

    assert require_final_target(10, stage="select_for_normal") is not None
    with pytest.raises(ToolError, match="trusted run contract"):
        require_final_target(64, stage="select_for_normal")

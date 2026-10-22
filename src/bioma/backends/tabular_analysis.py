"""Generic, reproducible analysis of a governed tabular WorkResult."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bioma.core.models import Artifact
from bioma.core.tabular import sha256_file, summarize_csv
from bioma.core.scientific_capabilities import COMPUTE, WorkResult


def generic_tabular_analysis(base_dir: Path):
    def execute(state, message, tools):
        del tools
        inputs = message.payload.get("inputs") or {}
        refs = set(message.payload.get("input_refs", inputs.get("input_refs", [])))
        candidates = []
        for observation in state.observations:
            if observation.id not in refs or observation.kind != "work_result":
                continue
            work = WorkResult.model_validate(observation.value)
            if work.provenance.get("external_import", {}).get("qualification_status") != "independently_validated":
                continue
            for artifact in work.artifacts:
                uri = Path(str(artifact.get("uri", "")))
                if uri.suffix.casefold() == ".csv":
                    candidates.append((observation, work, artifact, uri))
        from bioma.core.source_inputs import verify_source_identities
        for source_input in verify_source_identities(inputs.get('source_inputs', [])):
            if source_input.id in refs and Path(source_input.uri).suffix.casefold() == '.csv':
                candidates.append((source_input, None, {'metadata': {'sha256':source_input.sha256}}, Path(source_input.uri)))
        if len(candidates) != 1:
            raise ValueError("generic analysis requires exactly one referenced, imported, verified CSV WorkResult")
        source, upstream, artifact, csv_path = candidates[0]
        expected_hash = artifact.get("metadata", {}).get("sha256")
        if not csv_path.is_file() or sha256_file(csv_path) != expected_hash:
            raise ValueError("upstream tabular artifact is inaccessible or changed")
        root = Path(base_dir) / "artifacts" / "derived" / state.task_id / "generic-tabular-analysis"
        root.mkdir(parents=True, exist_ok=True)
        code_path, summary_path, receipt_path = root / "analysis.py", root / "summary.json", root / "execution-receipt.json"
        code = ('from pathlib import Path\nimport json,sys\n'
                'from bioma.core.tabular import summarize_csv\n'
                'Path(sys.argv[2]).write_text(json.dumps(summarize_csv(Path(sys.argv[1])), '
                'sort_keys=True, indent=2), encoding="utf-8")\n')
        code_path.write_text(code, encoding="utf-8")
        command = [sys.executable, str(code_path), str(csv_path), str(summary_path)]
        # The analysis directory need not be the package checkout. Pin the
        # executing installation, rather than resolving a relative PYTHONPATH
        # against a user workspace or silently loading another BioMA version.
        python_path = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]),
                                                   os.environ.get('PYTHONPATH')]))
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False,
                                   env={**os.environ, 'PYTHONPATH':python_path})
        receipt = {"schema_version": "python-analysis-receipt-v1", "command": ["python", "analysis.py", csv_path.name, summary_path.name],
            "python_version": sys.version, "exit_code": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr,
            "input_sha256": expected_hash, "code_sha256": sha256_file(code_path)}
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2), encoding="utf-8")
        if completed.returncode or not summary_path.is_file():
            raise ValueError("generic Python analysis failed")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary_hash = sha256_file(summary_path)
        artifacts = [Artifact(kind=kind, uri=str(path), source="scientific-compute",
            metadata={"sha256": sha256_file(path),
                      ("upstream_work_result_ref" if upstream else "source_input_ref"): source.id})
            for kind, path in (("analysis-code-python", code_path), ("analysis-summary-json", summary_path),
                               ("analysis-execution-receipt", receipt_path))]
        return WorkResult(family=COMPUTE, kind="AnalysisResult", input_refs=[source.id],
            payload={"summary": summary, "summary_sha256": summary_hash,
                "source_csv_uri": str(csv_path), "source_csv_sha256": expected_hash,
                "score_direction": "unverified", "upstream_kind": upstream.kind if upstream else "user_supplied_input"},
            artifacts=[row.model_dump(mode="json") for row in artifacts],
            provenance={"method": "verified_tabular_analysis", "runtime": "python-subprocess",
                **({"source_work_result_ref": source.id, "source_work_result_identity": upstream.model_dump(mode="json")}
                   if upstream else {"source_input_ref": source.id, "source_identity": source.model_dump(mode="json")}),
                "acceptance_input_hash": inputs.get("_acceptance_input_hash"),
                "execution_receipt_sha256": sha256_file(receipt_path)})
    return execute

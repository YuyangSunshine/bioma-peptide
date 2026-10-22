"""Read-only import boundary for previously governed scientific WorkResults."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from bioma.core.models import ExternalWorkResultReference, Observation
from bioma.core.research import fingerprint
from bioma.core.scientific_capabilities import WorkResult
from bioma.runtime.ledger import RunLedger


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_file(uri: str, expected: str, label: str) -> Path:
    path = Path(uri).resolve()
    if not path.is_file():
        raise ValueError(f"{label} is not accessible")
    if _sha256(path) != expected:
        raise ValueError(f"{label} hash mismatch")
    return path


def import_external_work_result(reference: ExternalWorkResultReference) -> Observation:
    """Verify source journal, receipt, identity, artifact and independent validation."""
    ledger_path = _require_file(reference.source_ledger, reference.source_ledger_sha256,
                                "source ledger")
    # The reference hashes one immutable SQLite file, not mutable WAL sidecars.
    wal = Path(str(ledger_path) + '-wal')
    if wal.is_file() and wal.stat().st_size:
        raise ValueError('Source ledger requires a standalone snapshot; WAL is not covered by its hash')
    artifact_path = _require_file(reference.artifact_uri, reference.artifact_sha256,
                                  "WorkResult artifact")
    validation_path = _require_file(reference.validation_uri, reference.validation_sha256,
                                    "independent validation")
    ledger = RunLedger(ledger_path, read_only=True)
    if not ledger.verify_chain(reference.source_task_id):
        raise ValueError("source ledger chain is invalid")
    events = ledger.events(reference.source_task_id)
    if not any(row.kind == "ExecutionPermitted" for row in events):
        raise ValueError("source WorkResult has no WP1 permit")
    receipts = [row for row in events if row.kind == "ExecutionResultRecorded"]
    if not receipts:
        raise ValueError("source WorkResult has no execution receipt")
    receipt_refs = {"result:" + str(row.payload.get("proposal_id")) + ":" +
                    str(row.payload.get("attempt", 1)) for row in receipts}
    if reference.execution_receipt_ref not in receipt_refs:
        raise ValueError("execution receipt identity mismatch")
    state = ledger.replay_state(reference.source_task_id)
    if state is None or state.status != "completed":
        raise ValueError("source task is not a completed governed execution")
    observation = next((row for row in state.observations
                        if row.id == reference.work_result_id and row.kind == "work_result"), None)
    if observation is None:
        raise ValueError("WorkResult identity does not exist in the source task")
    work = WorkResult.model_validate(observation.value)
    provenance, payload = work.provenance, work.payload
    # Bind this exact result to its own successful, journaled WP1 execution.
    bound = []
    for event in events:
        if event.kind != "TransitionCommitted":
            continue
        proof = event.payload['delta'].get('transition') or {}
        result, owner = proof.get('result', {}), proof.get('owner_delta', {})
        identity = f"result:{result.get('proposal_id')}:{result.get('attempt', 1)}"
        if (identity == reference.execution_receipt_ref
                and result.get('execution_status') == 'completed'
                and result.get('work_outcome') == 'completed' and owner.get('outcome') == 'completed'
                and observation.model_dump(mode='json') in owner.get('updates', {}).get('observations', [])):
            if not all(any(e.kind == kind and e.payload == proof.get(key) for e in events)
                       for kind, key in (('ExecutionPermitted','permit'),('ExecutionResultRecorded','result'))):
                raise ValueError('Source WorkResult lacks journaled permit or receipt')
            bound.append(result)
    if len(bound) != 1:
        raise ValueError('WorkResult is not bound to the specified successful execution receipt identity')
    remote = (reference.remote_job_id, reference.model_name, reference.model_version)
    if any(remote):
        if not all(remote):
            raise ValueError('Remote model identity must be complete')
        expected = {'remote_job_id':reference.remote_job_id, 'model_name':reference.model_name,
                    'input_sha256':reference.input_sha256}
        for key, value in expected.items():
            if provenance.get(key, payload.get(key)) != value:
                raise ValueError(f'WorkResult {key} identity mismatch')
        if (payload.get('parameters', {}).get('Model') or payload.get('model_version')) != reference.model_version:
            raise ValueError('WorkResult model_version identity mismatch')
    else:
        initial = next((e for e in events if e.kind == 'RunInitialized'), None)
        if initial is None or fingerprint(initial.payload['task']['inputs']) != reference.input_sha256:
            raise ValueError('WorkResult input identity mismatch')
    artifacts = [row for row in work.artifacts if Path(str(row.get("uri"))).resolve() == artifact_path
                 and row.get("metadata", {}).get("sha256") == reference.artifact_sha256]
    if not artifacts:
        raise ValueError("artifact is not bound to the source WorkResult")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if any(validation.get(key) != getattr(reference, key) for key in
           ('source_task_id','work_result_id','execution_receipt_ref')):
        raise ValueError('independent validation identity mismatch')
    qualified = next((e for e in state.evidence if e.id == validation.get('qualification_evidence_id')), None)
    if (qualified is None or qualified.source != 'harness' or not isinstance(qualified.value, dict)
            or qualified.value.get('work_result_ref') != observation.id
            or qualified.value.get('execution_result_identity') != fingerprint(bound[0])
            or not any(a['id'] in qualified.artifact_ids for a in artifacts)):
        raise ValueError('independent validation lacks bound committed qualification')
    if not any(row.get("sha256") == reference.artifact_sha256 and
               Path(str(row.get("uri"))).resolve() == artifact_path
               for row in validation.get("artifacts", [])):
        raise ValueError("independent validation does not bind the artifact")
    if artifact_path.suffix.casefold() == ".csv":
        with artifact_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        declared_rows = validation.get("result_validation", {}).get("rows")
        if declared_rows is not None and len(rows) != declared_rows:
            raise ValueError("independent validation row count mismatch")
    identity = fingerprint({"reference": reference.model_dump(mode="json"),
                            "work_result": work.model_dump(mode="json")})
    imported = work.model_copy(update={"artifacts": artifacts, "provenance": {**work.provenance,
        "external_import": {"source_task_id": reference.source_task_id,
            "source_work_result_id": reference.work_result_id,
            "source_ledger_sha256": reference.source_ledger_sha256,
            "execution_receipt_ref": reference.execution_receipt_ref,
            "validation_sha256": reference.validation_sha256,
            "access_scope": reference.access_scope,
            "qualification_status": reference.qualification_status}}})
    _require_file(reference.source_ledger, reference.source_ledger_sha256, "source ledger")
    return Observation(id=str(uuid5(NAMESPACE_URL, "external-work-result:" + identity)),
        source="external-work-result-boundary", kind="work_result",
        value=imported.model_dump(mode="json"), method="verified_external_work_result_import",
        artifact_ids=[a["id"] for a in artifacts], created_at=observation.created_at)


def import_external_work_results(references):
    references = [ExternalWorkResultReference.model_validate(row) for row in references]
    identities = {(r.source_ledger_sha256, r.source_task_id, r.work_result_id, r.artifact_sha256) for r in references}
    if len(identities) != len(references):
        raise ValueError('duplicate external WorkResult reference')
    rows = [import_external_work_result(row) for row in references]
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("duplicate external WorkResult reference")
    return rows

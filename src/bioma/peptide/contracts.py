"""Fail-closed result contracts. Execution feedback never grants scientific approval."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

TOOLS = {
    "build_crosslinked_peptide_topology": "peptide_structure",
    "generate_rdkit_conformer_ensemble": "small_molecule",
    "generate_boltz_conformer_ensemble": "boltz_structure",
    "optimize_conformer_ensemble_with_xtb": "small_molecule",
    "validate_cluster_select_conformers": "peptide_structure",
    "export_structure_ensemble": "structure_export",
    "prepare_receptor": "tbmb_bicyclic_docking",
    "detect_pockets": "tbmb_bicyclic_docking",
    "diffdock_screen": "tbmb_bicyclic_docking",
    "vina_refine": "tbmb_bicyclic_docking",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def confined(root, relative):
    root = Path(root).resolve(strict=True)
    p = Path(relative)
    if p.is_absolute() or not p.parts or ".." in p.parts:
        raise ValueError("path_outside_registered_root")
    current = root
    for part in p.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("symlink_rejected")
    current.resolve(strict=True).relative_to(root)
    if not current.is_file():
        raise ValueError("not_regular_file")
    return current


def artifact_reference(root, relative, *, job_id, identity, retention="retain_until_review"):
    p = confined(root, relative)
    checksum = sha256(p)
    return {"uri": p.as_uri(), "relative_path": str(relative), "size": p.stat().st_size,
            "sha256": checksum, "immutable_version": "sha256:" + checksum,
            "producer_job": job_id, "identity": identity, "retention_policy": retention,
            "storage": "external", "media_encoding": "identity"}


def verify_reference(root, ref, identity):
    p = confined(root, ref["relative_path"])
    if (ref["identity"] != identity or ref["sha256"] != sha256(p)
            or ref["size"] != p.stat().st_size or ref["uri"] != p.as_uri()
            or ref["immutable_version"] != "sha256:" + ref["sha256"]):
        raise ValueError("artifact_identity_mismatch")
    return p


def execution_status(stage):
    if stage in {"ok", "success", "completed"}:
        return "ok"
    if stage == "partial":
        return "partial_success"
    if stage in {"partial_success", "interrupted", "blocked", "failed", "cancelled"}:
        return stage
    if isinstance(stage, str) and stage.startswith("failed_"):
        return "failed"
    if isinstance(stage, str) and stage.startswith("blocked_"):
        return "blocked"
    return "failed"


def normalize(raw, *, tool, task_id, job_id, identity, method_version, root,
              artifacts=(), candidates=(), validator=None):
    """validator is trusted controller code, never a tool/Agent-supplied verdict.

    It receives the verified file references and expected identities and returns
    an independently checked candidate list and a pinned validator version.
    """
    reasons = []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("result_must_be_object")
    except (ValueError, TypeError) as exc:
        data = {}; reasons.append(str(exc))
    stage = data.get("stage_status", data.get("status"))
    status = execution_status(stage)
    if stage is None or (status == "failed" and not str(stage).startswith("failed")):
        reasons.append("unknown_stage_status")
    refs = list(artifacts)
    engineering = "not_run"
    scientific = "not_run"
    validation = {"validator_version": None, "passed_candidates": [], "failed_candidates": []}
    try:
        if not refs:
            raise ValueError("missing_artifact_manifest")
        for ref in refs:
            verify_reference(root, ref, identity)
        # Cross-check the stage manifest against the returned identities.
        if data.get('manifest_path'):
            manifest_path = Path(data['manifest_path'])
            relative = manifest_path.relative_to(Path(root).resolve()) if manifest_path.is_absolute() else manifest_path
            manifest_path = confined(root, relative)
            if str(relative) not in {r['relative_path'] for r in refs} or manifest_path.stat().st_size > 2*1024*1024:
                raise ValueError('unbound_or_oversized_stage_manifest')
            manifest = json.loads(manifest_path.read_text())
            for field in ('chemical_hash', 'config_hash', 'status'):
                if field in data and manifest.get(field) != data[field]:
                    raise ValueError('stage_manifest_identity_mismatch')
        for field in ('chemical_hash', 'input_hash', 'config_hash'):
            if field in identity and data.get(field) != identity[field]:
                raise ValueError('expected_stage_identity_mismatch')
        engineering = "passed"
    except (ValueError, KeyError, OSError) as exc:
        engineering = "failed"; reasons.append(str(exc))
    if engineering == "passed" and status in {"ok", "partial_success"} and validator:
        try:
            validation = validator(root, refs, identity)
            if not validation.get("validator_version"):
                raise ValueError("unpinned_validator")
            passed = validation.get("passed_candidates", [])
            failed = validation.get("failed_candidates", [])
            expected = set(candidates)
            if (set(passed) - expected or len(set(passed)) != len(passed)
                    or not passed or set(passed) & {x['candidate_id'] for x in failed}):
                raise ValueError("invalid_candidate_validation")
            scientific = "passed" if set(passed) == expected and not failed else "partial_success"
        except (ValueError, KeyError, OSError) as exc:
            scientific = "failed"; reasons.append(str(exc))
    approved = validation.get("passed_candidates", []) if scientific in {"passed", "partial_success"} else []
    missing = sorted(set(candidates) - set(approved))
    deliverable = status in {"ok", "partial_success"} and engineering == "passed" and bool(approved)
    return {"schema_version": "1.0", "tool": tool, "method_version": method_version,
            "task_id": task_id, "job_id": job_id, "identity": identity,
            "stage_status": stage, "execution_status": status,
            "artifact_validation_status": engineering, "scientific_validation_status": scientific,
            "successful_candidates": approved, "failed_candidates": validation.get("failed_candidates", []),
            "failed_count": len(validation.get("failed_candidates", [])),
            "missing_candidates": missing, "missing_count": len(missing),
            "validation": validation, "artifacts": refs, "deliverable": deliverable,
            "failure_reasons": reasons + (["independent_scientific_validation_required"] if not validator else [])}


def result_validator(result, context):
    """Strict engineering gate; scientific verdict must be recomputed by caller."""
    check = context.get("independent_validator")
    if not callable(check) or not isinstance(result, dict):
        return False
    try:
        refs = result["artifacts"]
        if not refs or result["execution_status"] not in {"ok", "partial_success"}:
            return False
        for ref in refs:
            verify_reference(context["root"], ref, context["identity"])
        verdict = check(context["root"], refs, context["identity"])
        return bool(verdict.get("validator_version") and verdict.get("passed_candidates"))
    except (ValueError, KeyError, OSError):
        return False

"""One provenance/manifest envelope for fixed providers and agent tool loops."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bioma.core.models import Artifact
from bioma.core.research import fingerprint

DISCOVERY_TOOLS = {"taxonomy_list", "taxonomy_skills", "skills_list", "skill_view", "list_artifacts", "write_artifact"}


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalize_result(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            pass
    return result


def successful(result):
    result = normalize_result(result)
    return not (isinstance(result, dict) and (result.get("success") is False or
                result.get("status") in {"error", "failed", "blocked"} or result.get("error")))


def output_paths(result):
    """Only explicit output declarations; never harvest input/config filenames."""
    result = normalize_result(result)
    if isinstance(result, dict):
        for key, value in result.items():
            if key in {"output_path", "output_file", "output_files", "artifacts", "outputs", "files"}:
                yield from _paths(value)
            elif isinstance(value, (dict, list)):
                yield from output_paths(value)
    elif isinstance(result, list):
        for value in result:
            yield from output_paths(value)


def _paths(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"path", "uri", "output_path"} or isinstance(child, (dict, list)):
                yield from _paths(child)


def bind_package(delta, state, message, base_dir):
    """Bind the actual assignment; every file is sealed before publishing it."""
    base_dir = Path(base_dir).resolve()
    inputs = message.payload.get("inputs", state.inputs)
    assignment = message.payload.get("research_assignment") or {}
    known = {str(Path(a.uri).resolve()) for a in delta.artifacts if "://" not in a.uri}
    for evidence in delta.evidence:
        value = dict(evidence.value) if isinstance(evidence.value, dict) else {"value": evidence.value}
        trace = value.get("tool_trace") or []
        receipts = []
        for step in trace:
            observation = normalize_result(step.get("observation"))
            receipts.append({"tool": step.get("name"), "arguments_hash": fingerprint(step.get("arguments", {})),
                             "result_hash": fingerprint(observation), "succeeded": successful(observation),
                             "result_kind": step.get('result_kind', 'unclassified'),
                             "result_validated": step.get('result_validated') is True})
            if not successful(observation):
                continue
            for raw in output_paths(observation):
                if "://" in raw:
                    continue
                path = Path(raw)
                path = (base_dir / path).resolve() if not path.is_absolute() else path.resolve()
                if str(path) in known or not path.is_file():
                    continue
                kind = {".pdb": "structure-pdb", ".cif": "structure-mmcif", ".csv": "csv", ".json": "json",
                        ".png": "png", ".svg": "svg", ".npy": "numpy-array", ".fasta": "sequence-fasta"}.get(path.suffix.lower(), "scientific-output")
                artifact = Artifact(kind=kind, uri=str(path), source=delta.agent_id, evidence_ids=[evidence.id])
                delta.artifacts.append(artifact)
                known.add(str(path))
        computed = [r for r in receipts if r['succeeded'] and r['result_validated']
                    and r['result_kind'] in {'computed_prediction', 'calculation'}]
        data_kind = value.get("data_kind")
        if evidence.kind == "prediction":
            if evidence.method == "step_fusion":
                data_kind = ('computed_prediction' if any(r['result_kind'] == 'computed_prediction' for r in computed)
                             else 'calculation') if computed else "hypothesis_proposal"
                value["live_tool_events"] = bool(trace)
            elif evidence.method == "llm_hypothesis+deterministic_substitution":
                data_kind = "hypothesis_proposal"
            else:
                data_kind = data_kind or "hypothesis_proposal"
        else:
            data_kind = data_kind or {"protocol": "study_design", "structure_resolution": "retrieved_fact",
                "analysis": "analysis_result", "deterministic_calculation": "calculation"}.get(evidence.kind, evidence.kind)
        manifest = []
        for artifact in delta.artifacts:
            if evidence.id not in artifact.evidence_ids:
                continue
            if "://" in artifact.uri:
                raise ValueError("Evidence output must be materialized before publication")
            path = Path(artifact.uri)
            sha = file_digest(path)
            artifact.metadata.update(sha256=sha, file_suffix=path.suffix.lower())
            manifest.append({"artifact_id": artifact.id, "uri": artifact.uri, "kind": artifact.kind,
                             "sha256": sha, "bytes": path.stat().st_size})
            if artifact.id not in evidence.artifact_ids:
                evidence.artifact_ids.append(artifact.id)
        value["evidence_package"] = {
            "schema_version": "scientific-evidence-v1", "data_kind": data_kind,
            "task_request_id": message.task_request_id, "source_message_id": message.id,
            "input_hash": fingerprint(inputs), "contract_id": assignment.get("contract_id"),
            "requirement_ids": assignment.get("requirement_ids", []), "method": evidence.method,
            "method_id": message.payload.get('method_id'),
            "responsibility": message.payload.get('selected_capability') or
                (message.payload.get('accepted_offer') or {}).get('capability_id'),
            "tool_receipts": receipts, "artifact_manifest": manifest,
            "limitations": ["A computational result or hypothesis is not experimental confirmation."] if evidence.kind == "prediction" else [],
            "uncertainty": value.get("uncertainty", "not_quantified"),
        }
        evidence.value = value
    return delta

"""Governed NeuroSnap remote scientific-compute adapter.

Service discovery is read-only.  Paid submission is exposed only through an
exact, reviewed service specification and a non-replay-safe Tool so the normal
WP1/effect ledger boundary applies.
"""
from __future__ import annotations

from dataclasses import dataclass
import csv
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any, Literal
from urllib.parse import quote, urlparse

import requests
from pydantic import Field
from dotenv import dotenv_values

from bioma.tools.base import Tool, ToolInput
from bioma.tools.errors import ToolError, requests_tool_error


SERVICE_STATES = (
    "LISTED", "ACCOUNT_ACCESSIBLE", "INPUT_READY",
    "COST_AUTHORIZED", "EXECUTABLE", "VERIFIED",
)
TERMINAL_STATUSES = frozenset({"completed", "failed"})
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


class NeuroSnapConfigurationError(ValueError):
    pass


class NeuroSnapCancellationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class NeuroSnapServiceSpec:
    """One reviewed binding. Discovery alone never constructs this authority."""

    service_name: str
    method_id: str
    scientific_task: Literal[
        "structure_prediction", "stability_prediction", "solubility_prediction",
        "mutation_effect_prediction", "sequence_design", "binding_prediction",
    ]
    result_kind: str
    required_fields: tuple[str, ...]
    file_fields: tuple[str, ...] = ()
    output_files: tuple[tuple[str, str], ...] = ()
    model_name: str = ""
    model_version: str = ""
    cost_credits_per_job: float | None = None
    account_accessible: bool = False
    licensing_restrictions: str = ""
    runtime_estimate: str = ""
    output_validator: Literal["integrity", "proteinmpnn_ddg_csv"] = "integrity"

    @property
    def executable(self) -> bool:
        return (
            self.account_accessible
            and self.cost_credits_per_job is not None
            and self.cost_credits_per_job > 0
            and bool(self.required_fields)
        )

    def qualification(self, input_payload: dict[str, Any] | None = None,
                      *, authorized_credits: float | None = None,
                      verified: bool = False) -> dict[str, bool]:
        ready = input_payload is not None and all(
            input_payload.get(name) not in (None, "") for name in self.required_fields
        )
        cost_ok = (
            self.cost_credits_per_job is not None
            and authorized_credits is not None
            and 0 < self.cost_credits_per_job <= authorized_credits
        )
        return {
            "LISTED": True,
            "ACCOUNT_ACCESSIBLE": self.account_accessible,
            "INPUT_READY": ready,
            "COST_AUTHORIZED": cost_ok,
            "EXECUTABLE": self.executable and ready and cost_ok,
            "VERIFIED": bool(verified),
        }


class RemoteComputeInput(ToolInput):
    input_payload: dict[str, Any]
    max_credits: float = Field(gt=0)
    estimated_credits: float = Field(gt=0)
    logical_step_id: str = Field(min_length=1, max_length=180)


class NeuroSnapConnector:
    """Small REST client matching the official NeuroSnap SDK endpoints."""

    def __init__(self, *, api_key: str, base_url: str = "https://neurosnap.ai/api",
                 artifact_root: str | Path, timeout_seconds: float = 30,
                 read_retries: int = 2, session: requests.Session | None = None):
        if not api_key or any(ch.isspace() for ch in api_key):
            raise NeuroSnapConfigurationError("NEUROSNAP_API_KEY is missing or malformed")
        normalized = self.normalize_base_url(base_url)
        parsed = urlparse(normalized)
        if (parsed.scheme != "https" or parsed.hostname != "neurosnap.ai"
                or parsed.path != "/api" or parsed.query or parsed.fragment):
            raise NeuroSnapConfigurationError(
                "NeuroSnap base URL must be exactly https://neurosnap.ai/api")
        self._api_key = api_key
        self.base_url = normalized
        self.artifact_root = Path(artifact_root).resolve()
        self.timeout_seconds = float(timeout_seconds)
        self.read_retries = int(read_retries)
        self.session = session or requests.Session()

    @staticmethod
    def normalize_base_url(value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if value.endswith("/services"):
            value = value[:-len("/services")]
        return value

    @classmethod
    def from_environment(cls, *, environment_file: str | Path,
                         artifact_root: str | Path, **kwargs):
        """Load the specified file explicitly; another directory is never implicit."""
        values = dotenv_values(Path(environment_file))
        url = values.get("NEUROSNAP_URL")
        key = values.get("NEUROSNAP_API_KEY")
        if not url or not key:
            raise NeuroSnapConfigurationError(
                "NEUROSNAP_URL and NEUROSNAP_API_KEY are required")
        return cls(api_key=str(key), base_url=str(url),
                   artifact_root=artifact_root, **kwargs)

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._api_key, "User-Agent": "BioMA-NeuroSnap/1.0"}

    def _request(self, method: str, path: str, *, retry_read: bool = True, **kwargs):
        attempts = self.read_retries + 1 if method.upper() in {"GET", "HEAD"} and retry_read else 1
        last = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, self.base_url + path, headers=self._headers(),
                    timeout=self.timeout_seconds, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, (
                    requests.exceptions.Timeout, requests.exceptions.ConnectionError
                )) or (status is not None and status >= 500)
                if attempt + 1 >= attempts or not retryable:
                    raise requests_tool_error(exc, backend="NeuroSnap") from exc
                time.sleep(min(0.25 * (2 ** attempt), 1.0))
        raise requests_tool_error(last, backend="NeuroSnap")  # pragma: no cover

    @staticmethod
    def _json(response) -> Any:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ToolError("NeuroSnap returned invalid JSON.",
                            error_type="internal", retriable=False,
                            backend="NeuroSnap") from exc

    def list_services(self) -> Any:
        return self._json(self._request("GET", "/services"))

    def list_jobs(self) -> Any:
        return self._json(self._request("GET", "/jobs"))

    def submit_job(self, spec: NeuroSnapServiceSpec, input_payload: dict[str, Any],
                   *, max_credits: float, action_instance_id: str) -> dict[str, Any]:
        missing = [key for key in spec.required_fields
                   if input_payload.get(key) in (None, "")]
        if missing:
            raise ValueError("Missing required NeuroSnap fields: " + ", ".join(missing))
        if not spec.account_accessible:
            raise PermissionError("NeuroSnap service is not confirmed account-accessible")
        if spec.cost_credits_per_job is None:
            raise PermissionError("NeuroSnap service cost is unknown")
        if max_credits <= 0 or spec.cost_credits_per_job > max_credits:
            raise PermissionError("NeuroSnap job exceeds its authorized credit ceiling")
        files = {}
        opened = []
        try:
            for name, value in input_payload.items():
                if name in spec.file_fields:
                    path = Path(value).expanduser().resolve()
                    if not path.is_file():
                        raise ValueError("NeuroSnap file input does not exist: " + name)
                    stream = path.open("rb")
                    opened.append(stream)
                    files[name] = (path.name, stream)
                elif isinstance(value, (dict, list)):
                    files[name] = (None, json.dumps(value))
                else:
                    files[name] = (None, str(value))
            note = "bioma-action:" + action_instance_id
            response = self._request(
                "POST", "/job/submit/" + quote(spec.service_name, safe=""),
                retry_read=False, files=files, params={"note": note})
            raw = self._json(response)
        finally:
            for stream in opened:
                stream.close()
        job_id = raw.get("job_id") if isinstance(raw, dict) else raw
        if not isinstance(job_id, str) or not job_id.strip():
            raise ToolError("NeuroSnap submission returned no job ID.",
                            error_type="internal", retriable=False,
                            backend="NeuroSnap")
        return {
            "job_id": job_id,
            "service_name": spec.service_name,
            "scientific_task": spec.scientific_task,
            "model_name": spec.model_name or spec.service_name,
            "model_version": spec.model_version,
            "input_sha256": sha256(
                json.dumps(input_payload, sort_keys=True, default=str,
                           separators=(",", ":")).encode()).hexdigest(),
            "authorized_max_credits": max_credits,
            "estimated_credits": spec.cost_credits_per_job,
            "submitted_at": time.time(),
            "remote_status": "submitted",
            "action_instance_id": action_instance_id,
        }

    def get_job_status(self, job_id: str) -> Any:
        return self._json(self._request("GET", "/job/status/" + quote(job_id, safe="")))

    def get_job_data(self, job_id: str) -> Any:
        return self._json(self._request("GET", "/job/data/" + quote(job_id, safe="")))

    def download_output(self, job_id: str, file_type: str, file_name: str) -> dict[str, Any]:
        if not all(SAFE_COMPONENT.fullmatch(x or "") for x in (job_id, file_type, file_name)):
            raise ValueError("Unsafe NeuroSnap output path component")
        response = self._request(
            "GET", f"/job/file/{quote(job_id, safe='')}/{quote(file_type, safe='')}/"
                   + quote(file_name, safe=""))
        destination = self.artifact_root / job_id / file_type / file_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = response.content
        destination.write_bytes(payload)
        return {"uri": str(destination), "sha256": sha256(payload).hexdigest(),
                "size_bytes": len(payload), "file_type": file_type,
                "file_name": file_name}

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("jobs", "data", "results"):
                if isinstance(payload.get(key), list):
                    return [x for x in payload[key] if isinstance(x, dict)]
        return []

    def reconcile_job(self, contract: dict[str, Any]) -> tuple[str, Any]:
        """Find the logical submission by its persisted action note.

        An absent listing is not proof that the POST failed, so it remains
        unknown and EffectManager prohibits an automatic second submission.
        """
        action_id = contract.get("action_instance_id")
        if not action_id:
            return "unknown", None
        marker = "bioma-action:" + action_id
        try:
            jobs = self.list_jobs()
        except ToolError:
            return "unknown", None
        for row in self._rows(jobs):
            if marker not in {str(row.get("note") or ""), str(row.get("name") or "")}:
                continue
            job_id = row.get("job_id") or row.get("id") or row.get("uuid")
            if not job_id:
                return "unknown", None
            return "succeeded", {
                "job_id": str(job_id),
                "service_name": row.get("service") or row.get("service_name"),
                "remote_status": row.get("status") or "submitted",
                "action_instance_id": action_id,
                "reconciled": True,
            }
        return "unknown", None

    def cancel_job(self, job_id: str, *, authorized: bool = False) -> None:
        del job_id
        if not authorized:
            raise PermissionError("Cancellation requires a separate authorized effect")
        raise NeuroSnapCancellationUnavailable(
            "The reviewed official NeuroSnap SDK exposes no cancellation endpoint")


def _remote_job_result(value: Any, arguments: dict[str, Any]) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("job_id"), str)
        and value.get("job_id")
        and isinstance(value.get("input_sha256"), str)
        and float(value.get("authorized_max_credits", 0)) <= float(arguments["max_credits"])
    )


def build_neurosnap_tool(connector: NeuroSnapConnector,
                         spec: NeuroSnapServiceSpec) -> Tool:
    """Build one exact service Tool; there is intentionally no run-any-service."""
    def submit(input_payload: dict[str, Any], max_credits: float,
               estimated_credits: float, logical_step_id: str):
        del logical_step_id
        if estimated_credits != spec.cost_credits_per_job:
            raise ValueError("Estimated credits differ from the reviewed service cost")
        from bioma.runtime.effects import current_action_instance_id
        result = connector.submit_job(
            spec, input_payload, max_credits=max_credits,
            action_instance_id=current_action_instance_id())
        if _remote_job_result(result, {"max_credits": max_credits}) is not True:
            raise ToolError("NeuroSnap submission receipt failed validation.",
                            error_type="internal", retriable=False,
                            backend="NeuroSnap")
        return result

    def reconcile(contract):
        status, result = connector.reconcile_job(contract)
        if status != "succeeded" or not isinstance(result, dict):
            return status, result
        arguments = contract.get("arguments") or {}
        input_payload = arguments.get("input_payload") or {}
        return status, {**result,
            "service_name": spec.service_name,
            "scientific_task": spec.scientific_task,
            "model_name": spec.model_name or spec.service_name,
            "model_version": spec.model_version,
            "input_sha256": sha256(json.dumps(
                input_payload, sort_keys=True, default=str,
                separators=(",", ":")).encode()).hexdigest(),
            "authorized_max_credits": arguments.get("max_credits"),
            "estimated_credits": spec.cost_credits_per_job,
        }

    return Tool(
        name="neurosnap_submit_" + re.sub(r"[^a-z0-9]+", "_", spec.method_id.lower()).strip("_"),
        description=f"Submit the reviewed NeuroSnap service {spec.service_name}.",
        category="paid-remote-compute", version="1.0.0", author="BioMA",
        func=submit, args_schema=RemoteComputeInput,
        tags=("neurosnap", "neurosnap-exact-service", spec.scientific_task,
              "service:" + spec.service_name),
        requires_network=True, result_kind="remote_compute_job",
        result_validator=_remote_job_result,
        external_effect=True, replay_safe=False,
        resources=("provider:neurosnap", "credits:neurosnap"),
        reconcile_effect=reconcile,
    )


def qualify_remote_result(*, spec: NeuroSnapServiceSpec, receipt: dict[str, Any],
                          job_data: Any, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate provenance without promoting a prediction to observation."""
    if not receipt.get("job_id") or receipt.get("service_name") != spec.service_name:
        raise ValueError("Remote result does not match the submitted service")
    for artifact in artifacts:
        path = Path(artifact["uri"])
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("Remote artifact integrity check failed")
    validation = {"validator": spec.output_validator, "integrity": "passed"}
    if spec.output_validator == "proteinmpnn_ddg_csv":
        validation.update(_validate_proteinmpnn_ddg_csv(artifacts))
    return {
        "schema_version": "neurosnap-model-result-v1",
        "scientific_task": spec.scientific_task,
        "evidence_class": "model_prediction",
        "is_experimental_observation": False,
        "service_name": spec.service_name,
        "model_name": spec.model_name or spec.service_name,
        "model_version": spec.model_version,
        "job_id": receipt["job_id"],
        "input_sha256": receipt["input_sha256"],
        "parameters": job_data.get("config", {}) if isinstance(job_data, dict) else {},
        "artifacts": artifacts,
        "result_validation": validation,
        "limitations": [
            "A model prediction is not an experimental observation.",
            "Predicted stability or mutation effect is not measured delta Tm.",
        ],
    }


def _validate_proteinmpnn_ddg_csv(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the saturation-mutagenesis table without interpreting score sign."""
    csv_artifact = next((row for row in artifacts
                         if str(row.get("file_name", "")).casefold().endswith(".csv")), None)
    if csv_artifact is None:
        raise ValueError("ProteinMPNN-ddG result has no CSV artifact")
    required = {"Chain", "Pre", "Position", "Post", "ProteinMPNN-ddG Score"}
    with Path(csv_artifact["uri"]).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if set(reader.fieldnames or ()) != required:
            raise ValueError("ProteinMPNN-ddG CSV schema is invalid")
        rows = list(reader)
    if not rows:
        raise ValueError("ProteinMPNN-ddG CSV contains no predictions")
    positions: set[tuple[str, int]] = set()
    substitutions: set[tuple[str, int, str]] = set()
    scores = []
    amino_acids = set("ACDEFGHIKLMNPQRSTVWY")
    for row in rows:
        try:
            position = int(row["Position"])
            score = float(row["ProteinMPNN-ddG Score"])
        except (TypeError, ValueError) as exc:
            raise ValueError("ProteinMPNN-ddG CSV has an invalid position or score") from exc
        if (not row["Chain"] or position < 1 or row["Pre"] not in amino_acids
                or row["Post"] not in amino_acids or score != score
                or abs(score) == float("inf")):
            raise ValueError("ProteinMPNN-ddG CSV has an invalid prediction row")
        key = (row["Chain"], position, row["Post"])
        if key in substitutions:
            raise ValueError("ProteinMPNN-ddG CSV has duplicate substitutions")
        substitutions.add(key)
        positions.add((row["Chain"], position))
        scores.append(score)
    return {
        "schema": "proteinmpnn-ddg-saturation-table-v1",
        "rows": len(rows), "positions": len(positions),
        "score_range": [min(scores), max(scores)],
        "score_semantics": "provider_model_score; direction is not inferred by validator",
    }


def build_neurosnap_method_backend(connector: NeuroSnapConnector,
                                   spec: NeuroSnapServiceSpec):
    """Bind one reviewed service to one Compute Method and result contract."""
    if not spec.executable:
        raise ValueError("Only account-accessible services with reviewed cost may bind")
    tool = build_neurosnap_tool(connector, spec)

    def execute(state, message, tools):
        from bioma.core.models import Artifact
        from bioma.backends.base import BackendIncomplete
        from bioma.core.scientific_capabilities import COMPUTE, WorkResult
        from bioma.runtime.effects import plan_current_action

        del tools
        inputs = message.payload.get("inputs") or {}
        payload = inputs.get("service_inputs") or inputs
        max_credits = message.payload.get("max_credits", message.payload.get(
            "budget", inputs.get("max_credits", 0)))
        step = str(message.payload.get("logical_step_id") or inputs.get("logical_step_id") or
                   f"neurosnap:{spec.method_id}:{message.id}")
        resumed = inputs.get("remote_job_receipt")
        if resumed is not None:
            if (not isinstance(resumed, dict)
                    or resumed.get("service_name") != spec.service_name
                    or not resumed.get("job_id")
                    or not resumed.get("input_sha256")):
                raise ValueError("Invalid NeuroSnap resume receipt")
            receipt = dict(resumed)
            action_id = str(receipt.get("action_instance_id") or "")
            max_credits = float(receipt.get("authorized_max_credits") or 0)
        else:
            action_id = plan_current_action(step)
            receipt = tool.call(action_instance_id=action_id, input_payload=payload,
                                max_credits=max_credits,
                                estimated_credits=spec.cost_credits_per_job,
                                logical_step_id=step)
        status_value = connector.get_job_status(receipt["job_id"])
        if isinstance(status_value, dict):
            remote_status = str(status_value.get("status") or
                                status_value.get("state") or "").lower()
        else:
            remote_status = str(status_value).lower()
        receipt = {**receipt, "remote_status": remote_status}
        if remote_status == "failed":
            raise BackendIncomplete("failed", {
                "code": "remote_job_failed", "provider": "NeuroSnap",
                "job_id": receipt["job_id"], "service_name": spec.service_name,
            })
        if remote_status != "completed":
            raise BackendIncomplete("suspended", {
                "code": "remote_job_pending", "provider": "NeuroSnap",
                "job_id": receipt["job_id"], "service_name": spec.service_name,
                "remote_status": remote_status or "unknown",
                "action_instance_id": action_id,
                "remote_job_receipt": receipt,
            })
        data = connector.get_job_data(receipt["job_id"])
        downloaded = [connector.download_output(receipt["job_id"], kind, name)
                      for kind, name in spec.output_files]
        qualified = qualify_remote_result(
            spec=spec, receipt=receipt, job_data=data, artifacts=downloaded)
        artifacts = [Artifact(kind="neurosnap-model-output", uri=row["uri"],
            source="scientific-compute", metadata={
                "sha256": row["sha256"], "remote_job_id": receipt["job_id"],
                "service_name": spec.service_name,
            }).model_dump(mode="json") for row in downloaded]
        return WorkResult(family=COMPUTE, kind=spec.result_kind,
            payload=qualified, input_refs=list(message.payload.get("input_refs", [])),
            artifacts=artifacts, provenance={
                "provider": "NeuroSnap", "service_name": spec.service_name,
                "model_name": spec.model_name or spec.service_name,
                "model_version": spec.model_version,
                "remote_job_id": receipt["job_id"],
                "input_sha256": receipt["input_sha256"],
                "authorized_max_credits": max_credits,
                "estimated_credits": spec.cost_credits_per_job,
                "action_instance_id": action_id,
            })

    from bioma.backends.base import MethodBinding
    from bioma.backends.catalog import method
    from bioma.core.scientific_capabilities import COMPUTE
    declaration = method(COMPUTE, spec.method_id, spec.result_kind, tools=(tool.name,))
    declaration.action_contract.resources = list(tool.resources)
    declaration.action_contract.risk_level = "medium"
    declaration.description = (
        f"Reviewed NeuroSnap {spec.service_name} binding for {spec.scientific_task}")
    declaration.limitations = [
        "Model output is predictive evidence, not an experimental observation.",
        "Execution requires WP1 authority and an explicit credit ceiling.",
    ]
    return MethodBinding(declaration, execute, (tool,))

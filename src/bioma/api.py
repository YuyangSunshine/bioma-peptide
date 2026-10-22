from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from bioma.runtime.deadline import DEFAULT_EXECUTION_SECONDS
from bioma.backends.base import ScientificBackend
from bioma.config import BioMAConfig, LLMConfig, load_config
from bioma.core.models import ScientificState, ScientificTask
from bioma.llm import ChatProvider, provider_for
from bioma.gateway import (
    DeterministicQueryInterpreter, QueryInterpreter,
)
from bioma.core.external_tasks import DeliveryState, ExternalTaskContract
from bioma.delivery import ArtifactDeliveryService
from bioma.artifact_generation import ArtifactGenerationPolicy
from bioma.runtime.engine import DecentralizedRuntime, RuntimeEventSink


class BioMA:
    """The single public Python entry point for a BioMA research run."""

    def __init__(
        self,
        backends: Iterable[ScientificBackend] | None = None,
        *,
        backend: str = "toy",
        config: BioMAConfig | None = None,
        config_path: str | Path | None = None,
        max_events: int | None = None,
        query_deadline_seconds: float = 600.0,
        task_deadline_seconds: float = DEFAULT_EXECUTION_SECONDS,
        ledger_path: str | Path | None = ".bioma/run-ledger.sqlite3",
        remote_nodes: Iterable[str] = (),
        observation_trust=None,
        provider_trust=None,
        provider_principal=None,
        allow_insecure_local_nodes: bool = False,
        query_interpreter: QueryInterpreter | None = None,
        memory_service=None,
        capability_authorizations: Iterable[tuple[str, str, str]] = (),
        support_use_resources=(),
        available_resources: Iterable[str] | None = None,
        artifact_provider=None,
        artifact_generation_policy: ArtifactGenerationPolicy | None = None,
        control_policy: Literal["legacy", "adaptive_v3", "egvoi"] = "legacy",
        option_generator=None,
        proposal_mode: Literal["central", "static"] = "central",
        outcome_forecaster=None,
        scientific_controller=None,
        revision_policy=None,
        control_budget=None,
        evidence_episode_factory=None,
        research_backend=None,
        completion_policy=None,
        result_qualifier=None,
        remote_compute_backends=(),
        provider_factory: Callable[[LLMConfig], ChatProvider] | None = None,
    ) -> None:
        if proposal_mode not in {"central", "static"}:
            raise ValueError(
                "Unsupported proposal_mode: " + str(proposal_mode) +
                ". Backend-owned scientific proposal modes have been retired; "
                "use central with an explicit option_generator, or static for a declared plan. "
                "Replay historical experiments from their frozen commit.")
        remote_nodes = tuple(remote_nodes)
        if remote_nodes:
            raise ValueError('RemoteAgent production execution is BLOCKED: direct-backend provenance is not established')
        resolved_config = config
        model_factory = provider_factory or provider_for
        if backends is None:
            config = config or load_config(config_path)
            resolved_config = config
            agent_list, default_interpreter = build_backends(
                config, backend, observation_trust=observation_trust,
                research_backend=research_backend,
                remote_compute_backends=remote_compute_backends,
                provider_factory=provider_factory,
            )
        else:
            agent_list = list(backends)
            if any(not isinstance(provider, ScientificBackend) for provider in agent_list):
                raise ValueError('BioMA production API accepts ScientificBackend instances only')
            default_interpreter = DeterministicQueryInterpreter()
        self.harness_mode = (resolved_config.resolved_compute_services().worker.harness_mode
                             if resolved_config is not None else 'current')
        if resolved_config is not None:
            compute = resolved_config.resolved_compute_services()
            worker = compute.worker
            self.execution_attribution = {
                "schema_version": 1,
                "record_status": "recorded_at_run_start",
                "backend": backend,
                "agent_engine": worker.agent_engine,
                "harness_mode": worker.harness_mode,
                "models": {
                    "query_contract_compiler": resolved_config.gateway.llm.model,
                    "scientific_worker": worker.llm.model,
                    "delivery_reviewer": (
                        worker.delivery_review_llm or worker.llm
                    ).model,
                    "proposal_service": compute.proposal_llm.model,
                    "assessment_service": compute.assessment_llm.model,
                },
                "configuration": "resolved_bioma_config",
            }
        else:
            self.execution_attribution = {
                "schema_version": 1,
                "record_status": "recorded_at_run_start",
                "backend": backend,
                "agent_engine": "externally_supplied_backends",
                "harness_mode": self.harness_mode,
                "models": {},
                "configuration": "caller_supplied_backends",
            }
        self.query_deadline_seconds = query_deadline_seconds
        self.backend = backend
        self.query_interpreter = query_interpreter or default_interpreter
        from threading import RLock
        self._query_lock = RLock()
        from bioma.core.verification import VerificationEngine
        self.runtime = DecentralizedRuntime(agent_list, max_events=max_events, ledger_path=ledger_path,
            task_deadline_seconds=task_deadline_seconds,
            memory_service=memory_service,
            capability_authorizations=capability_authorizations,
            support_use_resources=support_use_resources,
            available_resources=available_resources,
            verifier=VerificationEngine(observation_trust), provider_trust=provider_trust, provider_principal=provider_principal)
        self.control_policy = control_policy
        self.scientific_control_runtime = None
        if control_policy == "adaptive_v3":
            from bioma.runtime.scientific_control import ScientificControlRuntime
            if option_generator is None and proposal_mode == "static":
                from bioma.scientific_control.generation import PlanDerivedOptionGenerator
                option_generator = PlanDerivedOptionGenerator()
            elif option_generator is None and backend == "llm" and resolved_config is not None:
                from bioma.scientific_control.generation import LLMEvidenceDrivenOptionGenerator
                option_generator = LLMEvidenceDrivenOptionGenerator(
                    model_factory(resolved_config.gateway.llm),
                    model_id=resolved_config.gateway.llm.model,
                )
            self.scientific_control_runtime = ScientificControlRuntime(self.runtime,
                option_generator=option_generator, outcome_forecaster=outcome_forecaster,
                controller=scientific_controller, revision_policy=revision_policy,
                budget=control_budget, result_qualifier=result_qualifier, completion_policy=completion_policy)
        if control_policy == "egvoi":
            from bioma.runtime.evidence_control import EvidenceControlRuntime
            self.scientific_control_runtime = EvidenceControlRuntime(
                self.runtime, episode_factory=evidence_episode_factory, completion_policy=completion_policy)
        self.available_agents = sorted(self.runtime.agents)
        self.query_actor = 'query_contract_compiler'
        self.delivery_service = None
        policy = artifact_generation_policy or ArtifactGenerationPolicy()
        if artifact_provider is not None:
            self.delivery_service = ArtifactDeliveryService(artifact_provider)
        elif backend == "llm" and resolved_config is not None:
            self.delivery_service = ArtifactDeliveryService(
                model_factory(policy.provider_config(resolved_config.gateway.llm)),
            )

    def run(
        self,
        task: ScientificTask | dict[str, Any],
        *,
        event_sink: RuntimeEventSink | None = None,
        scientific_control_runtime=None,
        continuation: dict[str, Any] | None = None,
    ) -> ScientificState:
        with self._query_lock:
            if not isinstance(task, ScientificTask):
                task = ScientificTask.model_validate(task)
            contract = task.gateway.get('research_contract')
            if contract and contract.get('schema_version') != 'research-contract-v2':
                raise ValueError('Historical research contracts are read-only; new execution requires research-contract-v2')
            if self.control_policy in {"adaptive_v3", "egvoi"}:
                if continuation is not None:
                    raise ValueError("Work-package continuation requires the direct execution policy")
                return (scientific_control_runtime or self.scientific_control_runtime).run(task, event_sink=event_sink)
            return self.runtime.run(task, event_sink=event_sink, continuation=continuation)

    def deliver(
        self, contract: ExternalTaskContract | dict[str, Any], *, workspace: str | Path,
        event_sink: RuntimeEventSink | None = None,
    ) -> DeliveryState:
        """Execute an external artifact contract outside committed scientific state."""
        if self.delivery_service is None:
            raise RuntimeError("Artifact delivery requires an llm backend or artifact_provider.")
        if not isinstance(contract, ExternalTaskContract):
            contract = ExternalTaskContract.model_validate(contract)
        from bioma.runtime.telemetry import notify_observer, observe
        observer = (lambda event: notify_observer(event_sink, event)) if event_sink else None
        with observe(observer, agent="artifact_producer"):
            return self.delivery_service.deliver(contract, Path(workspace))

    def query(self, text, **kwargs):
        # Providers/generators may carry per-invocation mutable state. Serialize a
        # shared instance until they implement session-local execution. Different
        # BioMA instances remain independent. Policies below are still run-local.
        with self._query_lock:
            return self._query_with_deadline(text, **kwargs)

    def _query_with_deadline(self, text, *, files=(), external_work_results=(), session_context=None,
              event_sink=None, simulation=None, operational_deliverables=()):
        """Interpret and execute a query with best-effort progress notifications."""
        from bioma.runtime.deadline import deadline
        from bioma.runtime.telemetry import observe, notify_observer
        observer = (lambda event: notify_observer(event_sink, event)) if event_sink else None
        with deadline(self.query_deadline_seconds), observe(observer, agent=self.query_actor):
            return self._query(text, files=files, external_work_results=external_work_results,
                session_context=session_context, event_sink=event_sink, simulation=simulation,
                operational_deliverables=operational_deliverables)

    def _query(
        self,
        text: str,
        *,
        files: Iterable[str | Path] = (),
        external_work_results=(),
        session_context: list[dict[str, Any]] | None = None,
        event_sink: RuntimeEventSink | None = None,
        simulation: dict[str, Any] | None = None,
        operational_deliverables: Iterable[str] = (),
    ) -> ScientificState:
        """Interpret a query and execute it with an explicit scientific data boundary."""
        objective = text
        if not objective.strip():
            raise ValueError("Query cannot be empty.")
        self.runtime.refresh_providers()
        self.available_agents = sorted(self.runtime.agents)
        inputs = _query_inputs(files)
        operational = tuple(dict.fromkeys(
            Path(str(name)).name for name in operational_deliverables
        ))
        if operational:
            inputs["_operational_deliverables"] = operational
        if simulation is not None:
            from bioma.backends.automation.simulation import simulation_payload
            inputs['simulation'] = simulation_payload({'simulation': simulation})
            inputs['execution_mode'] = 'simulation'
        from bioma.runtime.deadline import bounded_call
        # Interpretation is Query's local cognition; remembered context is not
        # an input/evidence grant and must still pass the query contract boundary.
        from bioma.memory.context import agent_context
        from bioma.core.models import AgentMessage
        query_task = ScientificTask(objective=objective, inputs=inputs,
                                    external_work_results=list(external_work_results))
        memory = self.runtime.memory
        context = memory.context(self.query_actor, ScientificState.from_task(query_task),
            AgentMessage(sender='user', recipient=self.query_actor, message_type='query.submitted')) if memory else {}
        from bioma.runtime.telemetry import observe, notify_observer
        def query_telemetry(event):
            if self.runtime.ledger is not None:
                self.runtime.ledger.append(query_task.id, "LLMTelemetry", self.query_actor,
                    event, idempotency_key=None)
            notify_observer(event_sink, event)
        with agent_context(context), observe(query_telemetry,
                agent=self.query_actor, phase="query_interpretation", task_id=query_task.id):
            interpretation = bounded_call(lambda: self.query_interpreter.interpret(
                objective, inputs=inputs, capabilities=self.runtime.capability_registry.all(),
                session_context=list(session_context or [])))
        from bioma.runtime.query_compiler import QueryContractCompiler
        task = QueryContractCompiler().compile_task(query_task, interpretation,
            self.runtime.capability_registry.all())
        # Natural-language intake admits one agent work package. Explicit
        # scientific-control tasks remain available through run(task), rather
        # than a second planner being selected before observing the data.
        result = self.runtime.run(task, event_sink=event_sink)
        if self.harness_mode == 'governed':
            from bioma.runtime.result_qualification import production_scientific_result_qualifier
            checker = production_scientific_result_qualifier(inputs=task.inputs, state=result)
            work_refs = [o.id for o in result.observations if o.kind == 'work_result']
            qualified = checker.qualify(self.runtime, task.id, work_refs) if work_refs else []
            self.runtime.ledger.append(task.id, 'HarnessQualificationChecked', 'harness', {
                'model_id':checker.model_id, 'work_result_refs':work_refs,
                'qualified_evidence_refs':qualified,
                'scope':'Only installed independent validators grant Evidence; delivery is insufficient.'},
                idempotency_key='harness-qualification:' + checker.model_id)
            result = self.runtime.ledger.replay_state(task.id)
        return result


def _query_inputs(files: Iterable[str | Path]) -> dict[str, Any]:
    paths = [str(path) for path in files]
    if not paths:
        return {}
    from bioma.core.source_inputs import identify_sources
    identities = identify_sources(paths)
    inputs = {"files": [row.uri for row in identities],
              "source_inputs": [row.model_dump(mode="json") for row in identities],
              "input_refs": [row.id for row in identities]}
    return inputs


# Resource assembly is separate from model transport and contains no agent loop.
from bioma.backends.factory import build_backends

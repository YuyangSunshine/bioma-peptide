"""Assemble resource owners and direct method bindings; never execute research."""
from bioma.backends.base import MethodBinding
from bioma.backends.compute import build_compute_backend
from bioma.backends.evidence import build_assessment_backend
from bioma.backends.automation.bindings import build_execution_backend
from bioma.backends.research import build_research_backend
from bioma.backends.protocol.bindings import build_study_backend


def build_backends(config, backend='toy', *, base_dir=None, research_backend=None,
                         method_tools=None, remote_compute_backends=(), observation_trust=None,
                         provider_factory=None):
    """Assemble actual services and registries without constructing Role Agents."""
    from pathlib import Path
    from bioma.llm import provider_for
    from bioma.toy import toy_provider_factory
    from bioma.gateway import DeterministicQueryInterpreter, LLMQueryInterpreter
    from bioma.evidence.synthesis import DeterministicEvidenceSynthesizer, LLMEvidenceSynthesizer
    from bioma.backends.protocol.designers import ToyProtocolDesigner, LLMProtocolDesigner
    from bioma.backends.protocol.service import ProtocolDesignService
    from bioma.backends.automation.adapters import ConfiguredAutomationCompiler
    from bioma.backends.automation.simulation import ToyAutomationCompiler, ProtocolSimulator
    from bioma.backends.automation.service import AutomationService
    from bioma.backends.analysis.observation_runner import ObservationAnalysisRunner
    from bioma.skills.registry import SkillRegistry
    from bioma.tools.registry import ToolRegistry
    base_dir = Path(base_dir or Path.cwd()).resolve()
    if isinstance(research_backend,str):
        if research_backend != 'federated_biomedical':
            raise ValueError('Unknown research backend configuration')
        from bioma.backends.federated_research import FederatedBiomedicalSourceClient
        research_backend = FederatedBiomedicalSourceClient(cache_dir=base_dir / '.bioma' / 'source-cache')
    if backend not in {'toy', 'llm'}:
        raise ValueError("backend must be 'toy' or 'llm'.")
    provider_factory = provider_factory or (toy_provider_factory if backend == 'toy' else provider_for)
    compute_config = config.resolved_compute_services()
    if backend == 'toy':
        designer, automator = ToyProtocolDesigner(), ToyAutomationCompiler()
        interpreter, synthesizer = DeterministicQueryInterpreter(), DeterministicEvidenceSynthesizer()
    else:
        designer = LLMProtocolDesigner(provider_factory(config.proagent.llm))
        automator = ConfiguredAutomationCompiler(provider=provider_factory(config.operagent.llm))
        interpreter = LLMQueryInterpreter(provider_factory(config.gateway.llm), model_id=config.gateway.llm.model,
            max_attempts=config.gateway.max_interpretation_attempts)
        synthesizer = LLMEvidenceSynthesizer(provider_factory(compute_config.assessment_llm), model_id=compute_config.assessment_llm.model)
    protocol_service = ProtocolDesignService(designer, max_rectifications=config.proagent.max_rectifications,
        checkpoint_dir=base_dir / '.bioma' / 'protocol-checkpoints' if backend == 'llm' else None)
    automation_service = AutomationService(automator, max_rectifications=config.operagent.max_rectifications,
        allow_simulation=backend == 'toy' or config.operagent.allow_simulation,
        simulator=ProtocolSimulator(Path(config.operagent.simulation_journal) if backend == 'llm' else None))
    source_root = Path(__file__).resolve().parents[3]
    def resource(value):
        path = Path(value)
        resolved = path if path.is_absolute() else config.base_dir / path
        return resolved.resolve() if resolved.exists() or path.is_absolute() else (source_root / path).resolve()
    roots = [resource(value) for value in compute_config.skill_roots]
    skill_registry = SkillRegistry(roots)
    skill_registry.scan()
    tool_registry = ToolRegistry([resource(value) for value in compute_config.tool_roots])
    tool_registry.load()
    tool_registry.load_skills(roots)
    from bioma.backends.compute_methods import exact_sequence_structure_lookup
    from bioma.backends.sequence import structure_lookup
    declaration = exact_sequence_structure_lookup()
    from bioma.tools.structures import build_structure_tools
    resolver = build_structure_tools(base_dir / 'artifacts' / 'research-cache', allow_cached=False)[0]
    research = build_research_backend(research_backend, additional_backends=[
        MethodBinding(declaration,
                      structure_lookup(base_dir), (resolver,))])
    return [research,
        build_compute_backend(config=config, compute_config=compute_config, base_dir=base_dir,
            tool_registry=tool_registry, skill_registry=skill_registry, provider_factory=provider_factory,
            observation_runner=ObservationAnalysisRunner(base_dir), observation_trust=observation_trust,
            method_tools=method_tools, additional_backends=remote_compute_backends),
        build_study_backend(protocol_service, base_dir=base_dir),
        build_execution_backend(automation_service, base_dir=base_dir),
        build_assessment_backend(synthesizer)], interpreter

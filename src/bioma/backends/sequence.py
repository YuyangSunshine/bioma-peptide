"""Sequence calculation and source retrieval return unqualified, versioned work."""
from pathlib import Path
import hashlib
import json
from bioma.core.models import Artifact
from bioma.backends.prediction.planning import checkpoint_for, extract_sequence, structure_resolution_plan
from bioma.runtime.telemetry import emit

def _emit(sink, event):
    event = dict(event)
    kind = event.pop("type")
    emit(kind, **event)

def calculate_sequence_descriptors(base_dir, state, message, *,
                                   source='scientific-compute', event_sink=None):
    """Run the shared calculation without a role handler or state commit.

    Returns an unqualified WorkResult. No Evidence, state delta or final
    delivery is produced by this calculation.
    """
    if message.message_type != 'task.submitted' or message.payload.get('method_id') != 'sequence_descriptors':
        return None
    sequence = extract_sequence(state.inputs)
    if not sequence:
        return None
    from Bio import __version__ as biopython_version
    from Bio.SeqUtils.ProtParam import ProteinAnalysis

    digest = hashlib.sha256(sequence.encode("ascii")).hexdigest()
    plan_id = f"sequence-properties:{digest[:16]}"
    _emit(event_sink, {"type": "execution_plan.created", "agent": source, "state_version": state.version, "plan": {"id": plan_id, "capability": "scientific.computational_research", "task_request_id": message.task_request_id, "budget": {"total_deadline_seconds": 20, "llm_calls": 0, "skill_metadata_queries": 0, "tool_calls": {"analyze_protein_sequence": 1}}, "steps": [{"id": "calculate_properties", "action": "analyze_protein_sequence", "max_attempts": 1, "idempotency_key": digest}]}})
    _emit(event_sink, {"type": "tool.started", "agent": source, "state_version": state.version, "tool": "analyze_protein_sequence", "plan_id": plan_id, "step_id": "calculate_properties"})
    analysis = ProteinAnalysis(sequence)
    counts = analysis.count_amino_acids()
    percentages = {aa: round(counts[aa] * 100 / len(sequence), 4) for aa in counts}
    helix, turn, sheet = analysis.secondary_structure_fraction()
    result = {
        "sequence": sequence,
        "data_kind": "calculation",
        "sequence_sha256": digest,
        "length": len(sequence),
        "molecular_weight_da": round(analysis.molecular_weight(), 4),
        "theoretical_pI": round(analysis.isoelectric_point(), 4),
        "gravy": round(analysis.gravy(), 4),
        "aromaticity": round(analysis.aromaticity(), 6),
        "instability_index": round(analysis.instability_index(), 4),
        "secondary_structure_fraction": {"helix": round(helix, 6), "turn": round(turn, 6), "sheet": round(sheet, 6)},
        "amino_acid_counts": counts,
        "amino_acid_percent": percentages,
        "method": "Bio.SeqUtils.ProtParam.ProteinAnalysis",
        "biopython_version": biopython_version,
    }
    root = Path(base_dir) / "artifacts" / "derived" / state.task_id / digest
    root.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = root / "sequence-properties.json", root / "amino-acid-composition.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path.write_text("amino_acid,count,percent\n" + "".join(f"{aa},{counts[aa]},{percentages[aa]}\n" for aa in counts), encoding="utf-8")
    _emit(event_sink, {"type": "tool.completed", "agent": source, "state_version": state.version, "tool": "analyze_protein_sequence", "plan_id": plan_id, "step_id": "calculate_properties", "observation": {"success": True, **{key: result[key] for key in ("length", "molecular_weight_da", "theoretical_pI", "gravy", "sequence_sha256", "method")}}})
    from bioma.core.scientific_capabilities import WorkResult
    fasta_path = root / "sequence.fasta"
    fasta_path.write_text(f">{digest}\n{sequence}\n", encoding="utf-8")
    artifacts = [Artifact(kind=kind, uri=str(path), source=source,
        metadata={"plan_id": plan_id, "sequence_sha256": digest,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for path, kind in ((json_path, "sequence-properties-json"),
                          (csv_path, "amino-acid-composition-csv"), (fasta_path, "sequence-fasta"))]
    return WorkResult(family='scientific.compute', kind='Calculation',
        payload={'calculation': result,
                 'limitations': ['Sequence descriptors are calculations, not measured protein function.']},
        artifacts=[a.model_dump(mode='json') for a in artifacts],
        provenance={'method': 'sequence_descriptors', 'plan_id': plan_id,
                    'sequence_sha256': digest, 'biopython_version': biopython_version})


def retrieve_exact_structure(base_dir, state, message, tool, *,
                             source='scientific-research', event_sink=None):
    """Existing exact-identity retrieval implementation, without a role instance."""
    if message.message_type != "structure.requested":
        return None
    sequence = extract_sequence(state.inputs)
    if not sequence:
        return None
    plan = structure_resolution_plan(
        sequence=sequence, task_request_id=message.task_request_id
    )
    _emit(event_sink, {
        "type": "execution_plan.created",
        "agent": source,
        "state_version": state.version,
        "plan": plan.model_dump(),
    })
    if tool is None or tool.name != 'resolve_sequence_structure':
        raise ValueError('Exact structure retrieval requires its explicitly bound resolver')
    _emit(event_sink, {
        "type": "tool.started",
        "agent": source,
        "state_version": state.version,
        "tool": "resolve_sequence_structure",
        "plan_id": plan.id,
        "step_id": plan.steps[0].id,
    })
    observation = tool.call(sequence=sequence, reviewed_only=True)
    checkpoint = checkpoint_for(plan, observation)
    _emit(event_sink, {
        "type": "tool.completed",
        "agent": source,
        "state_version": state.version,
        "tool": "resolve_sequence_structure",
        "observation": observation,
        "plan_id": plan.id,
        "step_id": plan.steps[0].id,
        "checkpoint": checkpoint.model_dump(),
    })
    if not observation.get("success"):
        raise RuntimeError(checkpoint.reason or "structure plan failed")
    if observation.get("sequence_sha256") != hashlib.sha256(sequence.encode()).hexdigest() or observation.get("sequence_exact_match") is not True:
        raise ValueError("Structure result does not prove an exact full-sequence match")
    wants_experiment = any(t in state.objective.casefold() for t in ("experimental", "\u5b9e\u9a8c"))
    if wants_experiment and observation.get("structure_source") != "RCSB PDB":
        raise ValueError("Experimental PDB required; a predicted structure cannot satisfy this contract")
    observation = {**observation, "data_kind": "retrieved_fact",
                   "structure_data_kind": "experimental_observation" if observation.get("structure_source") == "RCSB PDB" else "prediction"}
    # Pin each run's deliverables: a later lookup of the same accession
    # must not overwrite previously committed metadata or coordinates.
    coordinates = Path(observation['pdb_path']).read_bytes()
    expected_hash = observation.get('pdb_sha256')
    if expected_hash and hashlib.sha256(coordinates).hexdigest() != expected_hash:
        raise ValueError('Coordinates changed before delivery could be sealed')
    from bioma.core.research import fingerprint
    version = fingerprint({'retrieval': observation, 'coordinates_sha256': hashlib.sha256(coordinates).hexdigest()})
    root = Path(base_dir) / 'artifacts' / 'derived' / state.task_id / observation['sequence_sha256'] / version
    root.mkdir(parents=True, exist_ok=True)
    pdb_path, metadata_path = root / 'structure.pdb', root / 'structure-index.json'
    pdb_path.write_bytes(coordinates)
    observation = {**observation, 'pdb_path': str(pdb_path), 'metadata_path': str(metadata_path),
                   'pdb_sha256': hashlib.sha256(coordinates).hexdigest()}
    metadata_path.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding='utf-8')
    artifacts = []
    for key, kind in (("pdb_path", "protein-structure-pdb"), ("metadata_path", "structure-index-json")):
        uri = observation.get(key)
        if uri:
            artifacts.append(Artifact(
                kind=kind, uri=str(uri), source=source,
                metadata={"plan_id": plan.id, "sequence_identity": observation.get("sequence_identity"), "pdb_id": observation.get("pdb_id")},
            ))
    from bioma.core.scientific_capabilities import RESEARCH, Source, RetrievedClaim, ResearchBundle, WorkResult
    from bioma.core.research import fingerprint
    retrieved = Source(id='source:' + fingerprint(observation),
        identifier='uniprot:' + observation['uniprot_accession'],
        citation=observation['uniprot_url'], spans={'retrieval': json.dumps(observation, sort_keys=True)},
        provenance={'method': 'execution_plan.resolve_sequence_structure', 'retrieval': observation})
    bundle = ResearchBundle(sources=[retrieved], retrieved_claims=[RetrievedClaim(
        statement='The supplied sequence has an exact UniProt match and a retrieved structure.',
        source_id=retrieved.id, span_ids=['retrieval'])],
        limitations=['Retrieved coordinates are not an admitted scientific claim.'],
        provenance={'method': 'exact_sequence_structure_lookup'})
    return WorkResult(family=RESEARCH, kind='ResearchBundle', payload=bundle.model_dump(mode='json'),
        input_refs=list(message.payload.get('input_refs', [])),
        artifacts=[item.model_dump(mode='json') for item in artifacts],
        provenance={'backend': 'retrieve_exact_structure', 'checkpoint': checkpoint.model_dump()})


def structure_lookup(base_dir):
    from bioma.core.scientific_capabilities import RESEARCH, ResearchBundle, WorkResult
    def exact_lookup(state, message, tools):
        # This method is a fixed read-only resolver, not a discoverable execution surface.
        try:
            result = retrieve_exact_structure(base_dir, state, message,
                next((tool for tool in tools if tool.name == 'resolve_sequence_structure'), None))
        except Exception as exc:
            from bioma.tools.errors import ToolError
            if not isinstance(exc, ToolError):
                raise
            import hashlib
            import json
            from pathlib import Path
            from bioma.core.models import Artifact
            sequence = str(message.payload.get('inputs', {}).get('sequence') or
                           state.inputs.get('sequence') or '')
            status = {'classification': 'unavailable',
                'match_status': 'lookup_unavailable', 'source': exc.backend or 'structure resolver',
                'reason_type': exc.error_type, 'retriable': exc.retriable,
                'sequence_sha256': hashlib.sha256(sequence.encode()).hexdigest() if sequence else None}
            root = Path(base_dir) / 'artifacts' / 'derived' / state.task_id / 'structure-status'
            root.mkdir(parents=True, exist_ok=True)
            path = root / 'structure-status.json'
            path.write_text(json.dumps(status, indent=2), encoding='utf-8')
            artifact = Artifact(kind='structure-status-json', uri=str(path),
                                source='scientific-research')
            return WorkResult(family=RESEARCH, kind='ResearchBundle',
                payload=ResearchBundle(sources=[], retrieved_claims=[],
                    limitations=['Exact structure lookup was unavailable; no structure claim was admitted.'],
                    provenance={'method': 'exact_sequence_structure_lookup',
                                'status': status}).model_dump(mode='json'),
                input_refs=list(message.payload.get('input_refs', [])),
                artifacts=[artifact.model_dump(mode='json')],
                provenance={'backend': 'retrieve_exact_structure', 'status': status})
        if result is None:
            raise ValueError('Exact structure retrieval did not complete')
        return result
    return exact_lookup


def sequence_backend(owner='scientific-worker', *, base_dir=None):
    """An explicit resource binding for runtime/remote fixtures, with no Evidence writer."""
    from bioma.backends.base import ScientificBackend, MethodBinding
    from bioma.backends.compute import _sequence_descriptors
    from bioma.backends.compute_methods import sequence_descriptors
    from bioma.core.scientific_capabilities import COMPUTE
    from importlib.util import find_spec
    return ScientificBackend(COMPUTE, owner, [MethodBinding(
        sequence_descriptors(available=find_spec('Bio') is not None),
        _sequence_descriptors(Path(base_dir or Path.cwd())))])

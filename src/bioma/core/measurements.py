"""Authenticated measurement admission, independent of scientific interpretation."""
from datetime import datetime
import json
import hashlib
from pathlib import Path
from pydantic import Field, model_validator

from bioma.core.models import StrictModel
from bioma.runtime.security import digest


def verify_simulation(observation, code, task_id, protocol_text, action_id):
    """Verify replay identity and complete DAG trace; never admit it as empirical."""
    def hash_json(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    value = observation.value
    receipt = value.get('receipt') or {}
    if (observation.kind != 'simulated_experiment' or value.get('mode') != 'simulation'
            or value.get('data_kind') != 'simulation' or value.get('physical_execution') is not False
            or value.get('simulation_model') != 'explicit-data-replay-v1'):
        raise ValueError('Simulation provenance is missing or claims physical execution')
    expected = {'task_id':task_id, 'action_id':action_id, 'protocol_id':code.get('protocolId'),
        'protocol_sha256':hashlib.sha256(protocol_text.encode()).hexdigest(),
        'machine_code_sha256':hash_json(code), 'measurements_sha256':hash_json(value.get('measurements')),
        'trace_sha256':hash_json(value.get('node_trace')), 'status':'completed', 'physical_execution':False}
    if any(receipt.get(k) != v for k,v in expected.items()):
        raise ValueError('Simulation receipt does not bind this task, protocol, action and output')
    if not value.get('source') or not value.get('measurements'):
        raise ValueError('Simulation requires replay data and provenance')
    nodes = {n['templateNodeId']:n for n in code.get('nodes', [])}
    trace = value.get('node_trace') or []
    if not nodes or len(trace) != len(nodes) or value.get('node_count') != len(nodes):
        raise ValueError('Simulation receipt lacks complete node coverage')
    done = set()
    for tick, step in enumerate(trace, 1):
        node_id = step.get('node_id')
        if node_id not in nodes or node_id in done:
            raise ValueError('Simulation has duplicate or unknown node execution')
        node = nodes[node_id]
        if (step.get('resource_id') != node['resourceId'] or step.get('status') != 'completed'
                or step.get('parameters_sha256') != hash_json(node['parameters']) or step.get('virtual_tick') != tick):
            raise ValueError('Simulation node receipt does not match compiled action')
        if any(edge['sourceNodeId'] not in done for edge in code.get('connections', []) if edge['targetNodeId'] == node_id):
            raise ValueError('Simulation violates protocol dependency order')
        done.add(node_id)


class CalibrationRecord(StrictModel):
    calibration_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    valid_from: datetime
    valid_until: datetime

    @model_validator(mode='after')
    def dates(self):
        if self.valid_from.tzinfo is None or self.valid_until.tzinfo is None or self.valid_from >= self.valid_until:
            raise ValueError('Calibration requires an ordered timezone-aware validity interval')
        return self


class MeasurementIdentity(StrictModel):
    schema_version: str = Field(default='measurement-v1', pattern='^measurement-v1$')
    run_id: str = Field(min_length=1)
    experiment_run_id: str = Field(min_length=1)
    action_instance_id: str = Field(min_length=1)
    sample_ids: list[str] = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    acquired_at: datetime
    raw_artifacts: dict[str, str] = Field(min_length=1)
    calibration: CalibrationRecord

    @model_validator(mode='after')
    def validate_identity(self):
        if self.acquired_at.tzinfo is None or not self.calibration.valid_from <= self.acquired_at <= self.calibration.valid_until:
            raise ValueError('Acquisition lies outside calibration validity')
        if len(set(self.sample_ids)) != len(self.sample_ids) or any(not s.strip() for s in self.sample_ids):
            raise ValueError('Samples must have distinct nonempty identities')
        import re
        if any(not k or not re.fullmatch('[0-9a-f]{64}', v) for k, v in self.raw_artifacts.items()):
            raise ValueError('Raw artifacts require SHA-256 digests')
        return self


def observation_payload(observation):
    value = dict(observation.value)
    value.pop('observation_attestation', None)
    return observation.model_copy(update={'value': value}).model_dump(mode='json')


def attest_observation(observation, identity, signer):
    """Only a trusted acquisition adapter owns signer; agents receive the result."""
    identity = MeasurementIdentity.model_validate(identity)
    value = {**observation.value, 'measurement_identity': identity.model_dump(mode='json')}
    unsigned = observation.model_copy(update={'value': value})
    value['observation_attestation'] = signer.sign('bioma.measurement.v1', {
        'run_id': identity.run_id, 'observation_sha256': digest(observation_payload(unsigned))})
    return observation.model_copy(update={'value': value})


def verify_observation(observation, artifacts, trust, run_id, *, check_files=True):
    if observation.kind != 'experimental_observation' or not isinstance(observation.value, dict):
        raise ValueError('Not an experimental observation')
    identity = MeasurementIdentity.model_validate(observation.value.get('measurement_identity'))
    if identity.run_id != run_id or set(identity.raw_artifacts) != set(observation.artifact_ids):
        raise ValueError('Measurement run or raw artifact binding mismatch')
    signer, payload = trust.verify(observation.value.get('observation_attestation'), 'bioma.measurement.v1')
    if signer != identity.adapter_id or payload != {'run_id': run_id, 'observation_sha256': digest(observation_payload(observation))}:
        raise ValueError('Observation differs from authenticated acquisition')
    trust.require(signer, 'observation.attest', run_id=run_id, resource=identity.instrument_id, agent=observation.source)
    by_id = {a.id: a for a in artifacts}
    for aid, expected in identity.raw_artifacts.items():
        artifact = by_id.get(aid)
        if artifact is None or artifact.metadata.get('sha256') != expected:
            raise ValueError('Raw artifact hash or identity mismatch')
        if check_files and hashlib.sha256(Path(artifact.uri).read_bytes()).hexdigest() != expected:
            raise ValueError('Raw measurement content has changed')
    return identity


def simulation_payload(inputs):
    payload = inputs.get('simulation')
    if payload is None:
        payload = {'source':'explicit-toy-fixture', 'measurements':inputs.get('toy_measurements')}
    if not isinstance(payload, dict) or set(payload) - {'source', 'measurements', 'fault'}:
        raise ValueError('Simulation payload accepts only source, measurements and optional fault')
    rows = payload.get('measurements')
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or not row for row in rows):
        raise ValueError('Simulation requires non-empty toy_measurements or explicit simulation.measurements objects')
    if not isinstance(payload.get('source'), str) or not payload['source'].strip():
        raise ValueError('Simulation requires explicit replay source provenance')
    if payload.get('fault') not in {None, 'lost_response', 'node_failure'}:
        raise ValueError('Unknown simulation fault')
    json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)  # Validate before dispatch.
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))

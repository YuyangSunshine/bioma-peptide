"""Scientific meaning is separate from an operation's execution status."""
from enum import Enum


class DataKind(str, Enum):
    CALCULATION = 'calculation'
    PREDICTION = 'prediction'
    SIMULATION = 'simulation'
    EXPERIMENTAL_OBSERVATION = 'experimental_observation'
    UNCLASSIFIED = 'unclassified'


def observation_data_kind(observation, *, artifacts=(), trust=None, run_id=None):
    value = observation.value if isinstance(observation.value, dict) else {}
    if observation.kind == 'simulated_experiment' or value.get('mode') == 'simulation':
        return DataKind.SIMULATION.value
    if observation.kind == 'experimental_observation' and trust is not None:
        from bioma.core.measurements import verify_observation
        try:
            verify_observation(observation, artifacts, trust, run_id)
            return DataKind.EXPERIMENTAL_OBSERVATION.value
        except (ValueError, PermissionError, TypeError, OSError):
            pass
    return DataKind.UNCLASSIFIED.value

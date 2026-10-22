"""Read-only adapter for independently authorized labels and committed evidence."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class IndependentLabel:
    reference: str
    hypothesis: str
    scope_id: str
    verified_at: int
    establishment: str  # known standard or a separate independently verified assay


class LedgerTrainingAuthority:
    """Registries must be supplied by a trusted application, never an LLM payload.

    Qualification of an Evidence record is necessary but insufficient. A separate
    authorized label registry establishes z and scope. Replay checks the original
    runtime verification receipts. Nothing is written to the scientific ledger.
    """
    def __init__(self, *, ledger, records, labels, run_ids):
        self.ledger = ledger
        self.records = MappingProxyType(dict(records))
        self.labels = MappingProxyType(dict(labels))
        self.run_ids = MappingProxyType(dict(run_ids))

    def resolve(self, reference, *, now):
        row = self.records[reference]
        label = self.labels.get(row.independent_label_ref)
        if (label is None or label.establishment not in {"known_standard", "independent_assay"}
                or label.reference != row.independent_label_ref
                or label.hypothesis != row.hypothesis or label.scope_id != row.scope_id
                or label.verified_at > now or row.verified_at < label.verified_at):
            raise ValueError("independent authorized hypothesis label unavailable")
        run_id = self.run_ids[reference]
        if not self.ledger.verify_chain(run_id):
            raise ValueError("source ledger integrity failure")
        state = self.ledger.replay_state(run_id)
        evidence = next((e for e in state.evidence if e.id == row.evidence_ref), None) if state else None
        if (evidence is None or not isinstance(evidence.value, dict)
                or evidence.value.get("qualified_outcome") != row.outcome
                or evidence.value.get("action") != row.action):
            raise ValueError("training source is not matching committed qualified evidence")
        return row

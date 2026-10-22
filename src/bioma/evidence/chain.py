from __future__ import annotations

from bioma.core.models import Evidence


class EvidenceChain:
    """Chain-of-Evidence: durable, typed evidence across analysis iterations."""

    def __init__(self, items: list[Evidence] | None = None):
        self.items: list[Evidence] = list(items or ())
        self.validate()

    def append(self, evidence: Evidence) -> None:
        known = {item.id for item in self.items}
        unknown = set(evidence.parent_ids) - known
        if unknown:
            raise ValueError(f"Evidence has unknown parents: {sorted(unknown)}.")
        if evidence.id in known:
            raise ValueError(f"Duplicate evidence id: {evidence.id!r}.")
        self.items.append(evidence)

    def latest(self, n: int = 8) -> list[Evidence]:
        return self.items[-n:]

    def descendants_of(self, evidence_id: str) -> list[Evidence]:
        return [e for e in self.items if evidence_id in e.parent_ids]

    def ancestry(self, evidence_id: str) -> list[Evidence]:
        by_id = {item.id: item for item in self.items}
        if evidence_id not in by_id:
            raise KeyError(evidence_id)
        ordered: list[Evidence] = []
        visited: set[str] = set()

        def visit(item: Evidence) -> None:
            for parent_id in item.parent_ids:
                parent = by_id[parent_id]
                if parent.id not in visited:
                    visit(parent)
                    visited.add(parent.id)
                    ordered.append(parent)

        visit(by_id[evidence_id])
        return ordered

    def validate(self) -> None:
        known: set[str] = set()
        for item in self.items:
            if item.id in known:
                raise ValueError(f"Duplicate evidence id: {item.id!r}.")
            unknown = set(item.parent_ids) - known
            if unknown:
                raise ValueError(f"Evidence has unknown parents: {sorted(unknown)}.")
            known.add(item.id)

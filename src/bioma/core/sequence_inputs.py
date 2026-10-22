"""Bind selected sequence inputs to their verbatim source in the user request."""
from __future__ import annotations

import json
import re

ALPHABET = frozenset('ACDEFGHIKLMNPQRSTVWY')


def canonical_sequence(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    sequence = ''.join(value.split()).upper()
    return sequence if sequence and set(sequence) <= ALPHABET else None


def bind_sequence_sources(objective: str, sources: list[str]) -> dict:
    for source in sources:
        if not source or not (
            re.search(r'(?<![A-Za-z])' + re.escape(source) + r'(?![A-Za-z])', objective)
            or json.dumps(source, ensure_ascii=False) in objective
        ):
            raise ValueError('Sequence inputs must quote complete source text verbatim from the current request; do not invent, repair, or truncate them.')
    if not sources:
        return {}
    # Admission establishes provenance. Invalid residues remain available for QC,
    # but must not become prediction targets or silently disappear from a batch.
    bound = {'sequence_sources': list(sources)}
    sequences = list(dict.fromkeys(canonical_sequence(source) for source in sources))
    if None not in sequences:
        bound['sequence'] = sequences[0]
        if len(sequences) > 1:
            bound['sequences'] = sequences
    return bound


def labelled_sequence_sources(objective: str) -> list[str]:
    """Accept explicitly labelled sequences, preserving invalid residue tokens."""
    # A qualified label can occur inline; a bare label must start a sentence
    # or line. Capture the whole payload, including invalid residues/punctuation,
    # so an invalid suffix cannot silently become a shorter valid sequence.
    pattern = (
        r'(?:(?<![A-Za-z])protein[ \t]+sequence|'
        r'(?:^|[.!?。！？]\s*)(?:sequence(?:[ \t]+[A-Za-z0-9_-]+)?|'
        r'(?:蛋白质|蛋白)?序列(?:[ \t]*[A-Za-z0-9_-]+)?))'
        r'[ \t]*[:：][ \t]*([^\n。；;]+?)(?=\.(?:\s|$)|[\n。；;]|$)'
    )
    sources = []
    for match in re.finditer(pattern, objective, re.IGNORECASE | re.MULTILINE):
        source = match.group(1).rstrip()
        # Continue only explicitly sequence-shaped lines; never consume another
        # labelled field or silently discard a malformed continuation.
        tail = objective[match.end():]
        for line in (tail.splitlines()[1:] if tail.startswith('\n') else []):
            if not line.strip() or ':' in line or '：' in line:
                break
            if not re.fullmatch(r'[ \t]*[A-Za-z*?0-9-]+(?:[ \t]+[A-Za-z*?0-9-]+)*[ \t]*', line):
                break
            source += '\n' + line
        sources.append(source.rstrip())
    return sources

"""Bounded, integrity-checked text from artifacts already scoped by the runtime."""

import hashlib
from pathlib import Path
import re

from bioma.runtime.deadline import check


_TEXT_SUFFIXES = {'.json', '.csv', '.tsv', '.md', '.txt', '.py'}
_FILE_BYTES = 16_000
_TOTAL_BYTES = 32_000


def build_artifact_context(artifacts):
    catalog, reads = [], []
    remaining = _TOTAL_BYTES
    for artifact in artifacts:
        check()
        row = {'id': artifact.id, 'kind': artifact.kind, 'source': artifact.source,
               'uri': artifact.uri, 'evidence_ids': artifact.evidence_ids,
               'sha256': artifact.metadata.get('sha256')}
        catalog.append(row)
        if artifact.uri.startswith('bioma+sha256:') or '://' in artifact.uri:
            row['content_status'] = 'not_materialized'
            continue
        path = Path(artifact.uri)
        suffix = artifact.metadata.get('file_suffix', path.suffix)
        if not isinstance(suffix, str) or suffix.lower() not in _TEXT_SUFFIXES:
            row['content_status'] = 'unsupported_format'
            continue
        if not isinstance(row['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
            row['content_status'] = 'unsealed'
            continue
        if remaining <= 0:
            row['content_status'] = 'omitted_context_budget'
            continue
        try:
            if not path.is_file():
                row['content_status'] = 'unavailable'
                continue
            with path.open('rb') as stream:
                raw = stream.read(min(_FILE_BYTES, remaining) + 1)
        except OSError:
            row['content_status'] = 'unavailable'
            continue
        check()
        if len(raw) > min(_FILE_BYTES, remaining):
            row['content_status'] = 'omitted_size_limit'
            continue
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError(f'Artifact content changed before evidence assessment: {artifact.id}')
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            row['content_status'] = 'unsupported_encoding'
            continue
        row.update(content_status='available', content=content)
        remaining -= len(raw)
        reads.append((path, row['sha256'], len(raw)))
    return catalog, reads


def verify_artifact_context(reads):
    for path, expected, size in reads:
        check()
        try:
            with path.open('rb') as stream:
                raw = stream.read(size + 1)
        except OSError as exc:
            raise ValueError(f'Artifact became unavailable during evidence assessment: {path.name}') from exc
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f'Artifact content changed during evidence assessment: {path.name}')
    check()

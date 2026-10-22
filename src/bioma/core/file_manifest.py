"""Bounded attachment availability facts for query interpretation, not evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def inspect_input_files(files, *, max_bytes=200_000):
    """Read only explicitly supplied paths. Never infer absent scientific values.

    JSON paths describe observed keys, not values or completeness of measurement.
    Large/binary inputs remain available for the responsible execution backend.
    This advisory snapshot neither grants access nor approves scientific inputs.
    """
    rows = []
    remaining = max(0, max_bytes)
    for filename in files:
        path = Path(filename)
        row = {'path': str(path), 'name': path.name, 'status': 'unavailable',
               'content_inspected': False,
               'scope': 'input availability and structure; not scientific validation'}
        rows.append(row)
        try:
            with path.open('rb') as stream:
                row['status'] = 'available'
                row['size_bytes'] = path.stat().st_size
                if path.suffix.lower() != '.json' or row['size_bytes'] > remaining:
                    continue
                raw = stream.read(remaining + 1)
            remaining = max(0, remaining - len(raw))
            if len(raw) != row['size_bytes']:
                row['inspection_error'] = 'FileChangedDuringInspection'
                continue
            row['sha256'] = hashlib.sha256(raw).hexdigest()
            value = json.loads(raw.decode('utf-8-sig'))
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            row['inspection_error'] = type(exc).__name__
            continue
        fields = set()
        complete = True

        def visit(item, location, depth=0):
            nonlocal complete
            if depth > 6 or len(fields) >= 200:
                complete = False
                return
            if isinstance(item, dict):
                for key, child in item.items():
                    if len(fields) >= 200:
                        complete = False
                        break
                    field = location + '.' + str(key)
                    fields.add(field)
                    visit(child, field, depth + 1)
            elif isinstance(item, list):
                if len(item) > 8:
                    complete = False
                for child in item[:8]:
                    visit(child, location + '[]', depth + 1)

        visit(value, '$')
        row.update(content_inspected=True, observed_json_fields=sorted(fields),
                   structure_complete=complete)
    return rows

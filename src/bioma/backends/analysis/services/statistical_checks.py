"""Independent mathematical checks for explicitly requested statistical methods.

No benchmark answers, filenames, genes or expected experimental effects enter
these checks. They verify adjusted probabilities from the supplied p values;
they do not certify the upstream test or its experimental assumptions.
"""
import math
import re


def validate_statistical_delivery(request, artifacts):
    if not re.search(r'\bBH\b|Benjamini[\s–—-]*Hochberg', request, re.IGNORECASE):
        return []
    checks = []

    def visit(value, location):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, location + '.' + str(key))
        elif isinstance(value, list):
            # Explicit observable p/q rows, not field names elsewhere in prose.
            if value and all(isinstance(row, dict) and 'p' in row and 'q' in row for row in value):
                for index, row in enumerate(value):
                    if any(type(row[key]) not in {int, float} or not math.isfinite(row[key])
                           or not 0 <= row[key] <= 1 for key in ('p', 'q')):
                        raise ValueError(f'Benjamini-Hochberg invalid probability at {location}[{index}]')
                order = sorted(range(len(value)), key=lambda i: value[i]['p'])
                expected = [0.0] * len(value)
                ceiling = 1.0
                for rank in range(len(order), 0, -1):
                    index = order[rank - 1]
                    ceiling = min(ceiling, value[index]['p'] * len(order) / rank)
                    expected[index] = ceiling
                bad = [i for i, row in enumerate(value)
                       if not math.isclose(row['q'], expected[i], rel_tol=1e-7, abs_tol=1e-12)]
                if bad:
                    raise ValueError(f'Benjamini-Hochberg consistency failed at {location}, rows {bad}. '
                                     'Apply reverse cumulative minima in sorted p-value order, then restore row order. '
                                     'Correct the implementation and any reproduction script; do not overwrite results by hand.')
                checks.append({'method': 'benjamini_hochberg', 'path': location,
                               'tests': len(value), 'passed': True, 'scope': 'q consistency given reported p; not p-value correctness'})
            else:
                for i, child in enumerate(value):
                    visit(child, location + f'[{i}]')

    for name, content in artifacts.items():
        if name.lower().endswith('.json'):
            visit(content, name)
    return checks

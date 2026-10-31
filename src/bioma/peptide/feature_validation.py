"""Independent, bounded checks of uncertain UniProt feature coordinates."""
import csv
import io
import re

VERSION = 'uniprot-feature-location/1.0'


def validate_feature_delivery(inputs, artifacts):
    """Run the fixed check independently of whether a model spots the defect."""
    sources = [(name, text) for name, text in inputs.items()
               if isinstance(text, str) and text.startswith('ID   ')]
    if len(sources) != 1:
        return []  # Do not infer which source an ambiguous multi-record table uses.
    source_name, source = sources[0]
    checks = []
    for name, table in artifacts.items():
        if not name.lower().endswith('.csv'):
            continue
        violation, reason = uncertain_feature_coordinates(source, table)
        if violation:
            raise ValueError(f'{reason}; source={source_name}, artifact={name}. '
                             'Preserve the non-exact endpoint; do not infer a closed range or overwrite source evidence.')
        if violation is False:
            checks.append({'method':VERSION, 'source':source_name, 'path':name,
                           'passed':True, 'scope':reason + '; endpoint uncertainty only, not biological or numerical-property validation'})
    return checks


def uncertain_feature_coordinates(source, table):
    """Detect asserted exact CSV endpoints for source endpoints that are not exact.

    Scope is deliberately narrow: type/start/end/annotation CSV columns, unique
    feature type + annotation matches, and Swiss-Prot location semantics. It does
    not certify feature biology, calculated properties, or unmatched rows.
    """
    if not isinstance(source, str) or not isinstance(table, str):
        return None, 'requires complete source and CSV text'
    if len(source.encode()) + len(table.encode()) > 200_000:
        return None, 'feature check exceeds bounded input size'
    try:
        from Bio import SwissProt
        from Bio.SeqFeature import ExactPosition
        record = SwissProt.read(io.StringIO(source))
        rows = csv.DictReader(io.StringIO(table))
        if not {'type', 'start', 'end', 'annotation'} <= set(rows.fieldnames or []):
            return None, 'unsupported feature table schema'
        if set(rows.fieldnames) - {'type', 'start', 'end', 'annotation', 'length', 'mw_da', 'pI', 'gravy'}:
            return None, 'additional columns may qualify uncertainty; requires another checker'
        checked = 0
        for row in rows:
            if not row.get('annotation'):
                continue
            matches = [f for f in record.features if f.type == row['type']
                       and f.qualifiers.get('note') == row['annotation']]
            if len(matches) != 1:
                continue
            feature = matches[0]
            if feature.location is None:
                continue
            for column, position in [('start', feature.location.start), ('end', feature.location.end)]:
                if type(position) is ExactPosition:
                    continue
                checked += 1
                if re.fullmatch(r'[0-9]+', (row.get(column) or '').strip()):
                    return True, f'{VERSION}: non-exact source {column} asserted as exact for {feature.type} / {row["annotation"]}'
        return (False, f'{VERSION}: {checked} non-exact endpoints preserved') if checked else (
            None, 'no uniquely matched non-exact endpoints; outside check scope')
    except (ImportError, ValueError, TypeError, KeyError, AttributeError, csv.Error):
        return None, 'source or table could not be independently parsed'

"""Bounded sequence-space screening; no affinity or cyclic-product ADMET claims."""
from pathlib import Path
import csv
import hashlib
import io
import json
import uuid
from .contracts import sha256

ROOT = Path(__file__).resolve().parents[3] / 'artifacts' / 'mutation_scans'
ALPHABET = 'ACDEFGHIKLMNPQRSTVWY'
VERSION = 'methionine-single-mutant-scan/1.0'
LIMITATIONS = [
    'Descriptors apply to unmodified linear all-L sequences with free termini, not the TBMB sulfonium product.',
    'No affinity, ADMET, permeability, toxicity, proteolysis, docking or MD prediction is performed.',
    'Extra methionine or introduced cysteine requires separate crosslink-chemistry review.',
    'Fixed positions are supplied explicitly; candidate IDs identify sequences, not complete chemical species.',
    'No universal best candidate or weighted affinity/developability score is assigned.',
]


def check_inputs(sequence, fixed_positions):
    if not 3 <= len(sequence) <= 80 or any(a not in ALPHABET for a in sequence):
        raise ValueError('Provide an uppercase canonical sequence of 3 to 80 residues')
    if len(set(fixed_positions)) != len(fixed_positions) or any(type(p) is not int or not 1 <= p <= len(sequence) for p in fixed_positions):
        raise ValueError('Fixed positions must be unique one-based positions')
    anchors = [i+1 for i,a in enumerate(sequence) if a == 'M']
    if len(anchors) != 3 or not set(anchors) <= set(fixed_positions):
        raise ValueError('This method requires exactly three methionines, all fixed as crosslink anchors. '
                         f'Observed one-based M positions: {anchors}; supplied fixed positions: {fixed_positions}. '
                         'Correct the indices from the input sequence; do not mutate an anchor.')
    if len(fixed_positions) == len(sequence):
        raise ValueError('At least one position must remain mutable')


def descriptors(sequence):
    from Bio.SeqUtils.ProtParam import ProteinAnalysis
    p = ProteinAnalysis(sequence)
    return {'linear_mass_da': p.molecular_weight(), 'linear_gravy': p.gravy(),
            'aromatic_fraction': p.aromaticity(), 'glycine_count': sequence.count('G'),
            'proline_count': sequence.count('P')}


def candidates(sequence, fixed_positions):
    check_inputs(sequence, fixed_positions)
    rows = []
    for index, original in enumerate(sequence, 1):
        if index in fixed_positions: continue
        for replacement in ALPHABET:
            if replacement == original: continue
            mutant = sequence[:index-1] + replacement + sequence[index:]
            rows.append({'candidate_id': 'seq-' + hashlib.sha256(mutant.encode()).hexdigest()[:16],
                'mutation': f'{original}{index}{replacement}', 'sequence': mutant,
                'position': index, 'from_residue': original, 'to_residue': replacement,
                'chemistry_flag': 'extra_methionine' if replacement == 'M' else
                    'introduced_cysteine' if replacement == 'C' else 'retains_anchor_count',
                **descriptors(mutant)})
    return rows


def csv_text(rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return stream.getvalue()


def plot(rows):
    from html import escape
    masses = [r['linear_mass_da'] for r in rows]; hydropathy = [r['linear_gravy'] for r in rows]
    lo, hi = min(masses), max(masses); bottom, top = min(hydropathy), max(hydropathy)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="520" viewBox="0 0 800 520">',
        '<rect width="800" height="520" fill="white"/>',
        '<text x="30" y="25">Single mutants: linear-sequence descriptors only</text>',
        '<text x="30" y="48">Not affinity, cyclic-product ADMET or a ranking. Orange: chemistry review.</text>',
        '<path d="M70 70 V450 H760" fill="none" stroke="black"/>',
        '<text x="310" y="505">Linear mass (Da)</text>',
        '<text x="15" y="290" transform="rotate(-90 15 290)">Linear GRAVY</text>']
    for i in range(5):
        x=lo+(hi-lo)*i/4;y=bottom+(top-bottom)*i/4
        parts += [f'<text x="{65+680*i/4:.2f}" y="475" font-size="12">{x:.1f}</text>',
                  f'<text x="25" y="{450-365*i/4:.2f}" font-size="12">{y:.2f}</text>']
    for row in rows:
        x=75+675*(row['linear_mass_da']-lo)/(hi-lo or 1)
        y=445-365*(row['linear_gravy']-bottom)/(top-bottom or 1)
        color='#287a91' if row['chemistry_flag']=='retains_anchor_count' else '#bd6c20'
        parts.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="3" fill="{color}"><title>{escape(row["mutation"])}</title></circle>')
    return ''.join(parts) + '</svg>'


def scan(sequence, fixed_positions):
    rows=candidates(sequence, fixed_positions)
    folder=ROOT/uuid.uuid4().hex;folder.mkdir(parents=True)
    summary={'schema_version':'1.0','method_version':VERSION,'parent_sequence':sequence,
        'sequence_sha256':hashlib.sha256(sequence.encode()).hexdigest(),
        'fixed_positions':sorted(fixed_positions), 'candidate_count':len(rows),
        'counts_by_chemistry_flag':{flag:sum(r['chemistry_flag']==flag for r in rows) for flag in sorted({r['chemistry_flag'] for r in rows})},
        'parent_linear_descriptors':descriptors(sequence),'limitations':LIMITATIONS}
    table=folder/'single_mutants.csv';table.write_text(csv_text(rows),newline='')
    chart=folder/'linear_descriptor_space.svg';chart.write_text(plot(rows))
    report=folder/'scan_summary.json';report.write_text(json.dumps(summary,indent=2))
    return {'status':'ok','summary':summary,'files':{str(p):sha256(p) for p in (table,chart,report)},
        'analysis_inputs':[str(table),str(chart),str(report)],
        'scientific_validation_scope':'Single-substitution identity, fixed positions and linear sequence descriptors only'}


def validate(result, args):
    try:
        if result.get('status') != 'ok':return False
        rows=candidates(args['sequence'],args['fixed_positions']);summary=result['summary']
        files=result['files'];by_name={Path(p).name:Path(p) for p in files}
        if set(by_name)!={'single_mutants.csv','linear_descriptor_space.svg','scan_summary.json'} or len(files)!=3:return False
        for name,digest in files.items():
            p=Path(name).resolve()
            if not p.is_relative_to(ROOT.resolve()) or not p.is_file() or p.stat().st_size>4*1024**2 or sha256(p)!=digest:return False
        with by_name['single_mutants.csv'].open(newline='') as stream:actual=list(csv.DictReader(stream))
        expected=list(csv.DictReader(io.StringIO(csv_text(rows))))
        if actual!=expected or by_name['linear_descriptor_space.svg'].read_text()!=plot(rows):return False
        for row in actual:
            seq=row['sequence'];parent=args['sequence']
            if sum(a!=b for a,b in zip(seq,parent))!=1 or len(seq)!=len(parent):return False
            if any(seq[i-1]!=parent[i-1] for i in args['fixed_positions']):return False
        return (summary['parent_sequence']==args['sequence'] and summary['candidate_count']==len(rows)
            and summary['fixed_positions']==sorted(args['fixed_positions']) and summary['limitations']==LIMITATIONS
            and summary['method_version']==VERSION and summary['parent_linear_descriptors']==descriptors(args['sequence'])
            and summary['sequence_sha256']==hashlib.sha256(args['sequence'].encode()).hexdigest()
            and summary['counts_by_chemistry_flag']=={flag:sum(r['chemistry_flag']==flag for r in rows) for flag in sorted({r['chemistry_flag'] for r in rows})}
            and json.loads(by_name['scan_summary.json'].read_text())==summary)
    except (OSError,ValueError,KeyError,TypeError):return False

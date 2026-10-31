"""Target-independent public discovery and independently recomputed structure checks."""
from pathlib import Path
import json
import re
import uuid
from urllib.parse import urlencode
from .contracts import sha256

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / 'artifacts' / 'target_research'


def fetch(url, *, limit=32*1024**2):
    import requests
    from bioma.runtime.deadline import check, remaining
    # URLs are built by the fixed methods below; no arbitrary endpoint is accepted.
    from urllib.parse import urlsplit
    if urlsplit(url).hostname not in {'rest.uniprot.org','files.rcsb.org'}:
        raise ValueError('unregistered_target_service')
    folder=DATA/uuid.uuid4().hex;folder.mkdir(parents=True)
    path=folder/(urlsplit(url).path.rsplit('/', 1)[-1] if url.endswith('.cif') else 'source.json')
    left=remaining();timeout=min(30,left) if left is not None else 30
    size=0
    # Retry only transient transport/server failures, within the enclosing deadline.
    # Source identity and scientific checks are never relaxed by a retry.
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session=requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=2, connect=2, read=0, status=2, backoff_factor=0.25,
        status_forcelist=[429,500,502,503,504], allowed_methods=['GET'],
        respect_retry_after_header=False)))
    try:
        check()
        with session.get(url,stream=True,timeout=timeout,allow_redirects=False) as response:
            response.raise_for_status()
            if response.is_redirect:raise ValueError('unexpected_service_redirect')
            with path.open('wb') as out:
                for chunk in response.iter_content(1024*1024):
                    check();size+=len(chunk)
                    if size>limit:raise ValueError('target_resource_too_large')
                    out.write(chunk)
        if not size:raise ValueError('empty_target_resource')
    except BaseException:
        path.unlink(missing_ok=True);raise
    finally:
        session.close()
    return path


def target_rows(data):
    rows=data.get('results',[data] if data.get('primaryAccession') else [])
    return [{'accession':r['primaryAccession'],'protein':r.get('proteinDescription',{}),
             'organism':r.get('organism',{}),'sequence_length':r.get('sequence',{}).get('length'),
             'sequence':r.get('sequence',{}).get('value'),
             'structures':[{'pdb_id':x['id'],'properties':x.get('properties',[]),
                            'download_url':'https://files.rcsb.org/download/'+x['id']+'.cif'}
                           for x in r.get('uniProtKBCrossReferences',[]) if x.get('database')=='PDB']}
            for r in rows]


def discover(query):
    query=query.strip()
    if not query or len(query)>500:raise ValueError('bounded_uniprot_query_required')
    # Ask the source for the discovery fields, rather than downloading unrelated
    # annotations and then truncating the original evidence during review.
    fields='accession,id,protein_name,organism_name,organism_id,length,sequence,xref_pdb'
    if re.fullmatch(r'[A-Z0-9]{6,10}',query) and any(c.isdigit() for c in query):
        url=f'https://rest.uniprot.org/uniprotkb/{query}.json?'+urlencode({'fields':fields})
    else:url='https://rest.uniprot.org/uniprotkb/search?'+urlencode({'query':query,'format':'json','size':10,'fields':fields})
    source=fetch(url);data=json.loads(source.read_text());rows=target_rows(data)
    return {'status':'ok','query':query,'source_url':url,'targets':rows,'returned_count':len(rows),
            'selection':'No target or structure selected by this tool; compare organism, construct, method and coverage.',
            'files':{str(source):sha256(source)},'source_fields':fields.split(','),
            'qualification':'Selected source fields for target discovery, not complete functional annotation or binding evidence'}


def validate_discovery(result,args):
    try:
        if result['status']!='ok' or result['query']!=args['query'].strip() or len(result['files'])!=1:return False
        name,digest=next(iter(result['files'].items()));p=Path(name).resolve()
        return (p.is_relative_to(DATA.resolve()) and p.stat().st_size<=32*1024**2 and sha256(p)==digest
                and result['targets']==target_rows(json.loads(p.read_text())))
    except (OSError,ValueError,KeyError,TypeError):return False


def structure_metrics(path):
    from Bio.PDB.MMCIF2Dict import MMCIF2Dict
    import math
    cif=MMCIF2Dict(str(path))
    ids=cif.get('_atom_site.label_asym_id',[]);groups=cif.get('_atom_site.group_PDB',[])
    elements=cif.get('_atom_site.type_symbol',[]);coords=[cif.get('_atom_site.Cartn_'+x,[]) for x in 'xyz']
    if not ids or any(len(x)!=len(ids) for x in [groups,elements,*coords]):raise ValueError('invalid_atom_table')
    if not all(math.isfinite(float(v)) for axis in coords for v in axis):raise ValueError('nonfinite_coordinates')
    residues=cif.get('_atom_site.label_seq_id',[])
    chains={c:{'atom_count':ids.count(c),'polymer_residues':len({residues[i] for i,x in enumerate(ids) if x==c and groups[i]=='ATOM' and residues[i] not in {'.','?'}})} for c in sorted(set(ids))}
    def table(prefix, fields):
        columns=[cif.get(prefix+field,[]) for field in fields]
        count=max((len(column) for column in columns),default=0)
        return [{field:column[i] if i<len(column) else None
                 for field,column in zip(fields,columns)} for i in range(count)]
    return {'entry_id':cif.get('_entry.id',[''])[0].upper(),'method':cif.get('_exptl.method',[]),
            'entities':table('_entity.',['id','type','pdbx_description']),
            'chain_entities':table('_struct_asym.',['id','entity_id']),
            'database_references':table('_struct_ref.',['id','entity_id','db_name','pdbx_db_accession','db_code']),
            'reference_alignments':table('_struct_ref_seq.',['ref_id','pdbx_strand_id','seq_align_beg','seq_align_end','db_align_beg','db_align_end']),
            'resolution':cif.get('_refine.ls_d_res_high',cif.get('_em_3d_reconstruction.resolution',[])),
            'chains':chains,'atom_count':len(ids),'hetero_atom_count':groups.count('HETATM'),
            'unobserved_residue_count':len(cif.get('_pdbx_unobs_or_zero_occ_residues.auth_seq_id',[])),
            'all_coordinates_finite':True,
            'limitations':['No receptor repair or chain removal performed','No peptide-binding, force-field or production-MD qualification']}


def structure_chart(metrics):
    """Deterministic inventory chart, not a binding or structure-quality score."""
    from html import escape
    rows = list(metrics['chains'].items())
    maximum = max((v['polymer_residues'] for _, v in rows), default=1) or 1
    height = 100 + 28 * len(rows)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{height}" viewBox="0 0 760 {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="20" y="25" font-size="18">{escape(metrics["entry_id"])}: observed polymer residues per chain</text>',
             '<text x="20" y="48" font-size="12">Coordinate inventory only; not binding affinity or receptor qualification.</text>']
    for i, (chain, values) in enumerate(rows):
        y = 70 + i * 28
        count = values['polymer_residues']
        parts += [f'<text x="20" y="{y+15}" font-size="12">{escape(chain)}</text>',
                  f'<rect x="100" y="{y}" width="{500*count/maximum:.2f}" height="20" fill="#287a91"/>',
                  f'<text x="620" y="{y+15}" font-size="12">{count}</text>']
    return ''.join(parts) + '</svg>'


def assess(pdb_id):
    pdb_id=pdb_id.strip().upper()
    if not re.fullmatch(r'[0-9][A-Z0-9]{3}',pdb_id):raise ValueError('invalid_pdb_id')
    url=f'https://files.rcsb.org/download/{pdb_id}.cif';source=fetch(url)
    metrics=structure_metrics(source)
    if metrics['entry_id']!=pdb_id:raise ValueError('pdb_identity_mismatch')
    report=source.parent/f'{pdb_id}_structure_assessment.json';report.write_text(json.dumps(metrics,indent=2))
    chart=source.parent/f'{pdb_id}_structure_inventory.svg';chart.write_text(structure_chart(metrics))
    return {'status':'ok','pdb_id':pdb_id,'source_url':url,'metrics':metrics,
            'files':{str(source):sha256(source),str(report):sha256(report),str(chart):sha256(chart)},
            'analysis_inputs':[str(report),str(chart)],'execution_status':'ok',
            'scientific_validation_scope':'mmCIF identity, coordinate finiteness and structural inventory only'}


def validate_assessment(result,args):
    try:
        files=result['files'];source=next(Path(x) for x in files if x.endswith('.cif'))
        if result['pdb_id']!=args['pdb_id'].strip().upper() or len(files)!=3:return False
        for name,digest in files.items():
            p=Path(name).resolve()
            if not p.is_relative_to(DATA.resolve()) or p.stat().st_size>32*1024**2 or sha256(p)!=digest:return False
        expected=structure_metrics(source)
        chart=next(Path(x) for x in files if x.endswith('.svg'))
        if chart.read_text()!=structure_chart(expected):return False
        return expected['entry_id']==result['pdb_id'] and expected==result['metrics'] and all(json.loads(Path(x).read_text())==expected for x in files if x.endswith('.json'))
    except (OSError,ValueError,KeyError,TypeError,StopIteration,IndexError):return False

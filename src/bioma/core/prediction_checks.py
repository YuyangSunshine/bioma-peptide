"""Independent checks of a recorded AlphaFold2 computation, not natural structure truth."""
from bioma.core.scientific_capabilities import has_computational_result
import hashlib
import json
import math
from pathlib import Path

from bioma.core.models import Evidence


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checked_structure(sequence, pdb_path, scores_path):
    mapping=dict(zip('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(), 'ARNDCQEGHILKMFPSTWYV'))
    residues=[]
    for line in Path(pdb_path).read_text().splitlines():
        if line.startswith('ATOM  '):
            if not all(math.isfinite(float(line[a:b])) for a,b in ((30,38),(38,46),(46,54))):
                raise ValueError('Nonfinite structure coordinate')
            if line[12:16].strip()=='CA':
                residues.append((line[21],int(line[22:26]),line[26],mapping.get(line[17:20]),float(line[60:66])))
    n=len(sequence)
    if not 16<=n<=1000 or set(sequence)-set(mapping.values()):
        raise ValueError('Invalid prediction input sequence')
    if (len(residues)!=n or len({r[0] for r in residues})!=1
            or [r[1] for r in residues]!=list(range(1,n+1)) or any(r[2].strip() for r in residues)
            or ''.join(r[3] or '?' for r in residues)!=sequence):
        raise ValueError('Predicted residue coverage or sequence differs from the executed input')
    scores=json.loads(Path(scores_path).read_text())
    confidence=scores['plddt'];pae=scores['pae']
    if (len(confidence)!=n or any(isinstance(x,bool) or not math.isfinite(x) or not 0<=x<=100 for x in confidence)
            or len(pae)!=n or any(len(row)!=n for row in pae)
            or any(isinstance(x,bool) or not math.isfinite(x) or x<0 for row in pae for x in row)
            or isinstance(scores['ptm'],bool) or not math.isfinite(scores['ptm']) or not 0<=scores['ptm']<=1):
        raise ValueError('Invalid confidence dimensions or values')
    if any(not math.isfinite(r[4]) or abs(r[4]-v)>.011 for r,v in zip(residues,confidence)):
        raise ValueError('PDB confidence disagrees with the score file')
    return {'residues':n,'sequence_sha256':hashlib.sha256(sequence.encode()).hexdigest(),
            'mean_plddt':sum(confidence)/n,'ptm':scores['ptm'],'pae_dimensions':[n,n]}


def qualify_alphafold_prediction(work):
    if (work.family!='scientific.compute' or not has_computational_result(work)
            or work.provenance.get('method')!='continuous_scientific_investigation'):
        return None
    if not any(r.get('tool')=='run_local_alphafold2' for r in work.payload.get('tool_receipts', [])):
        return None
    review=work.payload.get('delivery_review',{})
    if not review.get('review_version') or review.get('issues')!=[]:
        return None
    reviewed=review.get('sha256',{})
    artifacts=work.artifacts
    for a in artifacts:
        expected=a.get('metadata',{}).get('sha256')
        if not expected or reviewed.get(a['uri'])!=expected or file_hash(a['uri'])!=expected:
            raise ValueError('Prediction delivery artifact is not bound to current reviewed bytes')
    matches=[]
    for record in work.payload.get('tool_receipts',[]):
        if record.get('tool')!='run_local_alphafold2':continue
        path=record['path']
        if file_hash(path)!=record['sha256'] or reviewed.get(path)!=record['sha256']:
            raise ValueError('Prediction tool receipt changed or was not reviewed')
        receipt=json.loads(Path(path).read_text())
        if (receipt.get('tool')!='run_local_alphafold2' or receipt.get('version')!='1.0.0'
                or receipt.get('status')!='ok' or receipt.get('result_kind')!='computed_prediction'
                or not receipt.get('result_validated') or not receipt.get('tool_grant_ref')
                or not receipt.get('parent_scientific_action_ref')):
            raise ValueError('Prediction requires a successful granted typed executor receipt')
        for key in ('arguments','result'):
            computed=hashlib.sha256(json.dumps(receipt[key],sort_keys=True,default=str).encode()).hexdigest()
            if computed!=receipt[key+'_sha256'] or computed!=record[key+'_sha256']:
                raise ValueError('Prediction receipt content hash mismatch')
        result=receipt['result'];args=receipt['arguments'];sequence=args['sequence']
        if (result.get('method')!='AlphaFold2 via ColabFold' or result.get('model')!='alphafold2_ptm model 1'
                or result.get('seed')!=0 or result.get('recycles')!=3
                or result.get('msa_mode')!=args.get('msa_mode','mmseqs2_uniref_env')):
            raise ValueError('Unsupported or contradictory prediction configuration')
        files=result['files']
        for filename,expected in files.items():
            if file_hash(filename)!=expected:raise ValueError('Prediction source file changed')
        command=result['command']
        expected_flags={'--model-type':'alphafold2_ptm','--num-models':'1','--model-order':'1',
                        '--num-recycle':'3','--random-seed':'0','--msa-mode':result['msa_mode']}
        for flag,value in expected_flags.items():
            if command.count(flag)!=1 or command[command.index(flag)+1]!=value:
                raise ValueError('Recorded command differs from prediction configuration')
        fasta=Path(command[1])
        if str(fasta) not in files or ''.join(l.strip() for l in fasta.read_text().splitlines() if not l.startswith('>'))!=sequence:
            raise ValueError('Actual executor FASTA differs from its arguments')
        parameters=Path(command[command.index('--data')+1])/'params/params_model_1_ptm.npz'
        if file_hash(parameters)!=result['parameter_sha256']:
            raise ValueError('AlphaFold parameter identity changed')
        pdb=result['structure_path'];scores=result['scores_path']
        selected={files[pdb],files[scores]}
        delivered={a['metadata']['sha256'] for a in artifacts}
        if not selected<=delivered:continue
        checked=checked_structure(sequence,pdb,scores)
        if checked['sequence_sha256']!=result['sequence_sha256']:
            raise ValueError('Prediction sequence identity mismatch')
        matches.append((checked,result,record))
    if not matches:return None
    if len(matches)!=1:raise ValueError('Multiple prediction versions require separate qualification')
    checked,result,record=matches[0]
    return Evidence(kind='checked_computational_prediction',source='harness',
        claim='The recorded AlphaFold2 configuration generated a structure for the specified sequence; residue coverage and confidence files passed independent consistency checks. No experimental structure validation.',
        value={'qualification_status':'checked_computational_prediction','data_kind':'computed_prediction',
            'scientific_object':checked['sequence_sha256'],'checks':checked,
            'model':result['model'],'seed':result['seed'],'recycles':result['recycles'],
            'msa_mode':result['msa_mode'],'parameter_sha256':result['parameter_sha256'],
            'tool_receipt_sha256':record['sha256'], 'executor_tool_version':'1.0.0',
            'tool_implementation_identity':record['tool_implementation_identity'], 'experimental_validation':False,
            'allowed_claim_types':['structure_prediction_generated','residue_coverage','confidence_file_consistency'],
            'limits':['Confidence is a model estimate, not measured accuracy.',
                'Sequence-to-gene/species identity is not independently qualified by this check.',
                'No native conformation, biological activity, causal mechanism or experimental validation is established.']},
        artifact_ids=[a['id'] for a in artifacts])


def validate_prediction_claim_scope(claims, evidence):
    """Restricted prediction facts cannot resolve untyped/experimental claims."""
    if evidence.kind != 'checked_computational_prediction':
        return
    value=evidence.value or {}
    for claim in claims:
        if (claim.get('claim_type') not in value.get('allowed_claim_types', [])
                or claim.get('scientific_object') != value.get('scientific_object')
                or any(key in claim and claim[key]!=value.get(key) for key in ('parameter_sha256','model'))):
            raise ValueError('Checked computational prediction cannot resolve this claim type or scientific object')

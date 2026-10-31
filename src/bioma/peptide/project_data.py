"""Read-only access to explicitly imported, hash-bound local project materials."""
import json
import os
from pathlib import Path
from .contracts import confined, sha256


def inventory():
    root=Path(os.environ.get('BIOMA_PROJECT_DATA',Path(__file__).resolve().parents[3]/'.local-data/nectin4')).resolve()
    manifest=root/'manifest.json'
    if not manifest.is_file():raise ValueError('blocked_project_materials_not_imported')
    data=json.loads(manifest.read_text())
    return root,{x['local']:x for x in data['files']}


def listing(query='',offset=0):
    _,rows=inventory();terms=query.casefold().split()
    ranked=sorted(rows,key=lambda k:(-sum(t in k.casefold() for t in terms),k))
    if terms:ranked=[k for k in ranked if any(t in k.casefold() for t in terms)]
    return {'status':'ok','query':query,'offset':offset,'total':len(ranked),
            'materials':[{'material_id':k,'size':rows[k]['size'],'sha256':rows[k]['sha256']} for k in ranked[offset:offset+30]],
            'scope':'Local original evidence; presence does not imply scientific approval'}


def validate_listing(result,args):
    try:return result==listing(args.get('query',''),args.get('offset',0))
    except (OSError,KeyError,ValueError):return False


def read(material_id):
    root,rows=inventory()
    if material_id not in rows:raise ValueError('unregistered_project_material')
    p=confined(root,material_id);record=rows[material_id]
    if sha256(p)!=record['sha256'] or p.stat().st_size!=record['size']:raise ValueError('project_material_identity_mismatch')
    preview=None
    if p.suffix.lower() in {'.json','.yaml','.yml','.txt','.log','.pdb','.cif','.sdf'}:
        with p.open('r',errors='replace') as f:preview=f.read(12000)
    return {'status':'ok','material_id':material_id,'bytes':record['size'],'preview':preview,
            'files':{str(p):record['sha256']} if record['size'] <= 32*1024**2 else {},
            'external_reference':{'uri':p.as_uri(),'size':record['size'],'sha256':record['sha256'],
                                  'immutable_version':record['sha256'],'producer':'imported_original',
                                  'retention':'local project evidence; explicit owner-managed deletion'},'scope':'Imported original; scientific review status unchanged'}


def validate_read(result,args):
    try:
        expected=read(args['material_id'])
        return result==expected
    except (OSError,KeyError,ValueError):return False

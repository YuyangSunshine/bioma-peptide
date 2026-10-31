"""Independent production-use admission; neither tool output nor Agent flags grant it."""
import json
from pathlib import Path
from .contracts import confined, digest, sha256


def production_gate(profile, registry):
    def record(name):
        ref=profile.get(name)
        item=registry.get('inputs',{}).get(ref,{})
        if not item or ref not in profile.get('inputs',[]):
            raise ValueError('blocked_parameter_package: missing registered '+name)
        path=confined(registry['input_root'],item['path'])
        if sha256(path)!=item['sha256'] or path.stat().st_size>2*1024*1024:
            raise ValueError('blocked_parameter_package: review hash mismatch')
        return json.loads(path.read_text())
    review=record('production_review_ref')
    required={'approved','parameter_use_confirmed','receptor_review_confirmed','chemical_identity_confirmed','candidate_scope_confirmed'}
    if any(review.get(k) is not True for k in required) or not review.get('reviewed_by'):
        raise ValueError('blocked_parameter_package: independent use review required')
    expected={'method':profile['method'],'identity':profile['identity'],'source_hashes':profile['source_hashes'],
              'scope':profile['production_scope'],'interpreter':profile['interpreter'],
              'arguments':profile['arguments'],'gpu_ids':profile.get('gpu_ids',[]),'resource_caps':profile['resource_caps'],
              'input_hashes':{ref:registry['inputs'][ref]['sha256'] for ref in profile['inputs']
                              if ref not in {profile.get('production_review_ref'),profile.get('worker_qualification_ref')}}}
    if review.get('binding_sha256')!=digest(expected) or 'production_md' not in review.get('allowed_uses',[]):
        raise ValueError('blocked_parameter_package: approval binding or purpose mismatch')
    if profile.get('purpose')=='production_md':
        qualification=record('worker_qualification_ref')
        if (qualification.get('binding_sha256')!=digest(expected)
                or any(qualification.get(k) is not True for k in ['engine_started','outputs_verified','cancel_confirmed','resume_confirmed','budget_enforced'])):
            raise ValueError('blocked_worker_qualification: real engine canary required')

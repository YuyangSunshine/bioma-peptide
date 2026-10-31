import json
from pathlib import Path
import tempfile
import unittest
from bioma.peptide.admission import production_gate
from bioma.peptide.contracts import digest,sha256


class AdmissionTests(unittest.TestCase):
    def test_approval_is_bound_to_inputs_scope_and_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'input').write_text('synthetic')
            registry={'input_root':temp,'inputs':{'input':{'path':'input','sha256':sha256(root/'input')}}}
            p={'method':'tbmb_md_production','purpose':'md_canary','identity':{'test':'synthetic'},'source_hashes':{'engine':'abc'},
               'production_scope':{'candidate_count':1},'interpreter':{'path':'/synthetic'},'arguments':{},'resource_caps':{},
               'inputs':['input','review'],'production_review_ref':'review'}
            expected={'method':p['method'],'identity':p['identity'],'source_hashes':p['source_hashes'],'scope':p['production_scope'],
                      'interpreter':p['interpreter'],'arguments':{},'gpu_ids':[],'resource_caps':{},
                      'input_hashes':{'input':registry['inputs']['input']['sha256']}}
            review={k:True for k in ['approved','parameter_use_confirmed','receptor_review_confirmed','chemical_identity_confirmed','candidate_scope_confirmed']}
            review.update(reviewed_by='synthetic-test-only',binding_sha256=digest(expected),allowed_uses=['production_md'])
            (root/'review').write_text(json.dumps(review));registry['inputs']['review']={'path':'review','sha256':sha256(root/'review')}
            production_gate(p,registry)
            p['production_scope']={'candidate_count':3}
            with self.assertRaisesRegex(ValueError,'binding'):production_gate(p,registry)
            p['production_scope']={'candidate_count':1};p['purpose']='production_md'
            with self.assertRaisesRegex(ValueError,'qualification'):production_gate(p,registry)
            p['purpose']='md_canary';registry['inputs']['input']['sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'binding'):production_gate(p,registry)

    def test_agent_like_success_cannot_approve_production(self):
        with self.assertRaisesRegex(ValueError,'blocked_parameter_package'):
            production_gate({'purpose':'production_md','approved':True,'scientific_validation_status':'passed'}, {'inputs':{}})

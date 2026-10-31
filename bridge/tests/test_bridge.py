import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from bioma_peptide_bridge.broker import Broker, process_identity
from bioma_peptide_bridge.contracts import artifact_reference, normalize, sha256, verify_reference, result_validator
from bioma_peptide_bridge.data import measurement, check_split


class Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'a').write_text('evidence')
        self.identity = {'input':'abc','chemical':'def','config':'ghi'}
        self.ref = artifact_reference(self.root,'a',job_id='job-test',identity=self.identity)

    def norm(self, raw, **kw):
        return normalize(raw, tool='test', task_id='task', job_id='job-test', identity=self.identity,
                         method_version='1', root=self.root, artifacts=[self.ref], candidates=['c1','c2'], **kw)

    def test_status_strings_fail_closed(self):
        for stage, expected in [('failed_topology','failed'),('failed','failed'),('new_status','failed'),
                                ('partial_success','partial_success'),('interrupted','interrupted'),('blocked_parameter_package','blocked')]:
            r = self.norm(json.dumps({'status':stage,'scientific_validation_status':'passed'}))
            self.assertEqual(r['execution_status'],expected); self.assertFalse(r['deliverable'])
        self.assertFalse(self.norm('[]')['deliverable'])

    def test_partial_only_verified_subset(self):
        validator=lambda *args: {'validator_version':'test/1','passed_candidates':['c1'],
                                'failed_candidates':[{'candidate_id':'c2','reason':'bad_graph'}]}
        r=self.norm('{"status":"partial_success"}',validator=validator)
        self.assertEqual(r['successful_candidates'],['c1']);self.assertEqual(r['missing_count'],1)
        self.assertTrue(r['deliverable'])
        self.assertFalse(self.norm('{"status":"interrupted"}',validator=validator)['deliverable'])

    def test_forged_success_and_reference_tampering(self):
        self.assertFalse(result_validator({'execution_status':'ok','artifacts':[self.ref]}, {}))
        (self.root/'a').write_text('tampered')
        self.assertEqual(self.norm('{"status":"ok"}')['artifact_validation_status'],'failed')
        (self.root/'a').unlink()
        self.assertEqual(self.norm('{"status":"ok"}')['artifact_validation_status'],'failed')

    def test_path_and_identity_guards(self):
        for value in ['../a','/etc/passwd']:
            with self.assertRaises(ValueError): artifact_reference(self.root,value,job_id='j',identity={})
        (self.root/'link').symlink_to(self.root/'a')
        with self.assertRaises(ValueError): artifact_reference(self.root,'link',job_id='j',identity={})
        with self.assertRaises(ValueError): verify_reference(self.root,self.ref,{})

    def test_large_external_reference(self):
        with (self.root/'large').open('wb') as f: f.truncate(33*1024*1024)
        ref=artifact_reference(self.root,'large',job_id='j',identity={})
        self.assertEqual(ref['size'],33*1024*1024); self.assertLess(len(json.dumps(ref)),1000)
        self.assertNotIn('base64',json.dumps(ref))

    def test_missing_values_and_leakage(self):
        row=dict(candidate_id='c',target_id='t',chemical_state='x',source_id='s',source_version='1',measurement_kind='experimental_kd')
        for value in ['/', '-', None]:
            r=measurement(dict(row,value=value));self.assertIsNone(r['value']);self.assertEqual(r['missingness'],'unspecified')
        with self.assertRaises(ValueError):
            check_split([{'candidate_id':'a','family_id':'f','split':'train'}, {'candidate_id':'b','family_id':'f','split':'test'}])


class Jobs(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'in').mkdir();(self.root/'in'/'seed.txt').write_text('synthetic')
        self.budget=dict(seconds=5,cpu_seconds=4,memory_bytes=256*1024*1024,output_bytes=2*1024*1024,cpus=1)
        self.config={'input_root':str(self.root/'in'),'inputs':{'seed':{'path':'seed.txt','sha256':sha256(self.root/'in'/'seed.txt')}},
                     'profiles':{'canary':{'method':'engineering_canary','approved':True,'review_id':'synthetic-fixture-v1',
                       'inputs':['seed'],'identity':{'chemical':'synthetic'},'delay_seconds':0.2,'resource_caps':self.budget}}}
        self.registry=self.root/'registry.json';self.registry.write_text(json.dumps(self.config))
        self.broker=Broker(self.root/'jobs',self.registry)

    def submit(self, key='one', **kw):
        return self.broker.submit(profile='canary',input_refs=['seed'],idempotency_key=key,resources=kw.get('resources',self.budget))

    def wait(self, job):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            s=self.broker.status(job)
            if s['status'] not in {'running','queued'}:return s
            time.sleep(.05)
        self.fail('job did not finish')

    def test_concurrent_idempotency_and_reattach(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:self.submit(),range(4)))
        ids={r['job_id'] for r in results};self.assertEqual(len(ids),1);job=ids.pop()
        self.assertEqual(self.wait(job)['status'],'completed')
        self.assertEqual(Broker(self.root/'jobs',self.registry).reattach(job)['job_id'],job)
        self.assertTrue(self.broker.collect(job)['artifacts'])
        (self.root/'jobs'/job/'output'/'canary.json').write_text('changed')
        with self.assertRaises(ValueError):self.broker.collect(job)
        with self.assertRaises(ValueError):self.submit(resources=dict(self.budget,seconds=4))

    def test_cancel_real_process_and_resume_same_job(self):
        self.config['profiles']['canary']['delay_seconds']=2
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        job=self.submit()['job_id']
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            state=self.broker.status(job)
            if state.get('child_start'):break
            time.sleep(.05)
        self.assertEqual(self.broker.cancel(job)['cancellation'],'requested')
        self.assertEqual(self.wait(job)['status'],'cancelled')
        self.assertIsNone(process_identity(state['child_pid']))
        self.broker.resume(job);self.assertEqual(self.wait(job)['status'],'completed')
        self.assertEqual(self.broker.status(job)['attempt'],2)

    def test_budget_timeout(self):
        self.config['profiles']['canary']['delay_seconds']=2
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        job=self.submit(resources=dict(self.budget,seconds=1))['job_id']
        self.assertEqual(self.wait(job)['status'],'timed_out')

    def test_parameter_admission_and_input_hash(self):
        self.config['profiles']['canary'].update(purpose='production_md',approved=True)
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        with self.assertRaisesRegex(ValueError,'blocked_parameter_package'):self.submit()
        self.config['profiles']['canary'].pop('purpose')
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        (self.root/'in'/'seed.txt').write_text('wrong')
        with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):self.submit()

    def test_worker_interpreter_hash_rejected(self):
        import sys
        self.config['profiles']['canary']['interpreter']={'path':sys.executable,'sha256':'0'*64}
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        with self.assertRaisesRegex(ValueError,'interpreter_identity_mismatch'):self.submit()

    def test_unreviewed_and_wrong_docking_profile(self):
        self.config['profiles']['canary']['approved']=False
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        with self.assertRaisesRegex(ValueError,'unreviewed'):self.submit()
        self.config['profiles']['canary'].update(approved=True,method='diffdock_screen')
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        with self.assertRaisesRegex(ValueError,'outside_legacy_docking_scope'):self.submit()

    def test_checkpoint_profile_mismatch(self):
        self.config['profiles']['canary']['delay_seconds']=2
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        job=self.submit()['job_id']; self.broker.cancel(job);self.wait(job)
        self.config['profiles']['canary']['delay_seconds']=0
        self.registry.write_text(json.dumps(self.config));self.broker=Broker(self.root/'jobs',self.registry)
        with self.assertRaisesRegex(ValueError,'checkpoint_profile_mismatch'):self.broker.resume(job)


if __name__=='__main__':unittest.main()

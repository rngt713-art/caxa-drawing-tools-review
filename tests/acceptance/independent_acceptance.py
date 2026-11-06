"""Codex independent offline acceptance tests; no real CAXA process/network."""
import copy, hashlib, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
APP = Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and not sys.argv[1].startswith('-') else Path(__file__).parent/'dsh_fix'/'app'
sys.path.insert(0, str(APP))
import drawing_bundle as db, upload_core as core, journal, relations, staging
from caxa.bridge import CaxaError
from caxa.uploader import CaxaUploader, CLASS_ID_DRAWING
ACCOUNT=dict(ip='offline.invalid',port=1,user='test',password='unused')
class Bridge:
    workflows=[]
    workflow_reply=None
    def login(self,*a): return True
    def stop(self): pass
    def start_workflow(self,ids,*a,**kw):
        self.workflows.append(list(ids))
        return self.workflow_reply or dict(
            process_instance_id=501,
            started=True,
            process_state="2",
            process_start_time="offline",
            state_verified=True,
            documents_verified=True,
            linked_document_ids=list(ids),
        )
class Uploader:
    calls=[]; links=[]; fail=set(); existing=True; relation_status='已借用'
    def __init__(self,*a,**kw): self.borrow_actions=[]; self.staging=kw.get('staging_dir')
    def close(self):
        if self.staging: self.staging.close()
    def find_part(self,code): return {'object_id':20,'identity':code} if self.existing else None
    def upload_item(self,item,*a,**kw):
        self.calls.append((item['code'],kw.get('relation_type')))
        if item['code'] in self.fail: raise CaxaError('known pre-write validation failure',stage='resolve_parent')
        return dict(object_id=100+int(item['code'].split('.')[-1]),class_id=CLASS_ID_DRAWING,class_name='Drawing',class_verified=True,iterator_no='2',attached=True)
    def link_relation(self,parent,child,kind,**kw):
        self.links.append((parent,child,kind))
        return dict(status=self.relation_status,note='fake',link_type=kind)
class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.tmp.name); self.jp=str(self.root/'journal.db')
        Bridge.workflows=[]; Bridge.workflow_reply=None
        Uploader.calls=[]; Uploader.links=[]; Uploader.fail=set(); Uploader.existing=True; Uploader.relation_status='已借用'
    def tearDown(self): self.tmp.cleanup()
    def manifest(self,n=1):
        items=[]
        for i in range(1,n+1):
            f=self.root/f'01.0{i} test.pdf'; data=f'PDF{i}'.encode(); f.write_bytes(data)
            items.append(dict(code=f'01.0{i}',name='test',kind='component',file_path=str(f),sha256=hashlib.sha256(data).hexdigest(),size=len(data),status='待上传',upload_sel=True))
        return dict(schema=db.SCHEMA,schema_version=1,items=items,relations=[],relations_migrated=1)
    def run_bundle(self,m,**kw):
        with patch.object(core,'CaxaBridge',Bridge),patch.object(core,'CaxaUploader',Uploader),patch.object(core,'find_person_id',return_value=10):
            return core.upload_bundle(m,ACCOUNT,journal_path=self.jp,staging_root=str(self.root/'staging'),log=lambda *a:None,**kw)
    def test_repeat_upload_and_workflow(self):
        m=self.manifest(); self.run_bundle(m,start_workflow=True); self.run_bundle(m,start_workflow=True)
        self.assertEqual(len(Uploader.calls),1); self.assertEqual(Bridge.workflows,[[101]])
    def test_mixed_retry_workflow_contains_all_confirmed_drawings(self):
        m=self.manifest(2); Uploader.fail={'01.02'}
        self.assertFalse(self.run_bundle(m,start_workflow=True)['ok'])
        Uploader.fail=set(); self.run_bundle(m,start_workflow=True)
        self.assertEqual(Bridge.workflows,[[101,102]])
        self.assertEqual([x[0] for x in Uploader.calls].count('01.01'),1)
    def test_send_workflow_later_does_not_upload_again(self):
        m=self.manifest(); self.run_bundle(m,start_workflow=False); self.run_bundle(m,start_workflow=True)
        self.assertEqual(len(Uploader.calls),1); self.assertEqual(Bridge.workflows,[[101]])
    def test_relation_edit_does_not_upload_again(self):
        m=self.manifest(); self.run_bundle(m)
        m['relations']=[dict(parent_code='01.99',child_code='01.01',link_type='borrow',manual_override=True)]
        self.run_bundle(m); self.assertEqual(len(Uploader.calls),1); self.assertEqual(len(Uploader.links),1)
    def test_failed_relation_blocks_workflow(self):
        m=self.manifest(); m['relations']=[dict(parent_code='01.99',child_code='01.01',link_type='borrow',manual_override=True)]
        Uploader.relation_status='失败'; r=self.run_bundle(m,start_workflow=True)
        self.assertFalse(r['ok']); self.assertEqual(Bridge.workflows,[])
    def test_running_upload_survives_restart_as_pending(self):
        with journal.TaskJournal(self.jp) as j:
            t=j.create_task(digest='example'); j.record_operation(t['task_id'],'upload','01.01',status=journal.OP_RUNNING)
        with journal.TaskJournal(self.jp) as j: self.assertTrue(j.pending_operations(t['task_id']))
    def test_staging_rejected_collision_preserves_first_bytes(self):
        m=self.manifest(2); s=staging.TaskStaging(root=str(self.root/'stage'))
        first=s.stage(m['items'][0],file_name='same.pdf')
        with self.assertRaises(staging.StagingConflict): s.stage(m['items'][1],file_name='same.pdf')
        self.assertEqual(Path(first.path).read_bytes(),b'PDF1'); s.close()
    def test_existing_link_wrong_type_not_success(self):
        u=CaxaUploader(None,log=lambda *a:None); u.find_part=lambda c:dict(object_id=1 if c=='01.99' else 2)
        u._child_link_state=lambda *a:(True,False)
        r=u.link_relation('01.99','01.01','borrow')
        self.assertNotIn(r['status'],relations.DONE_STATUSES)
    def test_existing_link_unknown_type_not_success(self):
        u=CaxaUploader(None,log=lambda *a:None); u.find_part=lambda c:dict(object_id=1 if c=='01.99' else 2)
        u._child_link_state=lambda *a:(True,None)
        self.assertNotIn(u.link_relation('01.99','01.01','borrow')['status'],relations.DONE_STATUSES)
    def test_tree_reclassifies_multiple_existing_children_to_borrow(self):
        m=self.manifest(); m['relations']=[dict(parent_code=p,child_code='01.01',link_type='own') for p in ['01.98','01.99']]
        r=self.run_bundle(m); self.assertTrue(r['ok']); self.assertEqual([r[2] for r in Uploader.links],['borrow','borrow'])
    def test_tree_classification_new_own_persists_across_retry(self):
        m=self.manifest(); m['relations']=[dict(parent_code='01.99',child_code='01.01',link_type='borrow')]
        Uploader.existing=False; Uploader.fail={'01.01'}; self.run_bundle(m)
        m=json.loads(json.dumps(m)); Uploader.existing=True; Uploader.fail=set(); self.run_bundle(m)
        self.assertEqual([x[1] for x in Uploader.calls],['own','own'])
    def test_false_workflow_success_is_pending(self):
        m=self.manifest(); Bridge.workflow_reply=dict(process_instance_id=501,started=False)
        r=self.run_bundle(m,start_workflow=True); self.assertFalse(r['ok']); self.assertEqual(r['workflow']['status'],'待核实')
    def test_unchecked_bad_name_does_not_block_valid_row(self):
        m=self.manifest(2); m['items'][1].update(code='',kind='unknown',upload_sel=False)
        r=self.run_bundle(m); self.assertTrue(r['ok']); self.assertEqual(len(Uploader.calls),1)
    def test_workflow_record_restores_drawing_set(self):
        with journal.TaskJournal(self.jp) as j:
            t=j.create_task(digest='example'); j.record_workflow(t['task_id'],status=journal.OP_OK,process_id=501,drawing_ids=[101,102])
            r=core._workflow_from_record(j.workflow(t['task_id']))
        self.assertEqual(r['drawing_ids'],[101,102])
    def test_deleted_legacy_edge_stays_deleted(self):
        m=self.manifest(); m.pop('relations_migrated'); m['assemblies']=[dict(machine='03.TEST',codes=['01.01'])]
        db.normalize_manifest_relations(m); self.assertTrue(m['relations']); m['relations']=[]
        db.normalize_manifest_relations(m); self.assertEqual(m['relations'],[])
    def test_repeat_upload_only_returns_duplicate_without_error(self):
        m=self.manifest(); self.run_bundle(m); r=self.run_bundle(m)
        self.assertTrue(r['ok']); self.assertEqual(len(Uploader.calls),1)
    def test_pending_cannot_be_bypassed_with_new_task(self):
        m=self.manifest()
        with patch.object(Uploader,'upload_item',side_effect=CaxaError('lost reply',response_lost=True)):
            r=self.run_bundle(m)
        self.assertFalse(r['ok'])
        with self.assertRaises(core.PendingVerificationError): self.run_bundle(m,force_new_task=True)
        self.assertEqual(Uploader.calls,[])
    def test_pending_cannot_be_bypassed_by_workflow_option(self):
        m=self.manifest()
        with patch.object(Uploader,'upload_item',side_effect=CaxaError('lost reply',response_lost=True)):
            self.run_bundle(m,start_workflow=False)
        with self.assertRaises(core.PendingVerificationError): self.run_bundle(m,start_workflow=True)
        self.assertEqual(Uploader.calls,[])
    def test_modified_pdf_blocked_before_bridge(self):
        m=self.manifest(); Path(m['items'][0]['file_path']).write_bytes(b'CHANGED')
        with self.assertRaises(CaxaError): self.run_bundle(m)
        self.assertEqual(Uploader.calls,[])
if __name__=='__main__': unittest.main(verbosity=2)

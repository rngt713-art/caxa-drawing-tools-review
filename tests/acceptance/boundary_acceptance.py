"""Lower-level boundary acceptance using simulated helper replies only."""
import sys, unittest, tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).parent
APP=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and not sys.argv[1].startswith('-') else ROOT/'dsh_fix'/'app'
sys.path.insert(0,str(APP))
from caxa.bridge import CaxaBridge,CaxaError
from caxa.uploader import CaxaUploader,CLASS_ID_DRAWING,CLASS_ID_CAPP
import staging
class SimBridge(CaxaBridge):
    def __init__(self):super().__init__();self.sent=[];self.replies=[]
    def _spawn(self):self._proc=type('Proc',(),{'poll':lambda s:None,'terminate':lambda s:None})();self._logged_in=False
    def _send_raw(self,cmd):self.sent.append(cmd['cmd'])
    def _readline(self,timeout):return self.replies.pop(0)
class Boundary(unittest.TestCase):
    def test_new_process_relogs_before_query(self):
        b=SimBridge();b.replies=['{"ok":true,"logged_in":true}'];b.login('offline',1,'u','unused');b._proc=None
        b.replies=['{"ok":true,"logged_in":true}','{"ok":true,"items":[]}'];b.call({'cmd':'query_parts'})
        self.assertEqual(b.sent,['login','login','query_parts'])
    def test_write_lost_reply_not_replayed(self):
        b=SimBridge();b.replies=['{"ok":true,"logged_in":true}'];b.login('offline',1,'u','unused');b.replies=[None]
        with self.assertRaises(CaxaError) as c:b.call({'cmd':'upload'})
        self.assertTrue(c.exception.uncertain);self.assertEqual(b.sent.count('upload'),1)
    def test_workflow_error_keeps_id(self):
        b=SimBridge();b.replies=['{"ok":true,"logged_in":true}'];b.login('offline',1,'u','unused')
        b.replies=['{"ok":false,"error":"execute failed","stage":"execute","created_process_instance_id":123}']
        with self.assertRaises(CaxaError) as c:b.call({'cmd':'start_workflow'})
        self.assertEqual(c.exception.created_process_instance_id,123);self.assertEqual(c.exception.stage,'execute')
    def test_login_false_rejected(self):
        b=SimBridge();b.replies=['{"ok":true,"logged_in":false}']
        with self.assertRaises(CaxaError):b.login('offline',1,'u','unused')
        self.assertFalse(b._logged_in)
    def uploader_reply(self,reply):
        b=type('B',(),{'call':lambda self,cmd:reply})();return CaxaUploader(b,log=lambda *a:None)
    def test_any_append_path_requires_iteration_evidence(self):
        for method in ['high_level','low_level','append_iteration']:
            with self.subTest(method=method):
                u=self.uploader_reply(dict(attached=True,object_id=17,class_id=CLASS_ID_DRAWING,class_name='Drawing',method=method))
                u.part_drawings=lambda *a:[dict(object_id=17,name='drawing.pdf',draw_no='01.01',iterator_no='1',history_count=1)]
                with self.assertRaises(CaxaError) as c:u.attach_file(3,'offline.pdf',3,'drawing.pdf',draw_no='01.01',kind='component')
                self.assertTrue(c.exception.uncertain or c.exception.partial)
    def test_wrong_document_class_blocks(self):
        u=self.uploader_reply(dict(attached=True,object_id=17,class_id=CLASS_ID_CAPP,class_name='CappDocument'));u.part_drawings=lambda *a:[]
        with self.assertRaises(CaxaError) as c:u.attach_file(3,'offline.pdf',3,'drawing.pdf',kind='component')
        self.assertEqual(c.exception.result.get('object_id'),17)
    def test_missing_borrow_never_creates_machine_root(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            f=Path(td)/'01.01 test.pdf';f.write_bytes(b'PDF');s=staging.TaskStaging(root=td)
            u=CaxaUploader(None,log=lambda *a:None,staging_dir=s)
            u.find_product=lambda *a:dict(object_id=1);u.machine_part=lambda *a:None
            u.find_part=lambda c: dict(object_id=2) if c=='01.99' else None
            u.create_part=lambda *a: self.fail('borrow created a Part')
            with self.assertRaises(CaxaError):u.upload_item(dict(code='01.01',name='test',kind='component',machine='03.TEST',file_path=str(f)),set(),None,explicit_parent='01.99',relation_type='borrow')
            s.close()
    def test_display_query_failure_does_not_erase_upload_success(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            f=Path(td)/'01.01 test.pdf';f.write_bytes(b'PDF');s=staging.TaskStaging(root=td)
            u=CaxaUploader(None,log=lambda *a:None,staging_dir=s);u.find_part=lambda c:dict(object_id=2,name='test')
            u.attach_file=lambda *a,**kw:dict(attached=True,object_id=17,class_id=CLASS_ID_DRAWING,class_name='Drawing',iterator_no='2')
            def fail(*a):raise CaxaError('display query failed')
            u.find_product=fail
            r=u.upload_item(dict(code='01.01',name='test',kind='component',file_path=str(f)),set(),None,explicit_parent='01.99',relation_type='own')
            self.assertEqual(r['object_id'],17);s.close()
    def test_part_info_query_failure_does_not_write(self):
        writes=[]
        class B:
            def call(self,cmd):
                if cmd['cmd']=='query_parts':return dict(items=[dict(object_id=1,identity=cmd['keyword'])])
                if cmd['cmd']=='part_info':return dict(children=[],children_error='SDK error')
                writes.append(cmd);return dict(ok=True)
        u=CaxaUploader(B(),log=lambda *a:None)
        try:r=u.link_relation('01.99','01.01','borrow')
        except CaxaError:pass
        else:self.assertNotIn(r['status'],['已借用','已归属','已存在'])
        self.assertEqual(writes,[])
if __name__=='__main__':unittest.main(verbosity=2)

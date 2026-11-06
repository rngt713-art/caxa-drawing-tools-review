"""Real Tk widget smoke tests; no helper executable or network calls."""
import sys, tempfile, unittest, json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).parent
APP=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and not sys.argv[1].startswith('-') else ROOT/'dsh_fix'/'app'
sys.path[:0]=[str(ROOT/'test-deps'),str(APP)]
import upload_main as ui
class UIAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.patches=[patch.object(ui.config,'load',return_value={}),patch.object(ui.config,'save'),patch.object(ui.messagebox,'showwarning'),patch.object(ui.messagebox,'showinfo'),patch.object(ui.messagebox,'showerror')]
        for p in self.patches:p.start()
        self.app=ui.UploadApp(); self.app.withdraw(); self.app.update_idletasks()
        self.f=Path(self.tmp.name)/'01.01 test.pdf';self.f.write_bytes(b'PDF')
    def tearDown(self):
        self.app.destroy()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_create_add_freeze_unfreeze(self):
        a=self.app; a.add_pdfs([str(self.f)]);self.assertEqual(len(a.items),1)
        a.set_busy(True)
        a.add_pdfs([str(self.f)]); self.assertEqual(len(a.items),1)
        self.assertTrue(all(str(b.cget('state'))=='disabled' for b in a.action_buttons))
        a.set_busy(False); self.assertTrue(all(str(b.cget('state'))=='normal' for b in a.action_buttons))
    def test_canonical_delete_then_refresh(self):
        a=self.app; a.add_pdfs([str(self.f)],parent_code='01.99')
        self.assertEqual(len(a.relations),1)
        relation_iid=next(iid for iid,rel in a._tree_relations.items() if rel)
        a.tree.selection_set(relation_iid); a.delete_selected_relation()
        a.refresh_tree(); self.assertEqual(a.relations,[])
    def test_busy_toggle_and_delete_no_mutation(self):
        a=self.app; a.add_pdfs([str(self.f)],parent_code='01.99')
        relation_iid=next(iid for iid,rel in a._tree_relations.items() if rel)
        a.tree.selection_set(relation_iid); before=json.dumps(a.relations,sort_keys=True)
        a.set_busy(True); a.toggle_selected_relation_type(); a.delete_selected_relation()
        self.assertEqual(json.dumps(a.relations,sort_keys=True),before)
    def test_busy_bundle_load_no_mutation(self):
        a=self.app;a.add_pdfs([str(self.f)]);a.set_busy(True)
        with patch.object(ui.drawing_bundle,'load_bundle') as loader:
            a._load_bundle_from_path('unused');self.assertFalse(loader.called)
    def test_duplicate_policy_uses_chinese_labels(self):
        a=self.app
        self.assertEqual(tuple(a.duplicate_combo.cget('values')),('追加新版次','已有则复用'))
        self.assertEqual(a.duplicate_var.get(),'追加新版次')
    def test_relation_rows_are_nested_and_highlight_clears(self):
        a=self.app
        parent=Path(self.tmp.name)/'01.01 parent.pdf'; parent.write_bytes(b'PDF-P')
        child=Path(self.tmp.name)/'01.01-01 child.pdf'; child.write_bytes(b'PDF-C')
        a.add_pdfs([str(parent)])
        a.add_pdfs([str(child)],parent_code='01.01')
        parent_iid=next(iid for iid,code in a._tree_codes.items() if code=='01.01')
        child_iid=next(iid for iid,rel in a._tree_relations.items() if rel)
        self.assertEqual(a.tree.parent(child_iid),parent_iid)
        a._set_drag_target(parent_iid)
        self.assertIn('drop_target',a.tree.item(parent_iid,'tags'))
        a._drag_source_row=parent_iid
        a._show_drag_ghost(SimpleNamespace(x_root=100,y_root=100))
        self.assertIsNotNone(a._drag_ghost)
        a._clear_drag_visuals()
        self.assertIsNone(a._drag_ghost)
        self.assertNotIn('drop_target',a.tree.item(parent_iid,'tags'))
        parent2=Path(self.tmp.name)/'01.02 second-parent.pdf'; parent2.write_bytes(b'PDF-P2')
        a.add_pdfs([str(parent2)])
        a._add_relation('01.02','01.01-01')
        a.refresh_tree()
        child_relations=[iid for iid,rel in a._tree_relations.items() if rel]
        self.assertEqual(len(child_relations),2)
        a.tree.selection_set(child_relations[0]); a.delete_selected_relation()
        a.refresh_tree()
        child_item=next(item for item in a.items if item.get('code')=='01.01-01')
        self.assertEqual(len(a.relations),1)
        self.assertTrue(child_item['borrow'])
        self.assertTrue(child_item['parent_code'])
if __name__=='__main__':unittest.main(verbosity=2)

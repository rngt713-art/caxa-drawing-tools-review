"""Content binding: the uploaded bytes are always the selected file's bytes."""

import hashlib
import os
import unittest

import _support
import drawing_bundle
import staging
from caxa.bridge import CaxaError
from caxa.uploader import CaxaUploader


class StubBridge:
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240.0, idempotent=None):
        self.calls.append(cmd)
        return {"ok": True, "children": [], "attached": True}


class CaptureUploader(CaxaUploader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.links = []

    def ensure_machine(self, *args, **kwargs):
        return {"object_id": 1}, {"object_id": 2, "link_failed": False}

    def create_part(self, *args, **kwargs):
        self.created = True
        return {"object_id": 30}

    def find_part(self, code):
        if str(code).upper() in ("01.00", "03.TEST"):
            return {"object_id": 25, "identity": str(code)}
        return None

    def link_part(self, parent, child, reference, **kwargs):
        self.links.append((parent, child, reference))
        return {"ok": True}

    def attach_file(self, part_id, file_path, drawing_type, name, draw_no="", **kwargs):
        with open(file_path, "rb") as handle:
            self.sent.append(handle.read())
        return {
            "object_id": 40,
            "class_name": "CappDocument" if drawing_type == 2 else "Drawing",
            "class_id": 1003 if drawing_type == 2 else 1002,
        }


class FileBindingTests(unittest.TestCase):
    def test_rename_collision_uploads_selected_content(self):
        with _support.temp_dir() as td:
            source = os.path.join(td, "Z03.SOURCE.pdf")
            target = os.path.join(td, "Z03.TEST.pdf")
            with open(source, "wb") as handle:
                handle.write(b"NEW")
            with open(target, "wb") as handle:
                handle.write(b"OLD")

            stage = staging.TaskStaging(root=os.path.join(td, "staging"))
            uploader = CaptureUploader(StubBridge(), log=lambda *a: None, staging_dir=stage)
            item = {
                "code": "Z03.TEST",
                "name": "",
                "kind": "process_doc",
                "machine": "03.TEST",
                "file_path": source,
            }
            uploader.upload_item(item, set(), None)
            self.assertEqual(uploader.sent, [b"NEW"])
            # The unrelated OLD file was not touched or used.
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"OLD")
            with open(source, "rb") as handle:
                self.assertEqual(handle.read(), b"NEW")
            stage.close()

    def test_source_file_is_never_renamed(self):
        with _support.temp_dir() as td:
            source = os.path.join(td, "01.02 旧名.pdf")
            with open(source, "wb") as handle:
                handle.write(b"CONTENT")
            before = os.path.getmtime(source)
            stage = staging.TaskStaging(root=os.path.join(td, "staging"))
            uploader = CaptureUploader(StubBridge(), log=lambda *a: None, staging_dir=stage)
            item = {
                "code": "01.01",
                "name": "新名",
                "kind": "component",
                "machine": "",
                "file_path": source,
            }
            uploader.upload_item(item, {"01.01"}, None, explicit_parent="01.00")
            self.assertTrue(os.path.isfile(source))
            self.assertFalse(os.path.exists(os.path.join(td, "01.01 新名.pdf")))
            self.assertEqual(os.path.getmtime(source), before)
            stage.close()

    def test_staging_uses_canonical_name(self):
        with _support.temp_dir() as td:
            source = os.path.join(td, "01.02 旧名.pdf")
            with open(source, "wb") as handle:
                handle.write(b"CONTENT")
            stage = staging.TaskStaging(root=os.path.join(td, "staging"))
            staged = stage.stage(
                {"code": "01.01", "name": "新名", "file_path": source}
            )
            self.assertEqual(staged.file_name, "01.01 新名.pdf")
            with open(staged.path, "rb") as handle:
                staged_hash = hashlib.sha256(handle.read()).hexdigest()
            self.assertEqual(staged_hash, staged.sha256)
            stage.close()

    def test_conflicting_same_name_content_is_an_error(self):
        with _support.temp_dir() as td:
            a = os.path.join(td, "a.pdf")
            b = os.path.join(td, "b.pdf")
            with open(a, "wb") as handle:
                handle.write(b"AAA")
            with open(b, "wb") as handle:
                handle.write(b"BBB")
            stage = staging.TaskStaging(root=os.path.join(td, "staging"))
            stage.stage({"code": "01.01", "name": "零件", "file_path": a})
            with self.assertRaises(staging.StagingConflict):
                stage.stage({"code": "01.01", "name": "零件", "file_path": b})
            stage.close()

    def test_changed_source_since_registration_is_refused(self):
        with _support.temp_dir() as td:
            source = os.path.join(td, "01.01 零件.pdf")
            with open(source, "wb") as handle:
                handle.write(b"ORIGINAL")
            item = {"code": "01.01", "name": "零件", "file_path": source}
            drawing_bundle.register_fingerprint(item)
            with open(source, "wb") as handle:
                handle.write(b"SWAPPED")
            stage = staging.TaskStaging(root=os.path.join(td, "staging"))
            with self.assertRaises(staging.StagingError):
                stage.stage(item)
            stage.close()

    def test_uploader_staging_conflict_is_reported(self):
        with _support.temp_dir() as td:
            a = os.path.join(td, "a.pdf")
            b = os.path.join(td, "b.pdf")
            with open(a, "wb") as handle:
                handle.write(b"AAA")
            with open(b, "wb") as handle:
                handle.write(b"BBB")
            stage = staging.TaskStaging(root=os.path.join(td, "staging"))
            uploader = CaptureUploader(StubBridge(), log=lambda *a: None, staging_dir=stage)
            uploader.upload_item(
                {"code": "01.01", "name": "零件", "kind": "component",
                 "file_path": a},
                {"01.01"}, None, explicit_parent="01.00",
            )
            with self.assertRaises(staging.StagingConflict):
                uploader.upload_item(
                    {"code": "01.01", "name": "零件", "kind": "component",
                     "file_path": b},
                    {"01.01"}, None, explicit_parent="01.00",
                )
            stage.close()


if __name__ == "__main__":
    unittest.main()

"""Upload/link rules: explicit parent, own vs borrow, partial completion, class."""

import os
import unittest
import uuid

import _support
import drawing_bundle
import staging
from caxa.bridge import CaxaError
from caxa.uploader import CaxaUploader, UnverifiedResult


class RuleBridge:
    def __init__(self, upload_result=None, children=None, fail_product_link=False):
        self.calls = []
        self.link_calls = []
        self.upload_result = upload_result or {
            "ok": True,
            "attached": True,
            "object_id": 50,
            "class_name": "Drawing",
            "class_id": 1002,
            "method": "attach",
        }
        self.children = children if children is not None else []
        self.fail_product_link = fail_product_link
        # Simulate the read-back after a successful link.
        self.record_links = True
        self.reference_override = None

    def call(self, cmd, timeout=240.0, idempotent=None):
        self.calls.append(cmd)
        name = cmd.get("cmd")
        if name == "upload":
            return dict(self.upload_result)
        if name == "part_info":
            return {"ok": True, "children": list(self.children), "link_endpoints": []}
        if name == "link_part":
            self.link_calls.append(dict(cmd))
            if cmd.get("product_link") and self.fail_product_link:
                raise CaxaError("product link denied")
            if self.record_links and not cmd.get("product_link"):
                reference = cmd.get("reference")
                if self.reference_override is not None:
                    reference = self.reference_override
                self.children.append(
                    {"sub_id": cmd.get("child_id"), "reference": reference}
                )
            return {"ok": True}
        if name == "part_drawings":
            return {"ok": True, "drawings": []}
        if name == "query_parts":
            return {"ok": True, "items": []}
        if name == "find_product":
            return {"ok": True, "found": []}
        return {"ok": True}


class RuleUploader(CaxaUploader):
    def __init__(self, bridge, parts=None, log=lambda *a: None,
                 staging_root=None, **kwargs):
        if staging_root is None:
            staging_root = os.path.join(
                _support.BASE_TMP, "staging_" + uuid.uuid4().hex[:8]
            )
        kwargs["staging_dir"] = staging.TaskStaging(root=staging_root, log=log)
        super().__init__(bridge, log=log, **kwargs)
        self.parts = parts or {}
        self.created = []
        self.links = []

    def find_part(self, code):
        value = self.parts.get(str(code).upper())
        if value is None:
            return None
        return {"object_id": value, "identity": str(code)}

    def find_product(self, identity):
        return {"object_id": 1, "identity": str(identity), "name": "product"}

    def create_part(self, identity, name=""):
        self.created.append(str(identity).upper())
        return {"object_id": 70, "identity": identity}

    def link_part(self, parent, child, reference, product_link=False):
        self.links.append((parent, child, reference, product_link))
        return self.bridge.call(
            {
                "cmd": "link_part",
                "parent_id": parent,
                "child_id": child,
                "reference": reference,
                "product_link": product_link,
            }
        )


def component(code="01.01", name="零件", machine="", **extra):
    item = {"code": code, "name": name, "kind": "component", "machine": machine}
    item.update(extra)
    return item


class OwnBorrowTests(unittest.TestCase):
    def setUp(self):
        self.td = _support.temp_dir()
        self.dir = self.td.__enter__()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def make_file(self, name="01.01 零件.pdf", content=b"PDF"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_borrow_missing_child_never_creates_a_part(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={"01.02": 12})
        item = component(file_path=self.make_file("01.01 零件.pdf"))
        with self.assertRaises(CaxaError) as caught:
            uploader.upload_item(
                item, {"01.01"}, None, explicit_parent="01.02",
                relation_type="borrow",
            )
        self.assertIn("借用", str(caught.exception))
        self.assertEqual(uploader.created, [])
        self.assertEqual(bridge.link_calls, [])

    def test_new_own_child_keeps_selected_parent(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={"01.02": 12})
        item = component(file_path=self.make_file())
        uploader.upload_item(
            item, {"01.01"}, None, explicit_parent="01.02", relation_type="own"
        )
        self.assertEqual(uploader.created, ["01.01"])
        self.assertEqual(uploader.links, [(12, 70, False, False)])
        self.assertEqual(bridge.link_calls[0]["reference"], False)

    def test_explicit_missing_parent_is_not_replaced(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={"01.01": 11})
        item = component(code="01.01-02", file_path=self.make_file("01.01-02 零件.pdf"))
        with self.assertRaises(CaxaError) as caught:
            uploader.upload_item(item, {"01.01-02"}, None, explicit_parent="01.09")
        self.assertIn("01.09", str(caught.exception))
        self.assertEqual(uploader.links, [])

    def test_without_explicit_parent_prefix_candidate_is_used(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={"01.01": 11})
        item = component(code="01.01-02", file_path=self.make_file("01.01-02 零件.pdf"))
        uploader.upload_item(item, {"01.01-02"}, None)
        self.assertEqual(uploader.links[0][:3], (11, 70, False))

    def test_link_relation_missing_child_is_reported_not_created(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={"01.02": 12})
        row = uploader.link_relation("01.02", "01.99", "borrow")
        self.assertEqual(row["status"], "缺子")
        self.assertEqual(bridge.link_calls, [])
        self.assertEqual(uploader.created, [])

    def test_link_relation_missing_parent_is_reported(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={})
        row = uploader.link_relation("01.02", "01.03", "borrow")
        self.assertEqual(row["status"], "缺父")

    def test_link_relation_verifies_reference_flag(self):
        bridge = RuleBridge(children=[])
        uploader = RuleUploader(bridge, parts={"01.02": 12, "01.03": 13})
        row = uploader.link_relation("01.02", "01.03", "borrow")
        self.assertEqual(row["status"], "已借用")
        self.assertEqual(bridge.link_calls[0]["reference"], True)

    def test_link_relation_reports_unconfirmed_link(self):
        # Write succeeds but the read-back cannot see the link.
        bridge = RuleBridge(children=[])
        bridge.record_links = False
        uploader = RuleUploader(bridge, parts={"01.02": 12, "01.03": 13})
        row = uploader.link_relation("01.02", "01.03", "own")
        self.assertEqual(row["status"], "待核实")
        self.assertIn("回读", row["note"])

    def test_link_relation_reports_reference_mismatch(self):
        bridge = RuleBridge(children=[])
        bridge.reference_override = True
        uploader = RuleUploader(bridge, parts={"01.02": 12, "01.03": 13})
        row = uploader.link_relation("01.02", "01.03", "own")
        self.assertEqual(row["status"], "待核实")
        self.assertIn("reference", row["note"])

    def test_link_relation_existing_wrong_type_is_conflict(self):
        bridge = RuleBridge(children=[{"sub_id": 13, "reference": False}])
        uploader = RuleUploader(bridge, parts={"01.02": 12, "01.03": 13})
        row = uploader.link_relation("01.02", "01.03", "borrow")
        self.assertEqual(row["status"], "冲突")
        self.assertEqual(bridge.link_calls, [])

    def test_link_relation_unknown_existing_type_is_pending(self):
        bridge = RuleBridge(children=[{"sub_id": 13, "reference": None}])
        uploader = RuleUploader(bridge, parts={"01.02": 12, "01.03": 13})
        row = uploader.link_relation("01.02", "01.03", "borrow")
        self.assertEqual(row["status"], "待核实")
        self.assertEqual(bridge.link_calls, [])

    def test_part_info_error_blocks_relation_write(self):
        class ErrorBridge(RuleBridge):
            def call(self, cmd, timeout=240.0, idempotent=None):
                if cmd.get("cmd") == "part_info":
                    self.calls.append(cmd)
                    return {"ok": True, "children": [], "children_error": "SDK error"}
                return super().call(cmd, timeout, idempotent)

        bridge = ErrorBridge()
        uploader = RuleUploader(bridge, parts={"01.02": 12, "01.03": 13})
        with self.assertRaises(CaxaError):
            uploader.link_relation("01.02", "01.03", "borrow")
        self.assertEqual(bridge.link_calls, [])


class MachineAndClassTests(unittest.TestCase):
    def setUp(self):
        self.td = _support.temp_dir()
        self.dir = self.td.__enter__()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def make_file(self, name, content=b"PDF"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_machine_part_link_failure_is_partial_not_success(self):
        bridge = RuleBridge(fail_product_link=True)
        uploader = RuleUploader(bridge, parts={})
        uploader.machine_part = lambda *a, **k: None
        uploader.create_part = lambda identity, name="": {"object_id": 77}
        item = {
            "code": "03.MACH",
            "name": "整机",
            "kind": "machine_drawing",
            "machine": "03.MACH",
            "file_path": self.make_file("03.MACH 整机.pdf"),
        }
        with self.assertRaises(CaxaError) as caught:
            uploader.upload_item(item, {"03.MACH"}, None)
        self.assertTrue(getattr(caught.exception, "partial", False))
        self.assertIn("未能挂接", str(caught.exception))

    def test_wrong_document_class_is_unverified(self):
        bridge = RuleBridge(
            upload_result={
                "ok": True,
                "attached": True,
                "object_id": 60,
                "class_name": "CappDocument",
                "class_id": 1003,
                "method": "low:CappDocument",
            }
        )
        uploader = RuleUploader(bridge, parts={"01.02": 12})
        item = component(file_path=self.make_file("01.01 零件.pdf"))
        with self.assertRaises(UnverifiedResult):
            uploader.upload_item(
                item, {"01.01"}, None, explicit_parent="01.02", relation_type="own"
            )

    def test_matching_class_is_verified(self):
        bridge = RuleBridge()
        uploader = RuleUploader(bridge, parts={"01.02": 12})
        item = component(file_path=self.make_file("01.01 零件.pdf"))
        result = uploader.upload_item(
            item, {"01.01"}, None, explicit_parent="01.02", relation_type="own"
        )
        self.assertTrue(result["class_verified"])
        self.assertEqual(result["class_id"], 1002)


if __name__ == "__main__":
    unittest.main()

"""Bundle import validation: content fingerprints, schema, fields, paths."""

import json
import os
import tempfile
import unittest

import _support  # noqa: F401
import drawing_bundle


def build_bundle(directory, items, version=1):
    drawings = os.path.join(directory, "drawings")
    os.makedirs(drawings, exist_ok=True)
    manifest_items = []
    for entry in items:
        name = entry["file"]
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = entry.get("content", b"%PDF-1.4 " + name.encode("utf-8"))
        with open(path, "wb") as handle:
            handle.write(content)
        item = {key: value for key, value in entry.items() if key != "content"}
        if entry.get("with_hash", True):
            import hashlib

            item["sha256"] = hashlib.sha256(content).hexdigest()
            item["size"] = len(content)
        manifest_items.append(item)
    manifest = {
        "schema": "caxa-drawing-upload-bundle",
        "schema_version": version,
        "items": manifest_items,
        "assemblies": [],
        "relations": [],
    }
    path = os.path.join(directory, "manifest.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    return path


class LoadBundleTests(unittest.TestCase):
    def test_valid_bundle_loads_and_registers_relations(self):
        with _support.temp_dir() as td:
            path = build_bundle(
                td,
                [
                    {
                        "file": "drawings/01.01 零件.pdf",
                        "code": "01.01",
                        "name": "零件",
                        "kind": "component",
                        "machine": "",
                    }
                ],
            )
            _, manifest = drawing_bundle.load_bundle(path)
            self.assertEqual(len(manifest["items"]), 1)
            self.assertTrue(manifest["items"][0]["file_path"].endswith(".pdf"))
            self.assertEqual(manifest["relations"], [])

    def test_replaced_pdf_is_rejected(self):
        with _support.temp_dir() as td:
            path = build_bundle(
                td,
                [
                    {
                        "file": "drawings/01.01 零件.pdf",
                        "code": "01.01",
                        "name": "零件",
                        "kind": "component",
                    }
                ],
            )
            pdf = os.path.join(td, "drawings", "01.01 零件.pdf")
            with open(pdf, "wb") as handle:
                handle.write(b"%PDF-REPLACED")
            with self.assertRaises(ValueError) as caught:
                drawing_bundle.load_bundle(path)
            self.assertIn("内容校验失败", str(caught.exception))

    def test_unknown_version_is_rejected(self):
        with _support.temp_dir() as td:
            path = build_bundle(
                td,
                [
                    {
                        "file": "drawings/01.01 零件.pdf",
                        "code": "01.01",
                        "name": "零件",
                        "kind": "component",
                    }
                ],
                version=99,
            )
            with self.assertRaises(ValueError) as caught:
                drawing_bundle.load_bundle(path)
            self.assertIn("版本不支持", str(caught.exception))

    def test_path_traversal_is_rejected(self):
        with _support.temp_dir() as td:
            path = build_bundle(
                td,
                [
                    {
                        "file": "../escape.pdf",
                        "code": "01.01",
                        "name": "零件",
                        "kind": "component",
                    }
                ],
            )
            with self.assertRaises(ValueError):
                drawing_bundle.load_bundle(path)

    def test_missing_fingerprint_is_registered_on_import(self):
        with _support.temp_dir() as td:
            path = build_bundle(
                td,
                [
                    {
                        "file": "drawings/01.02 零件.pdf",
                        "code": "01.02",
                        "name": "零件",
                        "kind": "component",
                        "with_hash": False,
                    }
                ],
            )
            _, manifest = drawing_bundle.load_bundle(path)
            item = manifest["items"][0]
            self.assertTrue(item["sha256"])
            self.assertEqual(drawing_bundle.verify_fingerprint(item), [])
            with open(item["file_path"], "wb") as handle:
                handle.write(b"changed")
            self.assertTrue(drawing_bundle.verify_fingerprint(item))


class FieldValidationTests(unittest.TestCase):
    def test_process_doc_requires_machine(self):
        item = {
            "code": "Z03.OV.04-05030",
            "name": "",
            "kind": "process_doc",
            "machine": "",
            "file_path": __file__,
            "sha256": "",
            "size": 0,
        }
        problems = drawing_bundle.item_problems(item, check_content=False)
        self.assertTrue(any("成品代码" in problem for problem in problems))

    def test_kind_must_match_code_rule(self):
        item = {
            "code": "03.OV.01",
            "name": "整机",
            "kind": "component",
            "machine": "03.OV.01",
        }
        problems = drawing_bundle.item_problems(item, check_content=False)
        self.assertTrue(any("类型" in problem for problem in problems))

    def test_unknown_kind_is_rejected(self):
        item = {"code": "01.01", "name": "零件", "kind": "unknown", "machine": ""}
        problems = drawing_bundle.item_problems(item, check_content=False)
        self.assertTrue(problems)

    def test_digit_code_requires_name(self):
        item = {"code": "01.01", "name": "", "kind": "component", "machine": ""}
        problems = drawing_bundle.item_problems(item, check_content=False)
        self.assertTrue(any("中文名称" in problem for problem in problems))

    def test_duplicate_codes_are_rejected(self):
        manifest = {
            "schema": drawing_bundle.SCHEMA,
            "schema_version": 1,
            "items": [
                {"code": "01.01", "name": "甲", "kind": "component", "machine": ""},
                {"code": "01.01", "name": "乙", "kind": "component", "machine": ""},
            ],
        }
        errors = drawing_bundle.validate_bundle(manifest)
        self.assertTrue(any("图号重复" in error for error in errors))

    def test_filename_mismatch_is_a_warning_not_an_error(self):
        with _support.temp_dir() as td:
            item = {
                "code": "01.02",
                "name": "新名称",
                "kind": "component",
                "machine": "",
                "file_path": os.path.join(td, "01.01 旧名称.pdf"),
            }
            self.assertEqual(
                drawing_bundle.item_problems(item, check_content=False), []
            )
            warnings = drawing_bundle.filename_warnings(item)
            self.assertTrue(warnings)
            self.assertIn("01.02 新名称.pdf", warnings[0])
            self.assertEqual(
                drawing_bundle.expected_drawing_filename("01.02", "新名称"),
                "01.02 新名称.pdf",
            )


if __name__ == "__main__":
    unittest.main()

"""Relation rules: single source, own/borrow by tree, stability, conflicts."""

import os
import unittest

import _support  # noqa: F401  (sets sys.path)
import relations
import drawing_bundle
import upload_core


class FakeTree:
    def __init__(self, existing):
        self.existing = {code.upper() for code in existing}

    def find_part(self, code):
        return {"object_id": 1} if str(code).upper() in self.existing else None


def pairs(manifest):
    return {
        (rel["parent_code"], rel["child_code"]) for rel in manifest.get("relations") or []
    }


class MigrationTests(unittest.TestCase):
    def test_legacy_collections_are_migrated_once(self):
        manifest = {
            "items": [],
            "assemblies": [{"machine": "03.TEST", "codes": ["01.01"]}],
            "manual_borrows": [{"machine": "01.02", "codes": ["01.03"]}],
        }
        drawing_bundle.normalize_manifest_relations(manifest)
        self.assertEqual(
            pairs(manifest),
            {("03.TEST", "01.01"), ("01.02", "01.03")},
        )
        self.assertEqual(manifest["relations_migrated"], 1)

    def test_deleted_relation_does_not_come_back(self):
        manifest = {
            "items": [],
            "assemblies": [{"machine": "03.TEST", "codes": ["01.01"]}],
        }
        drawing_bundle.normalize_manifest_relations(manifest)
        manifest["relations"] = []  # what delete_selected_relation leaves behind
        drawing_bundle.normalize_manifest_relations(manifest)
        self.assertEqual(manifest["relations"], [])

    def test_item_parent_is_kept_as_source(self):
        manifest = {
            "items": [
                {"code": "01.02", "kind": "component", "parent_code": "01.01"},
            ]
        }
        drawing_bundle.normalize_manifest_relations(manifest)
        self.assertEqual(pairs(manifest), {("01.01", "01.02")})
        self.assertEqual(manifest["relations"][0]["source"], "drag")

    def test_new_item_after_migration_is_added_once(self):
        manifest = {"items": [{"code": "01.01", "kind": "component"}]}
        drawing_bundle.normalize_manifest_relations(manifest)
        manifest["items"].append(
            {"code": "01.02", "kind": "component", "parent_code": "01.01"}
        )
        drawing_bundle.normalize_manifest_relations(manifest)
        self.assertEqual(pairs(manifest), {("01.01", "01.02")})
        # Deleting it again must be permanent.
        manifest["relations"] = []
        manifest["items"][1]["parent_code"] = ""
        drawing_bundle.normalize_manifest_relations(manifest)
        self.assertEqual(pairs(manifest), set())


class TreeDecisionTests(unittest.TestCase):
    def test_absent_child_defaults_to_own(self):
        tree = FakeTree([])
        rels = [{"parent_code": "01.01", "child_code": "01.02"}]
        upload_core.refine_relations_from_tree(tree, rels, log=lambda *a: None)
        self.assertEqual(rels[0]["link_type"], "own")

    def test_existing_child_defaults_to_borrow(self):
        tree = FakeTree(["01.02"])
        rels = [{"parent_code": "01.01", "child_code": "01.02"}]
        upload_core.refine_relations_from_tree(tree, rels, log=lambda *a: None)
        self.assertEqual(rels[0]["link_type"], "borrow")

    def test_machine_prefix_does_not_force_borrow(self):
        tree = FakeTree([])
        rels = [{"parent_code": "03.MACH", "child_code": "01.02"}]
        upload_core.refine_relations_from_tree(tree, rels, log=lambda *a: None)
        self.assertEqual(rels[0]["link_type"], "own")

    def test_manual_override_wins_over_tree(self):
        tree = FakeTree(["01.02"])
        rels = [
            {
                "parent_code": "01.01",
                "child_code": "01.02",
                "link_type": "own",
                "manual_override": True,
            }
        ]
        upload_core.refine_relations_from_tree(tree, rels, log=lambda *a: None)
        self.assertEqual(rels[0]["link_type"], "own")
        self.assertTrue(rels[0]["manual_override"])

    def test_retry_does_not_flip_own_to_borrow(self):
        rels = [{"parent_code": "01.01", "child_code": "01.02"}]
        upload_core.refine_relations_from_tree(
            FakeTree([]), rels, log=lambda *a: None
        )
        self.assertEqual(rels[0]["link_type"], "own")
        # The first attempt created the Part; a retry must keep the decision.
        upload_core.refine_relations_from_tree(
            FakeTree(["01.02"]), rels, log=lambda *a: None
        )
        self.assertEqual(rels[0]["link_type"], "own")
        # Only an explicit re-check may change it.
        upload_core.refine_relations_from_tree(
            FakeTree(["01.02"]), rels, log=lambda *a: None, force=True
        )
        self.assertEqual(rels[0]["link_type"], "borrow")


class ConflictTests(unittest.TestCase):
    def test_cycle_is_detected(self):
        analysis = relations.analyze_relations(
            [
                {"parent_code": "01.01", "child_code": "01.02", "link_type": "own"},
                {"parent_code": "01.02", "child_code": "01.01", "link_type": "own"},
            ]
        )
        self.assertFalse(analysis["ok"])
        self.assertEqual(analysis["issues"][0]["type"], "cycle")

    def test_own_child_with_two_parents_is_detected(self):
        analysis = relations.analyze_relations(
            [
                {"parent_code": "01.01", "child_code": "01.03", "link_type": "own"},
                {"parent_code": "01.02", "child_code": "01.03", "link_type": "own"},
            ]
        )
        self.assertFalse(analysis["ok"])
        types = {issue["type"] for issue in analysis["issues"]}
        self.assertIn("own_multi_parent", types)

    def test_borrow_with_two_parents_is_allowed(self):
        analysis = relations.analyze_relations(
            [
                {"parent_code": "01.01", "child_code": "01.03", "link_type": "borrow"},
                {"parent_code": "01.02", "child_code": "01.03", "link_type": "borrow"},
            ]
        )
        self.assertTrue(analysis["ok"])


if __name__ == "__main__":
    unittest.main()

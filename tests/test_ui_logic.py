"""User-facing labels must not leak implementation policy names."""

import unittest

import _support  # noqa: F401
import upload_ui_logic


class DuplicatePolicyLabelTests(unittest.TestCase):
    def test_policy_labels_are_chinese(self):
        self.assertEqual(
            upload_ui_logic.duplicate_policy_label("append_iteration"),
            "追加新版次",
        )
        self.assertEqual(
            upload_ui_logic.duplicate_policy_label("reuse"),
            "已有则复用",
        )

    def test_chinese_label_maps_back_to_internal_policy(self):
        self.assertEqual(
            upload_ui_logic.duplicate_policy_value("追加新版次"),
            "append_iteration",
        )
        settings = upload_ui_logic.snapshot_settings(
            ip="127.0.0.1",
            port=8900,
            user="tester",
            password="unused",
            duplicate_policy="追加新版次",
            start_workflow=False,
        )
        self.assertEqual(settings["duplicate_policy"], "append_iteration")


if __name__ == "__main__":
    unittest.main()

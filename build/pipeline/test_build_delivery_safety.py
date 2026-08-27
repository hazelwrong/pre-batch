import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_delivery as build
import checks


class BuildDeliverySafetyTest(unittest.TestCase):
    def test_publish_refuses_non_delivery_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ordinary"
            target.mkdir()
            (target / "keep.txt").write_text("user data", encoding="utf-8")
            with mock.patch.object(build, "PUBLISH_TO", str(target)):
                with self.assertRaises(SystemExit):
                    build.publish()
            self.assertEqual((target / "keep.txt").read_text(), "user data")

    def test_clean_tree_refuses_non_delivery_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "delivery"
            target.mkdir()
            (target / "keep.txt").write_text("user data", encoding="utf-8")
            with mock.patch.object(build, "DELIVERY", str(target)):
                with self.assertRaises(SystemExit):
                    build.clean_tree([])
            self.assertEqual((target / "keep.txt").read_text(), "user data")

    def test_clean_tree_accepts_an_owned_previous_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "delivery"
            (target / "manifests").mkdir(parents=True)
            (target / "tasks.jsonl").write_text("{}\n", encoding="utf-8")
            (target / "stale.txt").write_text("old", encoding="utf-8")
            with mock.patch.object(build, "DELIVERY", str(target)):
                build.clean_tree([])
            self.assertFalse((target / "stale.txt").exists())
            self.assertTrue((target / "manifests").is_dir())

    def test_scratch_copy_can_be_normalised_when_source_was_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "evidence.pdf"
            source.write_bytes(b"evidence")
            source.chmod(0o444)
            scratch = Path(tmp) / "scratch.pdf"
            scratch.write_bytes(source.read_bytes())
            scratch.chmod(source.stat().st_mode)

            build._make_writable_copy(scratch)

            self.assertEqual(source.stat().st_mode & 0o777, 0o444)
            self.assertTrue(scratch.stat().st_mode & 0o200)

    def test_explicit_human_rubric_check_is_not_a_failed_checker(self):
        item = {
            "verification": "A reviewer compares the public copy to the source.",
            "check": {"human": True, "reason": "Requires professional judgement."},
        }

        status, detail, kind = checks.execute(item, None)

        self.assertEqual(status, "not_auto_evaluated")
        self.assertEqual(detail, "Requires professional judgement.")
        self.assertEqual(kind, "human_judgement")


if __name__ == "__main__":
    unittest.main()

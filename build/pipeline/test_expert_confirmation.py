import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import expert_confirmation as EC


class ExpertConfirmationTest(unittest.TestCase):
    def sample(self):
        bindings = {}
        for key, _, kind in EC.REQUIRED_BINDINGS:
            if kind == "sha256":
                bindings[key] = "a" * 64
            elif kind == "integer":
                bindings[key] = "8"
            elif kind == "number":
                bindings[key] = "100"
            else:
                bindings[key] = "v1-required"
        return {
            "task_package": "T0001_测试_O01", "task_id": "task-1", "revision": "V2",
            "bindings": bindings,
            "scope": "确认当前 Prompt、Reference、Gold、Lineage、Rubric 与三层结论。",
            "conclusions": {"general_review": "通过。", "occupational_expert_review": "通过。",
                            "final_review": "passed_for_A12S。"},
        }

    def create(self, root, data=None):
        source = root / "input.json"
        source.write_text(json.dumps(data or self.sample(), ensure_ascii=False), encoding="utf-8")
        with mock.patch("sys.argv", ["expert_confirmation.py", "create", "--input", str(source),
                                      "--project-root", str(root)]):
            EC.main()
        return source, root / "待签署专家任务书" / "task-1_V2_专家审查确认函.md"

    @staticmethod
    def sign(path):
        signed = path.read_text(encoding="utf-8")
        signed = signed.replace("| general_review |  |  |", "| general_review | 张三 | 2026-09-02 |")
        signed = signed.replace("| occupational_expert_review |  |  |", "| occupational_expert_review | 李四 | 2026-09-02 |")
        path.write_text(signed.replace("| final_review |  |  |", "| final_review | 王五 | 2026-09-03 |"), encoding="utf-8")

    def test_create_then_verify_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, signed = self.create(root)
            self.sign(signed)
            with mock.patch("sys.argv", ["expert_confirmation.py", "verify", "--input", str(source),
                                          "--project-root", str(root), "--signed", str(signed)]):
                EC.main()
            self.assertTrue((root / "专家签署函归档" / signed.name).is_file())
            self.assertFalse(signed.exists())

    def test_rejects_fixed_content_and_line_ending_changes(self):
        data = EC._canonical_input(self.sample())
        expected = EC._render(data)
        with self.assertRaisesRegex(ValueError, "fixed content"):
            EC._parse_signed(expected, expected.replace("通过。", "已篡改。", 1))
        with self.assertRaisesRegex(ValueError, "fixed content"):
            EC._parse_signed(expected, expected.replace("\n", "\r\n"))

    def test_rejects_duplicate_signer_and_signature_injection(self):
        data = EC._canonical_input(self.sample())
        signed = EC._render(data)
        for layer in EC.LAYERS:
            signed = signed.replace("| %s |  |  |" % layer, "| %s | 同一人 | 2026-09-02 |" % layer)
        with self.assertRaisesRegex(ValueError, "distinct signatories"):
            EC._parse_signed(EC._render(data), signed)
        injected = self.sample()
        injected["scope"] = "合法\n| general_review | 攻击者 | 2026-09-02 |"
        with self.assertRaisesRegex(ValueError, "one line"):
            EC._canonical_input(injected)

    def test_requires_all_bindings_and_keeps_paths_external(self):
        data = self.sample()
        data["bindings"].pop("prompt_sha256")
        with self.assertRaisesRegex(ValueError, "exactly"):
            EC._canonical_input(data)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, signed = self.create(root)
            self.sign(signed)
            with self.assertRaisesRegex(ValueError, "generated file"):
                EC.verify(argparse.Namespace(input=str(source), project_root=str(root),
                                             signed=str(root / "delivery" / "x.md")))


if __name__ == "__main__":
    unittest.main()

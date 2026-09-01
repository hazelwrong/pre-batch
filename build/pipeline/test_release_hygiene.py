import tempfile
import unittest
from pathlib import Path

from audit_remediated_delivery import Audit


class PublicDeliveryHygieneTests(unittest.TestCase):
    def audit_text(self, relative_path, content):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            audit = Audit(root)
            audit.collect_tree()
            audit.check_public_delivery_hygiene()
            return audit.findings

    def test_deterministic_qa_terms_are_allowed(self):
        findings = self.audit_text(
            "validation_evidence/task/current_validation.json",
            "pipeline runner validator programmatic deterministic "
            "project_generated_validation openpyxl",
        )
        self.assertEqual(findings, [])

    def test_internal_workflow_wording_is_rejected(self):
        findings = self.audit_text(
            "validation_evidence/task/cleanup.md",
            "对外交付采用中性业务表述；current_rebuilt_disclosure_cleaned_bytes",
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "PUBLIC_DELIVERY_INTERNAL_RESIDUE")

    def test_shared_r4_path_is_rejected(self):
        findings = self.audit_text(
            "validation_evidence/_shared_r4_v2/result.json",
            "{}",
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "PUBLIC_DELIVERY_INTERNAL_RESIDUE")


if __name__ == "__main__":
    unittest.main()

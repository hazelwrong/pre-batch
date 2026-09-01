import hashlib
import json
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


class DeliverableAuthenticityTests(unittest.TestCase):
    def audit_provenance(self, *, source_type, current=b"current gold",
                         source_sha=None, overrides=None, task_urls=None,
                         inventory_overrides=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = "deliverable_files/bundle/Gold.pdf"
            target = root / rel
            target.parent.mkdir(parents=True)
            target.write_bytes(current)
            lineage_rel = "validation_evidence/task/source_to_gold_lineage.json"
            lineage = root / lineage_rel
            lineage.parent.mkdir(parents=True)
            lineage.write_text('{"source":"SRC-1","transform":"redacted"}',
                               encoding="utf-8")
            current_sha = hashlib.sha256(current).hexdigest()
            source_sha = source_sha or hashlib.sha256(b"authentic source").hexdigest()
            inventory = {
                "source_id": "SRC-1", "source_type": "desensitization",
                "adopted": True, "canonical_url": "https://example.org/source.pdf",
                "source_sha256": source_sha,
            }
            inventory.update(inventory_overrides or {})
            manifests = root / "manifests"
            manifests.mkdir()
            (manifests / "source_inventory.jsonl").write_text(
                json.dumps(inventory) + "\n", encoding="utf-8")
            row = {
                "path": rel, "content_sha256": current_sha, "bytes": len(current),
                "source_type": source_type,
                "source_url": "https://example.org/source.pdf",
                "source_sha256": source_sha,
                "current_sha256": current_sha,
                "source_record_id": "SRC-1",
                "transformation_record": "removed direct identifiers and rebuilt layout",
                "lineage_path": lineage_rel,
                "lineage_sha256": hashlib.sha256(lineage.read_bytes()).hexdigest(),
            }
            row.update(overrides or {})
            (manifests / "provenance_manifest.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8")
            audit = Audit(root)
            audit.collect_tree()
            audit.tasks = [{"deliverable_files": [rel],
                            "deliverable_file_urls": task_urls or []}]
            audit.check_provenance()
            return [finding.code for finding in audit.findings
                    if finding.code != "PROVENANCE_COVERAGE"]

    def test_desensitized_gold_passes_with_source_current_transform_and_lineage(self):
        self.assertEqual(self.audit_provenance(source_type="desensitization"), [])

    def test_desensitized_gold_fails_without_explicit_transformation(self):
        codes = self.audit_provenance(
            source_type="desensitization", overrides={"transformation_record": ""})
        self.assertIn("DELIVERABLE_TRANSFORMATION_RECORD", codes)

    def test_desensitized_gold_fails_on_current_or_lineage_hash_mismatch(self):
        codes = self.audit_provenance(
            source_type="desensitization",
            overrides={"current_sha256": "0" * 64, "lineage_sha256": "1" * 64})
        self.assertIn("DELIVERABLE_CURRENT_HASH_MISMATCH", codes)
        self.assertIn("DELIVERABLE_LINEAGE_HASH_MISMATCH", codes)

    def test_desensitized_gold_fails_without_adopted_matching_source(self):
        codes = self.audit_provenance(
            source_type="desensitization",
            inventory_overrides={"source_sha256": "2" * 64})
        self.assertIn("DELIVERABLE_SOURCE_RECORD_MISMATCH", codes)

    def test_generated_deliverable_cannot_use_transformed_path(self):
        codes = self.audit_provenance(source_type="generated_deliverable")
        self.assertIn("DELIVERABLE_SOURCE_TYPE", codes)

    def test_exact_source_copy_still_fails_on_byte_mismatch(self):
        codes = self.audit_provenance(
            source_type="real_input_and_real_deliverable",
            overrides={"transformation_record": "none; exact source bytes"},
            task_urls=["https://example.org/source.pdf"])
        self.assertIn("DELIVERABLE_SOURCE_HASH_MISMATCH", codes)

    def test_exact_source_copy_passes_when_bytes_and_url_match(self):
        current = b"authentic source"
        digest = hashlib.sha256(current).hexdigest()
        self.assertEqual(self.audit_provenance(
            source_type="real_input_and_real_deliverable", current=current,
            source_sha=digest,
            overrides={"transformation_record": "none; exact source bytes"},
            task_urls=["https://example.org/source.pdf"]), [])

if __name__ == "__main__":
    unittest.main()

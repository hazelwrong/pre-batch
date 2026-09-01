import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from review_contract import ReviewContractError, prepare_review_input


class ReviewInputContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_id = "11111111-1111-5111-8111-111111111111"
        self.task = self.root / "tasks" / self.task_id
        self.delivery = self.root / "delivery"
        self.task.mkdir(parents=True)
        (self.delivery / "deliverable_files" / "bundle").mkdir(parents=True)
        (self.delivery / "reference_files" / "bundle").mkdir(parents=True)
        self.deliverable = self.delivery / "deliverable_files" / "bundle" / "Original.xlsx"
        self.deliverable.write_bytes(b"real deliverable bytes")
        self.reference = self.delivery / "reference_files" / "bundle" / "Policy.md"
        self.reference.write_text("policy", encoding="utf-8")
        self.write("task_meta.json", {
            "task_id": self.task_id, "task_name": "Procurement review",
            "sector": "Manufacturing", "occupation": "Buyers and Purchasing Agents",
            "language": "English", "rubric_version": "v1", "item_codes": ["R01"],
        })
        self.write("rubric.json", [{"rubric_item_id": "r1", "score": 100,
                                    "required": True, "criterion": "Correct"}])
        (self.task / "prompt.md").write_text("Do the work.", encoding="utf-8")
        (self.task / "rubric_pretty.txt").write_text("Correct - 100", encoding="utf-8")
        digest = hashlib.sha256(b"real deliverable bytes").hexdigest()
        self.write("gold_provenance.json", {
            "source_type": "real_input_and_real_deliverable",
            "real_deliverable_files": [{
                "filename": "Original.xlsx", "source_url": "https://example.org/Original.xlsx",
                "source_sha256": digest,
                "rights_holder": "Example Ltd", "license": "Licensed project use",
                "acquired_at": "2026-08-20",
            }],
        })
        self.write("source_inventory.json", [{
            "source_id": "SRC-01", "source_type": "official_publication",
            "description": "Public policy source", "source_url": "https://example.org/policy",
            "license": "Publicly available for project reference", "adopted": True,
        }])
        (self.task / "owner-authorization.md").write_bytes(b"owner authorization")
        self.write("provenance.json", {
            "defaults": {
                "rights_holder": "Example Ltd", "license": "Licensed project use",
                "usage_scope": "Client-controlled GDPval evaluation",
                "usage_boundaries": {
                    "public_release": "not_authorized", "internal_use": "authorized",
                    "third_party_redistribution": "not_authorized",
                    "sublicensing": "not_authorized",
                },
                "project_use_authorization": {
                    "status": "client_confirmed_internal_use",
                    "confirmed_by": "Client Owner", "role": "Client project owner",
                    "confirmed_at": "2026-08-24T10:26:00+08:00",
                    "task_id": self.task_id, "scope": "single_task_internal_gdpval",
                    "evidence_file": "owner-authorization.md",
                    "evidence_sha256": hashlib.sha256(
                        b"owner authorization").hexdigest(),
                },
            },
        })
        self.write("expert_profiles.json", [{
            "expert_id": "E%02d" % index, "alias": alias, "expert_role": role,
            "review_layer": layer, "required_industry": "Manufacturing",
            "required_occupation": "Buyers and Purchasing Agents",
            "review_scope": "Review only the assigned layer and current task basis.",
            "expert_profile": "Experienced reviewer", "strengths": ["Evidence"],
            "first_thought": "Check the current package",
        } for index, (alias, role, layer) in enumerate((
            ("李明", "通用审查", "general_review"),
            ("周宁", "职业专家审查", "occupational_expert_review"),
            ("陈洁", "终审", "final_review")), start=1)])
        row = {
            "task_id": self.task_id, "sector": "Manufacturing",
            "occupation": "Buyers and Purchasing Agents", "prompt": "Do the work.",
            "reference_files": ["reference_files/bundle/Policy.md"],
            "reference_file_urls": [], "reference_file_hf_uris": [],
            "deliverable_files": ["deliverable_files/bundle/Original.xlsx"],
            "deliverable_file_urls": ["https://example.org/Original.xlsx"],
            "deliverable_file_hf_uris": [], "rubric_pretty": "Correct - 100",
            "rubric_json": "[]",
        }
        (self.delivery / "tasks.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        self.policy = {"human_review": {
            "expert_profiles_required_per_task": 3,
            "expert_profile_required_fields": [
                "expert_id", "alias", "expert_role", "expert_profile",
                "review_layer", "required_industry", "required_occupation",
                "review_scope", "strengths", "first_thought"],
        }}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        (self.task / name).write_text(json.dumps(value), encoding="utf-8")

    def test_manifest_binds_sources_profiles_rights_and_all_current_files(self):
        manifest = prepare_review_input(
            self.task, self.delivery, self.task_id, self.policy,
            {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertEqual(manifest["schema_version"], "review-input-v1")
        self.assertEqual(len(manifest["expert_profiles"]), 3)
        self.assertEqual(manifest["deliverable_sources"][0]["source_sha256"],
                         hashlib.sha256(b"real deliverable bytes").hexdigest())
        self.assertTrue(any(item["path"] == "prompt.md" for item in manifest["files"]))

    def test_missing_profile_fails_before_reviewer_package_generation(self):
        self.write("expert_profiles.json", [])
        with self.assertRaises(ReviewContractError):
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})

    def test_transformed_deliverable_bytes_are_rejected(self):
        self.deliverable.write_bytes(b"modified")
        with self.assertRaises(ReviewContractError):
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})

    def test_desensitized_deliverable_requires_and_binds_full_lineage(self):
        self.deliverable.write_bytes(b"task scoped reconstructed gold")
        current_sha = hashlib.sha256(self.deliverable.read_bytes()).hexdigest()
        source_sha = hashlib.sha256(b"authentic source bytes").hexdigest()
        lineage = self.task / "source_to_gold_lineage.json"
        lineage.write_text('{"source":"SRC-GOLD","steps":["redact","rebuild"]}',
                           encoding="utf-8")
        provenance = json.loads(
            (self.task / "gold_provenance.json").read_text(encoding="utf-8"))
        provenance["source_type"] = "desensitization"
        provenance["real_deliverable_files"][0].update({
            "source_type": "desensitization",
            "source_url": "https://example.org/authentic.pdf",
            "source_sha256": source_sha,
            "current_sha256": current_sha,
            "source_record_id": "SRC-GOLD",
            "transformation_record": "redacted identifiers and rebuilt task layout",
            "lineage_path": "source_to_gold_lineage.json",
            "lineage_sha256": hashlib.sha256(lineage.read_bytes()).hexdigest(),
        })
        self.write("gold_provenance.json", provenance)
        inventory = json.loads(
            (self.task / "source_inventory.json").read_text(encoding="utf-8"))
        inventory.append({
            "source_id": "SRC-GOLD", "source_type": "desensitization",
            "description": "Authentic source for reconstructed gold",
            "source_url": "https://example.org/authentic.pdf",
            "source_sha256": source_sha, "license": "Project reference only",
            "adopted": True, "transformation_record": "registered separately",
        })
        self.write("source_inventory.json", inventory)
        manifest = prepare_review_input(
            self.task, self.delivery, self.task_id, self.policy,
            {"digest": "a" * 64}, {"digest": "b" * 64})
        source = manifest["deliverable_sources"][0]
        self.assertEqual(source["current_sha256"], current_sha)
        self.assertEqual(source["source_record_id"], "SRC-GOLD")

        provenance["real_deliverable_files"][0]["transformation_record"] = ""
        self.write("gold_provenance.json", provenance)
        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("transformation record", str(caught.exception))

    def test_duplicate_deliverable_basenames_require_explicit_paths(self):
        second = self.delivery / "deliverable_files" / "second" / "Original.xlsx"
        second.parent.mkdir(parents=True)
        second.write_bytes(b"second source bytes")
        row = json.loads((self.delivery / "tasks.jsonl").read_text(encoding="utf-8"))
        row["deliverable_files"].append(
            "deliverable_files/second/Original.xlsx")
        (self.delivery / "tasks.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        provenance = json.loads(
            (self.task / "gold_provenance.json").read_text(encoding="utf-8"))
        provenance["real_deliverable_files"].append({
            "filename": "Original.xlsx",
            "source_url": "https://example.org/second/Original.xlsx",
            "source_sha256": hashlib.sha256(b"second source bytes").hexdigest(),
            "rights_holder": "Example Ltd", "license": "Licensed project use",
            "acquired_at": "2026-08-20",
        })
        self.write("gold_provenance.json", provenance)

        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("unambiguous delivery path", str(caught.exception))

        provenance["real_deliverable_files"][0]["path"] = \
            "deliverable_files/bundle/Original.xlsx"
        provenance["real_deliverable_files"][1]["path"] = \
            "deliverable_files/second/Original.xlsx"
        self.write("gold_provenance.json", provenance)
        manifest = prepare_review_input(
            self.task, self.delivery, self.task_id, self.policy,
            {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertEqual(
            {item["path"] for item in manifest["deliverable_sources"]},
            set(row["deliverable_files"]))

    def test_deliverable_source_requires_per_file_rights_and_acquisition(self):
        provenance = json.loads(
            (self.task / "gold_provenance.json").read_text(encoding="utf-8"))
        provenance["real_deliverable_files"][0].pop("acquired_at")
        self.write("gold_provenance.json", provenance)
        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("acquired_at", str(caught.exception))

    def test_synthetic_reference_source_is_rejected(self):
        inventory = json.loads(
            (self.task / "source_inventory.json").read_text(encoding="utf-8"))
        inventory[0]["source_type"] = "synthetic"
        self.write("source_inventory.json", inventory)
        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("cannot use source_type synthetic", str(caught.exception))

    def test_desensitized_reference_requires_transformation_record(self):
        inventory = json.loads(
            (self.task / "source_inventory.json").read_text(encoding="utf-8"))
        inventory[0]["source_type"] = "desensitization"
        self.write("source_inventory.json", inventory)
        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("transformation record", str(caught.exception))

    def test_prompt_and_rubric_must_match_declared_language(self):
        rubric = json.loads((self.task / "rubric.json").read_text(encoding="utf-8"))
        rubric[0]["criterion"] = "必须逐项核对全部证据并准确记录最终结论。"
        rubric[0]["verification"] = "检查交付文件中的对应字段和支持材料。"
        self.write("rubric.json", rubric)
        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("language does not match", str(caught.exception))

    def test_rubric_pretty_must_match_declared_language(self):
        (self.task / "rubric_pretty.txt").write_text(
            "评分标准要求逐项核对全部证据，并准确记录最终结论。",
            encoding="utf-8")
        with self.assertRaises(ReviewContractError) as caught:
            prepare_review_input(
                self.task, self.delivery, self.task_id, self.policy,
                {"digest": "a" * 64}, {"digest": "b" * 64})
        self.assertIn("rubric_pretty language", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

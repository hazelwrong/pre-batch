import hashlib
import json
import os
import tempfile
import unittest

import spec_checks as checks


POLICY = {
    "human_review": {"expert_rejection_required": True},
    "rubric": {"gold_must_not_score_full": True,
                "required_field_default": True,
                "required_field_allowed": [True, False],
                "item_count_distribution": {"mean": 30, "stddev": 10,
                                              "lower_bound": 25}},
}


class UnverifiedHumanEvidenceTest(unittest.TestCase):
    def test_placeholder_expert_objection_does_not_count_as_human_rejection(self):
        result = checks.expert_rejection_recorded({
            "occupational_expert_review": [{
                "reviewer": None,
                "title": None,
                "date": None,
                "counts_toward_acceptance": False,
                "adoption_rounds": [{"objected": ["R01"]}],
            }],
        }, POLICY)

        self.assertEqual(result[1], "not_run")


class ProjectUsePermissionTest(unittest.TestCase):
    def test_redistribution_restriction_needs_project_authorization(self):
        result = checks.license_permits_delivery({}, {
            "defaults": {
                "license": "Copyright; no open redistribution license identified",
                "usage_scope": "Internal evaluation only",
            },
        }, {})

        self.assertEqual(result[1], "failed")

    def test_client_confirmed_internal_use_passes_with_restriction(self):
        with tempfile.TemporaryDirectory() as task_root:
            evidence = os.path.join(task_root, "owner-authorization.md")
            with open(evidence, "wb") as fh:
                fh.write(b"client authorization")
            result = checks.license_permits_delivery({"task_id": "task-a"}, {
                "defaults": {
                    "license": "Copyright; external redistribution is prohibited",
                    "usage_scope": "Client-controlled internal GDPval use",
                    "project_use_authorization": {
                        "status": "client_confirmed_internal_use",
                        "confirmed_by": "Client Owner", "role": "Client project owner",
                        "confirmed_at": "2026-08-24T10:26:00+08:00",
                        "task_id": "task-a", "scope": "single_task_internal_gdpval",
                        "evidence_file": "owner-authorization.md",
                        "evidence_sha256": hashlib.sha256(
                            b"client authorization").hexdigest(),
                        "usage_boundaries": {
                            "public_release": "not_authorized",
                            "internal_use": "authorized",
                            "third_party_redistribution": "not_authorized",
                            "sublicensing": "not_authorized",
                        },
                    },
                },
            }, {}, task_root=task_root)

        self.assertEqual(result[1], "passed")
        self.assertIn("对外再分发仍受限", result[2])

    def test_pending_clearance_still_fails(self):
        result = checks.license_permits_delivery({}, {
            "defaults": {
                "license": "Copyright material",
                "usage_scope": "pending written rights clearance",
                "project_use_authorization": {
                    "status": "client_confirmed_internal_use",
                },
            },
        }, {})

        self.assertEqual(result[1], "failed")

    def test_government_work_without_restriction_still_passes(self):
        result = checks.license_permits_delivery({}, {
            "defaults": {
                "license": "U.S. Government work; 17 U.S.C. 105",
                "usage_scope": "Project use with attribution",
                "usage_boundaries": {
                    "public_release": "authorized",
                    "internal_use": "authorized",
                    "third_party_redistribution": "authorized",
                    "sublicensing": "authorized",
                },
            },
        }, {})

        self.assertEqual(result[1], "passed")

    def test_declared_internal_use_must_be_authorized(self):
        result = checks.license_permits_delivery({"task_id": "task-a"}, {
            "defaults": {
                "license": "Public source",
                "usage_scope": "No project use granted",
                "usage_boundaries": {
                    "public_release": "authorized",
                    "internal_use": "not_authorized",
                    "third_party_redistribution": "authorized",
                    "sublicensing": "authorized",
                },
            },
        }, {})

        self.assertEqual(result[1], "failed")
        self.assertIn("internal_use", result[2])

    def test_provisional_marking_does_not_establish_gold_shortfall(self):
        result = checks.gold_not_full_marks({
            "marked_by": None,
            "marked_on": None,
            "counts_toward_acceptance": False,
            "items": [{"code": "R01", "shortfall": "missing evidence"}],
        }, POLICY)

        self.assertEqual(result[1], "not_run")

    def test_full_mark_task_exception_is_evidence_bound_and_scoped(self):
        task_id = "d6a10b76-e511-518c-88cb-6cef2e718fbe"
        marking = {
            "marked_by": "Occupational Reviewer",
            "marked_on": "2026-08-24T01:16:42+08:00",
            "returned_form_total": 100,
            "items": [{"code": "R01", "shortfall": None}],
        }
        with tempfile.TemporaryDirectory() as task_root:
            evidence = os.path.join(task_root, "policy_exception.md")
            with open(evidence, "wb") as fh:
                fh.write(b"signed task exception")
            digest = hashlib.sha256(b"signed task exception").hexdigest()
            exception = {
                "status": "approved_task_exception",
                "check": "gold_not_full_marks",
                "task_id": task_id,
                "approved_by": "Client Owner",
                "approved_role": "Client project owner",
                "approved_at": "2020-08-24T11:00+08:00",
                "accepted_score": 100,
                "global_policy_unchanged": True,
                "scope": "single_task_only",
                "reason": "Retain the signed score without inventing a shortfall.",
                "evidence_file": "policy_exception.md",
                "evidence_sha256": digest,
            }
            result = checks.gold_not_full_marks(
                marking, POLICY, {"task_id": task_id},
                {"gold_not_full_marks": exception}, task_root)
            self.assertEqual(result[1], "passed")
            self.assertIn("single-task exception", result[2])

            other = checks.gold_not_full_marks(
                marking, POLICY, {"task_id": "other-task"},
                {"gold_not_full_marks": exception}, task_root)
            self.assertEqual(other[1], "failed")

            exception["evidence_sha256"] = "0" * 64
            tampered = checks.gold_not_full_marks(
                marking, POLICY, {"task_id": task_id},
                {"gold_not_full_marks": exception}, task_root)
            self.assertEqual(tampered[1], "failed")


class RubricContractTest(unittest.TestCase):
    def _record(self, count=25, score=4, required=True):
        items = [{"rubric_item_id": str(i), "score": score,
                  "required": required} for i in range(count)]
        return {"rubric_json": json.dumps(items)}

    def test_required_allows_explicit_false(self):
        result = checks.rubric_required_field(self._record(required=False), POLICY)
        self.assertEqual(result[1], "passed")

    def test_item_count_uses_lower_truncated_floor(self):
        self.assertEqual(checks.rubric_item_count(self._record(25), POLICY)[1], "passed")
        self.assertEqual(checks.rubric_item_count(self._record(24), POLICY)[1], "failed")

    def test_scores_are_not_limited_to_three_or_low_share(self):
        # A 10-point criterion and a rubric with no 1-2 point items are valid
        # under the revised client contract.
        result = checks.rubric_score_granularity(self._record(25, score=4), POLICY)
        self.assertEqual(result[1], "passed")
        self.assertEqual(checks.rubric_score_granularity(
            self._record(25, score=10), POLICY)[1], "passed")


if __name__ == "__main__":
    unittest.main()

"""Contract tests for the fixed validator check registry."""
import os
import unittest
from unittest import mock

import validate
from validation_registry import (
    BASE_VALIDATION_CHECKS, TEMPLATE_GUARD_CHECKS, TEMPLATE_GUARD_ROLES,
    expected_validation_checks,
)


class ValidationRegistryTest(unittest.TestCase):
    def test_non_template_task_requires_only_applicability_check(self):
        expected = expected_validation_checks({})
        self.assertIn("template_guards_applicability", expected)
        self.assertTrue(TEMPLATE_GUARD_CHECKS.isdisjoint(expected))

    def test_complete_template_task_requires_all_three_guards(self):
        meta = {"file_roles": {name: [name + ".txt"]
                               for name in TEMPLATE_GUARD_ROLES}}
        expected = expected_validation_checks(meta)
        self.assertTrue(TEMPLATE_GUARD_CHECKS.issubset(expected))

    def test_partial_template_roles_do_not_claim_guards_ran(self):
        expected = expected_validation_checks({"file_roles": {"policy": "p.md"}})
        self.assertTrue(TEMPLATE_GUARD_CHECKS.isdisjoint(expected))

    def test_render_contract_is_required_only_when_declared(self):
        self.assertNotIn("visual_output_contract", expected_validation_checks({}))
        expected = expected_validation_checks({
            "render_expectations": {"Report.xlsx": {"pages": 2}}})
        self.assertIn("visual_output_contract", expected)

    def test_validator_rejects_omitted_registered_check(self):
        original = validate.results
        try:
            validate.results = [
                {"check": name, "status": "passed"}
                for name in sorted(BASE_VALIDATION_CHECKS)
                if name != "tasks_jsonl_parses"]
            with self.assertRaises(RuntimeError) as caught:
                validate._assert_validation_registry({})
            self.assertIn("tasks_jsonl_parses", str(caught.exception))
        finally:
            validate.results = original

    def test_orchestrated_validator_requires_nonce(self):
        with mock.patch.dict(os.environ, {
                "GDPVAL_VALIDATOR_ORCHESTRATED": "1",
                "GDPVAL_VALIDATION_NONCE": ""}, clear=False):
            with self.assertRaises(SystemExit) as caught:
                validate.main()
        self.assertIn("fresh UUID nonce", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

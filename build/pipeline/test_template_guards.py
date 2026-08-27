import unittest

try:
    import validate as V
    MISSING = None
except Exception as exc:                                          # noqa: BLE001
    V, MISSING = None, str(exc)


@unittest.skipIf(MISSING, "validate.py needs its document dependencies: %s" % MISSING)
class TemplateGuardApplicabilityTest(unittest.TestCase):
    def test_unrelated_task_is_explicitly_not_applicable(self):
        status, missing = V._template_guard_applicability({
            "narrative_deliverable": "Final Decision.docx",
        })

        self.assertEqual(status, "not_applicable")
        self.assertEqual(missing, [])

    def test_partial_template_fails_closed(self):
        status, missing = V._template_guard_applicability({
            "policy": "Policy.md",
            "narrative_deliverable": "Recommendation.docx",
        })

        self.assertEqual(status, "invalid")
        self.assertEqual(missing, ["issue_log", "profile", "quotations"])

    def test_complete_template_runs_guards(self):
        status, missing = V._template_guard_applicability({
            "policy": "Policy.md",
            "issue_log": "Issues.xlsx",
            "profile": "Profile.xlsx",
            "quotations": ["Quote.md"],
            "narrative_deliverable": "Recommendation.docx",
        })

        self.assertEqual(status, "applicable")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

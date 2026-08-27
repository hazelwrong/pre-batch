import unittest

import validate


class HumanMarkingTotalTests(unittest.TestCase):
    def test_complete_human_marking_overrides_machine_score(self):
        marks = {
            "R01": {"awarded": 2},
            "R02": {"awarded": 1},
            "R03": {"awarded": 2},
        }
        result = validate._combined_gold_score(
            ["R01", "R02", "R03"], {"R03"}, marks, machine_earned=4)
        self.assertEqual(result["combined"], 5)
        self.assertEqual(result["basis"], "complete_human_marking")
        self.assertEqual(result["human_auto_awarded"], 3)
        self.assertEqual(result["manual_awarded"], 2)
        self.assertEqual(result["machine_cross_check_delta"], 1)

    def test_partial_marking_combines_only_judgement_items(self):
        marks = {"R03": {"awarded": 1}}
        result = validate._combined_gold_score(
            ["R01", "R02", "R03"], {"R03"}, marks, machine_earned=4)
        self.assertEqual(result["combined"], 5)
        self.assertEqual(result["basis"], "machine_plus_human_judgement_items")
        self.assertEqual(result["human_auto_awarded"], 0)
        self.assertIsNone(result["machine_cross_check_delta"])


if __name__ == "__main__":
    unittest.main()

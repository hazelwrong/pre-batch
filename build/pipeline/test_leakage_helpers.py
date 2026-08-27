"""The answer-leakage scan's two failure modes, both of which it has had.

1. Vacuity — the list of gold values was a literal, and went on naming the
   superseded package's totals. It reported "no leakage" while searching for
   numbers that were in no version of the gold.
2. A boundary that swallowed the sentence — a first attempt at matching whole
   numbers excluded any following period, so a leak written as
   "the total is 44,520." matched nothing.

Both are tested here, because both passed review by reading the code.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import validate as V
    MISSING = None
except Exception as exc:                                          # noqa: BLE001
    V, MISSING = None, str(exc)


@unittest.skipIf(MISSING, "validate.py needs python-docx and pypdf: %s" % MISSING)
class LeakageHelperTest(unittest.TestCase):
    def test_number_boundary_allows_a_sentence_to_end(self):
        self.assertTrue(V._number_boundary("44,520").search("the total is 44,520."))
        self.assertTrue(V._number_boundary("44,520").search("(44,520)"))
        self.assertTrue(V._number_boundary("2.80").search("scores 2.80 overall"))

    def test_number_boundary_rejects_fragments(self):
        self.assertIsNone(V._number_boundary("44,520").search("is 144,520 here"))
        self.assertIsNone(V._number_boundary("44,520").search("44,520.00 exact"))
        self.assertIsNone(V._number_boundary("2.80").search("12.80 weight"))

    def test_precision_threshold_applies_only_to_the_derived_set(self):
        # A weight of 0.10 or a headcount of 6 turning up in a policy is
        # coincidence, so they are excluded from the noisy derived set …
        self.assertEqual(V._formats(0.10, min_significant=3), set())
        self.assertEqual(V._formats(6, min_significant=3), set())
        # … but a figure the task asks the Agent to produce is a leak at any
        # precision, so declared results carry no threshold.
        self.assertIn("6.00", V._formats(6))
        self.assertIn("2.80", V._formats(2.8))
        self.assertIn("50,124.00", V._formats(50124.0, min_significant=3))

    def test_significant_digits_come_from_the_value_not_the_spelling(self):
        self.assertEqual(V._significant(6.00), 1)
        self.assertEqual(V._significant(0.10), 1)
        self.assertEqual(V._significant(2.80), 2)
        self.assertEqual(V._significant(50124), 5)


if __name__ == "__main__":
    unittest.main()

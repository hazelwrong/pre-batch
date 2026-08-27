import calendar
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import package as PKG


class PackageDateTest(unittest.TestCase):
    def test_default_date_is_utc_independent_of_local_timezone(self):
        # Make accidental use of local-time ``mktime`` observable even on
        # platforms (such as the test runner) without ``tzset``.
        original_mktime = PKG.time.mktime
        try:
            PKG.time.mktime = lambda _tuple: (_ for _ in ()).throw(
                AssertionError("source_date_epoch used local-time mktime"))
            actual = PKG.source_date_epoch("2026-08-14")
        finally:
            PKG.time.mktime = original_mktime
        expected = calendar.timegm((2026, 8, 14, 0, 0, 0))
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openpyxl

import security_scans as scans


class SecurityScansTest(unittest.TestCase):
    def workbook(self, directory, name, value):
        path = Path(directory) / name
        book = openpyxl.Workbook()
        book.active["A1"] = value
        book.save(path)
        return path

    def test_excel_external_call_formula_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.workbook(tmp, "Threat.xlsx",
                                 '=WEBSERVICE("https://example.invalid")')

            result = scans.scan_malicious([path])

        self.assertFalse(result["passed"])
        self.assertIn("external-call formula",
                      {hit["type"] for hit in result["hits"]})

    def test_extension_matching_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.workbook(tmp, "Threat.XLSX", '=DDE("cmd", "payload")')

            result = scans.scan_malicious([path])

        self.assertFalse(result["passed"])
        self.assertIn("external-call formula",
                      {hit["type"] for hit in result["hits"]})

    def test_run_all_extracts_each_file_text_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.workbook(tmp, "Workbook.xlsx", "ordinary content")
            real_text_of = scans._text_of
            with mock.patch.object(scans, "_text_of", wraps=real_text_of) as extract:
                scans.run_all([path], [path.name])

        self.assertEqual(extract.call_count, 1)


if __name__ == "__main__":
    unittest.main()

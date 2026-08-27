"""S-REF contract tests: the standard's rules refuse a bad spec before any file
is written, and two builds of the same spec produce the same bytes."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_references as REF
import officestrip

POLICY_MD = {
    "filename": "PS-2026-04 Store Systems Procurement Policy.md",
    "format": "md",
    "title": "Store Systems Procurement Evaluation Policy PS-2026-04",
    "meta": [["Issued by", "Group Procurement"], ["Effective", "2026-04-01"]],
    "blocks": [
        {"heading": "1. Scope"},
        {"text": "This policy governs the selection of store systems."},
        {"list": ["Bids are scored on four dimensions.",
                  "An unquoted line is never recorded as zero."]},
        {"table": {"columns": ["Dimension", "Weight"],
                   "rows": [["Three-year cost", "40%"], ["Functional cover", "30%"]]}},
    ],
}
REGISTER_XLSX = {
    "filename": "Store Profile - Chaoyang Stores.xlsx",
    "format": "xlsx",
    "sheets": [{"name": "Stores",
                "columns": ["Store Code", "Store", "Floor Area (sqm)"],
                "rows": [["CY-01", "Chaoyangmen", 210.5],
                         ["CY-02", "Wangjing", 178.0]],
                "widths": {"A": 12, "B": 22},
                "number_formats": {"C": "0.00"},
                "freeze": "A2"}],
}


class BuildReferencesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "reference_files"

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_both_formats_and_strips_the_workbook(self):
        written = REF.build([POLICY_MD, REGISTER_XLSX], self.out)
        self.assertEqual(len(written), 2)
        text = Path(written[0]).read_text(encoding="utf-8")
        self.assertIn("# Store Systems Procurement Evaluation Policy PS-2026-04", text)
        self.assertIn("- Issued by: Group Procurement", text)
        self.assertIn("| Dimension | Weight |", text)
        # The generator must never be what puts a toolchain name into a file.
        self.assertEqual(officestrip.residue(written[1]), [])

    def test_two_builds_are_byte_identical(self):
        first = REF.build([REGISTER_XLSX], self.out / "a")[0]
        second = REF.build([REGISTER_XLSX], self.out / "b")[0]
        self.assertEqual(Path(first).read_bytes(), Path(second).read_bytes())

    def test_the_other_allowed_formats_round_trip(self):
        specs = [
            {"filename": "Intake Extract - Q1 2026.csv", "format": "csv",
             "columns": ["Log ID", "Received"], "rows": [["A-1", "2026-01-02"]]},
            {"filename": "Intake Extract - Q1 2026.tsv", "format": "tsv",
             "columns": ["Log ID", "Received"], "rows": [["A-1", "2026-01-02"]]},
            {"filename": "Docket Export - Q1 2026.json", "format": "json",
             "columns": ["Log ID", "Received"], "rows": [["A-1", "2026-01-02"]]},
            {"filename": "Docket Export - Q1 2026.xml", "format": "xml",
             "root": "entries", "item_tag": "entry",
             "columns": ["Log ID", "Received"], "rows": [["A-1", "2026-01-02"]]},
        ]
        written = REF.build(specs, self.out)
        self.assertEqual(len(written), 4)
        import csv as _csv, json as _json
        import xml.etree.ElementTree as ET
        rows = list(_csv.reader(open(written[0], encoding="utf-8")))
        self.assertEqual(rows, [["Log ID", "Received"], ["A-1", "2026-01-02"]])
        rows = list(_csv.reader(open(written[1], encoding="utf-8"), delimiter="\t"))
        self.assertEqual(rows[1], ["A-1", "2026-01-02"])
        self.assertEqual(_json.load(open(written[2], encoding="utf-8")),
                         [{"Log ID": "A-1", "Received": "2026-01-02"}])
        root = ET.parse(written[3]).getroot()
        self.assertEqual(root.tag, "entries")
        # A heading with a space is not a legal element name; it must be made one.
        self.assertEqual(root[0].find("Log_ID").text, "A-1")

    def test_a_structured_file_with_no_content_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            REF.validate([{"filename": "Empty Export.json", "format": "json",
                           "columns": ["a"]}])
        self.assertIn("rows or data", str(caught.exception))

    def test_pdf_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            REF.validate([dict(POLICY_MD, filename="Policy.pdf", format="pdf")])
        self.assertIn("only", str(caught.exception))

    def test_forbidden_format_wins_if_it_is_also_allowed(self):
        policy = {"reference_files": {
            "allowed_formats": ["md", "pdf"],
            "forbidden_formats": ["pdf"]}}
        with self.assertRaises(ValueError):
            REF.validate([dict(POLICY_MD, filename="Policy.pdf", format="pdf")],
                         policy)

    def test_engineered_name_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            REF.validate([dict(POLICY_MD, filename="store_profile.md")])
        self.assertIn("engineered", str(caught.exception))

    def test_duplicate_basename_is_refused(self):
        with self.assertRaises(ValueError):
            REF.validate([POLICY_MD, dict(POLICY_MD)])

    def test_empty_spec_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            REF.validate([])
        self.assertIn("at least one", str(caught.exception))

    def test_filename_must_be_a_single_relative_name(self):
        for name in ("/tmp/escape.md", "../escape.md", "nested/file.md",
                     r"nested\escape.md", ".", ".."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                REF.validate([dict(POLICY_MD, filename=name)])

    def test_existing_symlink_cannot_redirect_reference_write(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("keep me", encoding="utf-8")
        self.out.mkdir(parents=True)
        link = self.out / POLICY_MD["filename"]
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are unavailable on this filesystem")
        with self.assertRaises(ValueError) as caught:
            REF.build([POLICY_MD], self.out)
        self.assertIn("outside outdir", str(caught.exception))
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me")

    def test_meta_accepts_the_shapes_people_write_and_loses_nothing(self):
        shapes = [
            [["Issuing Office", "EPA Region III"], ["Docket", "BTR-5200"]],
            [{"key": "Issuing Office", "value": "EPA Region III"},
             {"key": "Docket", "value": "BTR-5200"}],
            [{"label": "Issuing Office", "value": "EPA Region III"},
             {"label": "Docket", "value": "BTR-5200"}],
            {"Issuing Office": "EPA Region III", "Docket": "BTR-5200"},
        ]
        for n, meta in enumerate(shapes):
            spec = dict(POLICY_MD, meta=meta)
            rendered = REF.render_markdown(spec)
            self.assertIn("- Issuing Office: EPA Region III", rendered, str(meta))
            self.assertIn("- Docket: BTR-5200", rendered, str(meta))
            # The bug this replaces produced the literal word "key" as a label.
            self.assertNotIn("- key: value", rendered)

    def test_an_unreadable_meta_entry_is_refused_not_mangled(self):
        spec = dict(POLICY_MD, meta=[{"name": "x", "detail": "y", "extra": "z"}])
        with self.assertRaises(ValueError) as caught:
            REF.validate([spec])
        self.assertIn("meta entry", str(caught.exception))

    def test_column_settings_accept_the_three_shapes_people_write(self):
        columns = ["Log ID", "Date Received", "Note"]
        base = {"filename": "Intake Log - Q1.xlsx", "format": "xlsx",
                "sheets": [{"name": "Intake Log", "columns": columns,
                            "rows": [["A-1", "2026-01-02", "note"]]}]}
        shapes = [[16, 14, 40],                                   # 按位置
                  {"Log ID": 16, "Date Received": 14},            # 按列名
                  {"A": 16, "B": 14}]                             # 按列字母
        for widths in shapes:
            spec = dict(base)
            spec["sheets"] = [dict(base["sheets"][0], widths=widths)]
            REF.validate([spec])
            written = REF.build([spec], self.out / str(shapes.index(widths)))
            self.assertTrue(Path(written[0]).is_file())

    def test_a_bad_column_setting_fails_before_anything_is_written(self):
        spec = {"filename": "Intake Log - Q1.xlsx", "format": "xlsx",
                "sheets": [{"name": "Intake Log", "columns": ["A col"],
                            "rows": [["x"]], "widths": {"No Such Column": 10}}]}
        with self.assertRaises(ValueError) as caught:
            REF.validate([spec])
        self.assertIn("neither a column heading", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_row_wider_than_its_columns_is_refused(self):
        spec = {"filename": "Intake Log - Q1.xlsx", "format": "xlsx",
                "sheets": [{"name": "Intake Log", "columns": ["one"],
                            "rows": [["x", "y"]]}]}
        with self.assertRaises(ValueError) as caught:
            REF.validate([spec])
        self.assertIn("2 cells for 1 column", str(caught.exception))

    def test_pipe_in_a_cell_cannot_break_the_table(self):
        spec = dict(POLICY_MD, blocks=[{"table": {
            "columns": ["Line", "Note"],
            "rows": [["Hardware | rental", "two\nlines"]]}}])
        rendered = REF.render_markdown(spec)
        body = [ln for ln in rendered.splitlines() if ln.startswith("| Hardware")][0]
        # Escaped pipes stay in the text but no longer act as cell separators,
        # so the row still has exactly two cells.
        unescaped = len(re.findall(r"(?<!\\)\|", body))
        self.assertEqual(unescaped, 3)
        self.assertIn(r"Hardware \| rental", body)
        self.assertNotIn("\n", body.replace("\\n", ""))


if __name__ == "__main__":
    unittest.main()

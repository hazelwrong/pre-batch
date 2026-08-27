"""Two tasks, one delivery root.

The builder held its task in module globals, which is another way of saying it
could build exactly one. The client's format is a single delivery root whose
tasks.jsonl carries every task, so this is the shape the pipeline has to be
right about before a second task's data arrives — not after.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4, uuid5

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_references as REF
import build_delivery as BD


def reference_spec(tag):
    return [
        {"filename": "%s Procurement Policy.md" % tag, "format": "md",
         "title": "%s Procurement Policy" % tag,
         "blocks": [{"heading": "1. Scope"}, {"text": "Governs %s." % tag}]},
        {"filename": "%s Site Register.xlsx" % tag, "format": "xlsx",
         "sheets": [{"name": "Sites", "columns": ["Code", "Site"],
                     "rows": [["S-01", "%s site" % tag]]}]},
    ]


def rubric(tag):
    return [{"score": 2, "criterion": "%s item %d says one thing" % (tag, i),
             "rubric_item_id": str(uuid4()), "required": True,
             "verification": "Read it."} for i in range(50)]


class MultiTaskBuildTest(unittest.TestCase):
    def test_late_written_manifests_do_not_claim_stale_hashes(self):
        self.assertIn("manifests/validation_status.jsonl", BD.SELF_REFERENTIAL)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.tasks = self.base / "tasks"
        self.staging = self.base / "staging"
        self.delivery = self.base / "delivery"
        self.ids = [str(uuid4()), str(uuid4())]
        for task_id, tag in zip(self.ids, ("Alpha", "Beta")):
            self.make_task(task_id, tag)

    def tearDown(self):
        self.tmp.cleanup()

    def make_task(self, task_id, tag):
        out = self.tasks / task_id
        out.mkdir(parents=True)
        spec = reference_spec(tag)
        REF.build(spec, self.staging / task_id / "reference_files")
        gold_dir = self.staging / task_id / "deliverable_files"
        REF.build([{"filename": "%s Comparison.xlsx" % tag, "format": "xlsx",
                    "sheets": [{"name": "Summary", "columns": ["Metric", "Value"],
                    "rows": [["Total", 1]]}]}], gold_dir)
        gold_name = "%s Comparison.xlsx" % tag
        items = rubric(tag)

        def write(name, value):
            (out / name).write_text(
                value if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

        write("prompt.md", "You are a %s manager. Produce the workbook.\n" % tag)
        write("rubric.json", items)
        write("rubric_pretty.txt",
              "\n\n".join("[+%d] %s" % (i["score"], i["criterion"]) for i in items) + "\n")
        write("task_meta.json", {
            "task_id": task_id, "sector": "Retail Trade",
            "occupation": "General and Operations Managers",
            "rubric_version": "v1", "language": "en",
            "file_order": {
                "reference_files": [e["filename"] for e in spec],
                "deliverable_files": ["%s Comparison.xlsx" % tag]}})
        write("coverage.json", {"task_family": "%s-family" % tag.lower(),
                                "duplicate_group_id": "dg-%s" % tag.lower(),
                                "workflow": "multi-step analysis"})
        write("provenance.json", {
            "source_record_prefix": "supplier-work-records/%s#" % tag.lower(),
            "defaults": {"rights_holder": "Supplier", "version": "v1",
                         "license": "Provided by the supplier",
                         "usage_scope": "GDPval", "contains_pii": False,
                         "deidentification_note": "De-identified.",
                         "acquisition_date": "2026-08-22"},
            "roles": {
                "reference": {"source_type": "supplier_work_record",
                              "production_method": "supplier work record; reconstructed",
                              "drafted_by": "Supplier"},
                "deliverable": {"source_type": "supplier_deliverable",
                                "production_method": "supplier deliverable",
                                "drafted_by": "Supplier project team"},
                "index": {"source_type": "supplier_delivery_record",
                          "production_method": "supplier-assembled delivery record",
                          "drafted_by": "Supplier delivery team"},
                "validation_evidence": {"source_type": "supplier_validation_record",
                                        "production_method": "supplier QA record",
                                        "drafted_by": "Supplier QA team"}}})
        write("gold_provenance.json", {
            "real_deliverable_files": [{
                "filename": gold_name,
                "source_url": "https://example.test/%s" % gold_name.replace(" ", "-"),
                "source_sha256": BD.sha256(gold_dir / gold_name),
            }]})
        write("source_inventory.json", [{"source_id": "SRC-%s" % tag,
                                         "source_type": "supplier_work_record",
                                         "description": "%s records" % tag,
                                         "adopted": True, "rejection_reason": None,
                                         "license": "Supplier-owned"}])

    def build(self):
        env = dict(os.environ,
                   GDPVAL_TASKS=str(self.tasks),
                   GDPVAL_STAGING=str(self.staging),
                   GDPVAL_BUILD_ROOT=str(self.base / "scratch"),
                   GDPVAL_DELIVERY=str(self.delivery))
        env.pop("GDPVAL_TASK_ID", None)
        proc = subprocess.run([sys.executable, str(HERE / "build_delivery.py")],
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_both_tasks_land_in_one_delivery_root(self):
        self.build()
        records = [json.loads(line) for line in
                   (self.delivery / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(sorted(r["task_id"] for r in records), sorted(self.ids))

        bundles = set()
        for record in records:
            for path in record["reference_files"] + record["deliverable_files"]:
                bundles.add(path.split("/")[1])
                self.assertTrue((self.delivery / path).is_file(), path)
        # Four distinct bundle ids: two tasks, reference and deliverable each.
        self.assertEqual(len(bundles), 4)

        # Bundle ids are the UUID5 derivation the specification names.
        for record in records:
            expected = uuid5(UUID(record["task_id"]), "reference_files").hex
            self.assertEqual(record["reference_files"][0].split("/")[1], expected)
            self.assertEqual(len(record["deliverable_file_urls"]),
                             len(record["deliverable_files"]))

    def test_no_task_sees_another_task_files(self):
        self.build()
        records = [json.loads(line) for line in
                   (self.delivery / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
        for record in records:
            tag = "Alpha" if "Alpha" in record["prompt"] else "Beta"
            other = "Beta" if tag == "Alpha" else "Alpha"
            names = [os.path.basename(p) for p in
                     record["reference_files"] + record["deliverable_files"]]
            self.assertTrue(all(n.startswith(tag) for n in names), names)
            self.assertFalse(any(other in n for n in names))

    def test_manifests_cover_every_task(self):
        self.build()
        coverage = json.loads((self.delivery / "manifests" /
                               "coverage_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(c["task_id"] for c in coverage), sorted(self.ids))
        # Modalities are read off the files, not declared beside them.
        for entry in coverage:
            self.assertEqual(entry["input_modalities"], ["md", "xlsx"])
            self.assertEqual(entry["output_types"], ["xlsx"])

        rows = [json.loads(line) for line in
                (self.delivery / "manifests" / "provenance_manifest.jsonl")
                .read_text(encoding="utf-8").splitlines() if line.strip()]
        payload = [r for r in rows if r["role"] in ("reference", "deliverable")]
        self.assertEqual(len(payload), 6)          # (2 refs + 1 gold) x 2 tasks
        self.assertEqual({r["source_type"] for r in payload},
                         {"supplier_work_record", "supplier_deliverable"})
        for row in payload:
            self.assertIn(row["task_id"], self.ids)

        inventory = [json.loads(line) for line in
                     (self.delivery / "manifests" / "source_inventory.jsonl")
                     .read_text(encoding="utf-8").splitlines() if line.strip()]
        adopted = [r for r in inventory if r.get("adopted")]
        self.assertEqual(sorted(r["task_id"] for r in adopted), sorted(self.ids))


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from uuid import uuid4
from xml.sax.saxutils import escape

from orchestrator import (Pipeline, PipelineError, PRODUCTION_ROLES,
                          REQUIRED_VALIDATION_CHECKS,
                          _bundle_manifest, _review_payload_digest, _sha256,
                          validation_registry_digest)
from review_kits import (_display_literal, _final_config, _phase1_configs,
                         _candidate_snapshot, _supplemental_config,
                         _review_change_impacts,
                         _xlsx_cells,
                         create_final, create_supplemental, ingest_receipt,
                         ingest_supplemental, production_basis, record_remediation)


class StagedReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.pipeline = Pipeline.initialise(self.workspace, str(uuid4()))
        self.validator_run = mock.patch.object(
            Pipeline, "_run_fixed_validator", return_value={
                "nonce": "test-validation-nonce", "returncode": 0,
                "stdout": "", "stderr": "",
            })
        self.validator_run.start()
        state = self.pipeline._load()
        state["runs"] = [{
            "run_id": str(uuid4()), "role": role, "decision": "passed",
            "status": "completed", "input_artifacts": {},
            "output_artifacts": {}, "agent_id": "a-" + role,
            "context_id": "ctx-" + role,
        } for role in PRODUCTION_ROLES]
        self.pipeline._save(state)

        self.tasks = self.root / "tasks"
        self.task = self.tasks / state["task_id"]
        self.task.mkdir(parents=True)
        self.items = [{
            "score": 4, "criterion": "Criterion %d" % (index + 1),
            "required": True, "rubric_item_id": str(uuid4()),
            "verification": "Review the Gold and cite evidence.",
        } for index in range(25)]
        self.codes = ["R%02d" % (index + 1) for index in range(25)]
        self.write_json(self.task / "task_meta.json", {
            "task_id": state["task_id"], "sector": "Manufacturing",
            "occupation": "Buyers and Purchasing Agents", "language": "English",
            "rubric_version": "v1", "item_codes": self.codes,
        })
        self.write_json(self.task / "rubric.json", self.items)
        (self.task / "prompt.md").write_text("Complete the task.\n", encoding="utf-8")
        (self.task / "rubric_pretty.txt").write_text(
            "Reviewer-facing rubric.\n", encoding="utf-8")
        basis = production_basis(self.pipeline, self.tasks)
        state = self.pipeline._load()
        state["review_cycle"] = {
            "cycle_id": str(uuid4()), "status": "awaiting_phase1_reviews",
            "created_at": "2026-08-27T09:00:00+08:00",
            "initial_basis": basis,
            "candidate_delivery": {"sha256": "a" * 64},
            "phase1": {"receipts": {}}, "remediation": None,
            "pre_final_validation": None,
            "final": {"package": None, "receipt": None},
        }
        self.pipeline._save(state)

    def tearDown(self):
        self.validator_run.stop()
        self.tmp.cleanup()

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def xlsx(path, sheets, absolute_targets=False):
        def sheet_xml(cells):
            rows = {}
            for address, value in cells.items():
                row = int("".join(char for char in address if char.isdigit()))
                if isinstance(value, bool):
                    cell = '<c r="%s" t="b"><v>%s</v></c>' % (
                        address, "1" if value else "0")
                elif isinstance(value, (int, float)):
                    cell = '<c r="%s"><v>%s</v></c>' % (address, value)
                else:
                    cell = '<c r="%s" t="str"><v>%s</v></c>' % (
                        address, escape(str(value)))
                rows.setdefault(row, []).append(cell)
            body = "".join('<row r="%d">%s</row>' %
                           (row, "".join(values))
                           for row, values in sorted(rows.items()))
            return ('<?xml version="1.0" encoding="utf-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main"><sheetData>%s</sheetData>'
                    '</worksheet>') % body

        names = list(sheets)
        workbook = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships"><sheets>%s</sheets></workbook>') % \
            "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' %
                    (escape(name), index, index)
                    for index, name in enumerate(names, start=1))
        relationships = ('<?xml version="1.0" encoding="utf-8"?>'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/'
                         'package/2006/relationships">%s</Relationships>') % \
            "".join('<Relationship Id="rId%d" Target="%sworksheets/sheet%d.xml" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/worksheet"/>' % (
                        index, "/xl/" if absolute_targets else "", index)
                    for index in range(1, len(names) + 1))
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", relationships)
            for index, cells in enumerate(sheets.values(), start=1):
                archive.writestr("xl/worksheets/sheet%d.xml" % index,
                                 sheet_xml(cells))

    def metadata(self, candidate_sha):
        return {
            "A3": "Task ID", "B3": self.pipeline._load()["task_id"],
            "A7": "Rubric version", "B7": "v1",
            "A8": "Candidate SHA-256", "B8": candidate_sha,
        }

    def workbook(self, layer, path, verdict="Pass", unexplained_na=False,
                 finding_confirmation=None):
        state = self.pipeline._load()
        meta = json.loads((self.task / "task_meta.json").read_text(encoding="utf-8"))
        candidate_sha = (state["review_cycle"]["candidate_delivery"]["sha256"]
                         if layer != "final_review" else
                         state["review_cycle"]["final"]["package"]["candidate_sha256"])
        if layer == "general_review":
            general_config, _expert_config = _phase1_configs(
                meta, self.items, self.codes,
                state["review_cycle"]["initial_basis"], candidate_sha)
            main = self.metadata(candidate_sha)
            for index in range(1, 9):
                row = 11 + index
                main["A%d" % row] = "G%02d" % index
                main["B%d" % row] = general_config["checklist"][index - 1]["text"]
                main["C%d" % row] = (
                    "N/A" if unexplained_na and index == 1 else
                    "Issue" if finding_confirmation and index == 1 else "Pass")
            main.update({"A22": "Conclusion", "B22": verdict,
                         "A23": "Substantive opinion",
                         "B23": "Completed a substantive review."})
            findings = {}
            if finding_confirmation:
                findings = {
                    "A4": "G-F01", "B4": "Major", "C4": "tasks.jsonl",
                    "D4": "Mismatch", "E4": "Correct it",
                    "F4": finding_confirmation,
                }
            sheets = {"General Review": main, "Findings": findings}
        elif layer == "occupational_expert_review":
            _general_config, expert_config = _phase1_configs(
                meta, self.items, self.codes,
                state["review_cycle"]["initial_basis"], candidate_sha)
            main = self.metadata(candidate_sha)
            main.update({
                "A11": "Proposed mapping", "B11": expert_config["mapping"]["proposed"],
                "A12": "Boundary", "B12": expert_config["mapping"]["boundary"],
                "A13": "Decision", "B13": "Accept",
                "A14": "Substantive reason", "B14": "The mapping fits the role boundary.",
                "A25": "Conclusion", "B25": verdict,
                "A26": "Substantive opinion", "B26": "Completed a substantive review.",
            })
            for index in range(1, 6):
                row = 17 + index
                main["A%d" % row] = "E%02d" % index
                main["B%d" % row] = expert_config["checklist"][index - 1]["text"]
                main["C%d" % row] = "Pass"
            rubric = {}
            for index, (code, item) in enumerate(zip(self.codes, self.items), start=4):
                rubric.update({
                    "A%d" % index: code,
                    "B%d" % index: item["rubric_item_id"],
                    "C%d" % index: True,
                    "D%d" % index: 4,
                    "E%d" % index: item["criterion"],
                    "F%d" % index: (
                        item["verification"] + "\nMachine: " +
                        expert_config["rubrics"][index - 4]["machine_result"]),
                    "G%d" % index: "Adopt",
                    "I%d" % index: 4,
                    "J%d" % index: "Located in the returned Gold.",
                })
            sheets = {"Occupation Review": main, "Rubric and Gold": rubric,
                      "Findings": {}}
        else:
            final_config = _final_config(
                meta, production_basis(self.pipeline, self.tasks),
                candidate_sha, state["review_cycle"])
            main = self.metadata(candidate_sha)
            for index, evidence in enumerate(final_config["final_evidence"]):
                row = 12 + index
                main["A%d" % row] = evidence["label"]
                main["B%d" % row] = _display_literal(evidence["value"])
                main["C%d" % row] = "Confirmed"
            main["A21"] = "ID"
            for index in range(1, 6):
                row = 21 + index
                main["A%d" % row] = "F%02d" % index
                main["B%d" % row] = final_config["checklist"][index - 1]["text"]
                main["C%d" % row] = "Confirmed"
            main.update({"A29": "Conclusion", "B29": verdict,
                         "A30": "Substantive opinion",
                         "B30": "Completed a substantive review."})
            sheets = {"Final Review": main,
                      "Finding Closure": {"A4": "None", "G4": "Confirmed"}}
        self.xlsx(path, sheets)

    def supplemental_workbook(self, path, decision, verdict, opinion):
        state = self.pipeline._load()
        package = state["review_cycle"]["remediation"]["supplemental_package"]
        meta = json.loads((self.task / "task_meta.json").read_text(encoding="utf-8"))
        config = _supplemental_config(
            meta, self.items, self.codes,
            production_basis(self.pipeline, self.tasks),
            package["candidate_sha256"], state["review_cycle"],
            "general_review")
        main = self.metadata(package["candidate_sha256"])
        for index, item in enumerate(config["requirements"], start=12):
            main.update({
                "A%d" % index: item["requirement_id"],
                "B%d" % index: item["kind"],
                "C%d" % index: item["summary"],
                "D%d" % index: item["remediation_display"],
                "E%d" % index: decision,
                "F%d" % index: ("The correction remains incomplete."
                                 if decision == "Issue" else ""),
                "G%d" % index: "N/A", "H%d" % index: "N/A",
                "I%d" % index: "N/A",
            })
        last = 11 + len(config["requirements"])
        main.update({
            "A%d" % (last + 3): "Conclusion",
            "B%d" % (last + 3): verdict,
            "A%d" % (last + 4): "Substantive opinion",
            "B%d" % (last + 4): opinion,
        })
        self.xlsx(path, {"Supplemental Review": main})

    def transcription(self, layer, reviewer, reviewed_at):
        return {
            "task_id": self.pipeline._load()["task_id"], "layer": layer,
            "reviewer_id": reviewer, "reviewer_title": "Reviewer",
            "reviewed_at": reviewed_at, "credential_status": "not_supplied",
        }

    def ingest(self, layer, reviewer, reviewed_at):
        receipt = self.root / (layer + ".xlsx")
        self.workbook(layer, receipt)
        transcription = self.root / (layer + ".json")
        self.write_json(transcription,
                        self.transcription(layer, reviewer, reviewed_at))
        return ingest_receipt(self.pipeline, layer, receipt, transcription,
                              self.tasks)

    def validation_tree(self, final=False):
        state = self.pipeline._load()
        delivery = self.root / ("final-delivery" if final else "pre-delivery")
        (delivery / "manifests").mkdir(parents=True)
        (delivery / "tasks.jsonl").write_text(json.dumps({
            "task_id": state["task_id"], "sector": "Manufacturing",
            "occupation": "Buyers and Purchasing Agents",
            "prompt": "Complete the task.",
            "rubric_pretty": "Reviewer-facing rubric.",
            "rubric_json": json.dumps(self.items),
            "reference_files": [], "deliverable_files": [],
        }) + "\n", encoding="utf-8")
        evidence = delivery / "validation_evidence" / state["task_id"]
        evidence.mkdir(parents=True)
        (evidence / "report.json").write_text("{}", encoding="utf-8")
        checks = [{"check": name, "status": "passed"}
                  for name in sorted(REQUIRED_VALIDATION_CHECKS)]
        if not final:
            next(item for item in checks
                 if item["check"] == "human_review_final_review")["status"] = "not_run"
        row = {
            "task_id": state["task_id"],
            "validator": "pipeline/validate.py (programmatic self-check)",
            "validator_sha256": _sha256(Path(__file__).with_name("validate.py")),
            "validation_run_nonce": "test-validation-nonce",
            "registry_sha256": validation_registry_digest(
                item["check"] for item in checks),
            "checks": checks,
        }
        (delivery / "manifests" / "validation_status.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        return delivery

    def prepare_final_package(self, frozen_at="2026-08-27T11:00:00+08:00"):
        final_delivery = self.validation_tree(final=True)
        delivery_digest, _files = _bundle_manifest(final_delivery)
        review_payload_digest, _payload = _review_payload_digest(
            final_delivery, self.pipeline._load()["task_id"])
        package = self.root / "Final-Review-Package.zip"
        package.write_bytes(b"package")
        artifact = self.pipeline.add_artifact(
            "final_review_package", [package], "review-kit")
        state = self.pipeline._load()
        state["review_cycle"]["pre_final_validation"]["run_at"] = \
            "2026-08-27T10:30:00+08:00"
        state["review_cycle"]["final"]["package"] = {
            "artifact_digest": artifact["digest"],
            "basis_digest": state["review_cycle"]["remediation"]["to_basis_digest"],
            "candidate_sha256": "b" * 64,
            "delivery_digest": delivery_digest,
            "review_payload_digest": review_payload_digest,
            "frozen_at": frozen_at,
        }
        state["review_cycle"]["status"] = "awaiting_final_review"
        self.pipeline._save(state)
        return final_delivery

    def test_staged_receipts_validation_and_hreg(self):
        self.ingest("occupational_expert_review", "person-b",
                    "2026-08-27T10:05:00+08:00")
        self.ingest("general_review", "person-a",
                    "2026-08-27T10:00:00+08:00")
        self.assertEqual(self.pipeline.status()["workflow_stage"],
                         "remediation_required")

        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"], "findings": []})
        record_remediation(self.pipeline, closure, self.tasks)
        self.pipeline.record_validation(self.validation_tree(), "pre_final")

        final_delivery = self.prepare_final_package()
        self.ingest("final_review", "person-c",
                    "2026-08-27T11:05:00+08:00")
        self.pipeline.record_validation(final_delivery, "final")
        self.pipeline.record_human_review()
        self.assertTrue(self.pipeline.status()["release_ready"])
        state = self.pipeline._load()
        state["review_cycle"]["status"] = "hreg_required"
        self.pipeline._save(state)
        self.assertFalse(self.pipeline.status()["release_ready"])

    def test_final_equal_to_freeze_is_rejected(self):
        self.ingest("general_review", "person-a", "2026-08-27T10:00:00+08:00")
        self.ingest("occupational_expert_review", "person-b",
                    "2026-08-27T10:05:00+08:00")
        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"], "findings": []})
        record_remediation(self.pipeline, closure, self.tasks)
        self.pipeline.record_validation(self.validation_tree(), "pre_final")
        self.prepare_final_package()
        with self.assertRaises(PipelineError) as caught:
            self.ingest("final_review", "person-c",
                        "2026-08-27T11:00:00+08:00")
        self.assertIn("strictly later", str(caught.exception))

    def test_non_xlsx_receipt_is_rejected(self):
        receipt = self.root / "general_review.xlsx"
        receipt.write_text("not a workbook", encoding="utf-8")
        transcription = self.root / "general.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-27T10:00:00+08:00"))
        with self.assertRaises(PipelineError) as caught:
            ingest_receipt(self.pipeline, "general_review", receipt,
                           transcription, self.tasks)
        self.assertIn("valid XLSX", str(caught.exception))

    def test_na_decision_requires_a_reason(self):
        receipt = self.root / "general_review.xlsx"
        self.workbook("general_review", receipt, unexplained_na=True)
        transcription = self.root / "general.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-27T10:00:00+08:00"))
        with self.assertRaises(PipelineError) as caught:
            ingest_receipt(self.pipeline, "general_review", receipt,
                           transcription, self.tasks)
        self.assertIn("N/A needs", str(caught.exception))

    def test_chinese_review_config_and_receipt_stay_in_task_language(self):
        meta = json.loads((self.task / "task_meta.json").read_text(encoding="utf-8"))
        meta["language"] = "Chinese"
        self.write_json(self.task / "task_meta.json", meta)
        state = self.pipeline._load()
        state["review_cycle"]["initial_basis"] = production_basis(
            self.pipeline, self.tasks)
        self.pipeline._save(state)
        general, expert = _phase1_configs(
            meta, self.items, self.codes, state["review_cycle"]["initial_basis"],
            state["review_cycle"]["candidate_delivery"]["sha256"])
        self.assertEqual(general["locale"], "zh")
        self.assertEqual(general["ui"]["sheet_names"]["general_review"], "通用审查")
        self.assertEqual(general["ui"]["choices"]["pass"], "通过")
        self.assertTrue(all(any("\u4e00" <= char <= "\u9fff"
                                for char in item["text"])
                            for item in general["checklist"]))
        self.assertEqual(expert["title"], "GDPval 职业专家审查")

        candidate_sha = state["review_cycle"]["candidate_delivery"]["sha256"]
        cells = {
            "A3": "任务 ID", "B3": state["task_id"],
            "A7": "评分标准版本", "B7": "v1",
            "A8": "候选包 SHA-256", "B8": candidate_sha,
            "A22": "结论", "B22": "通过",
            "A23": "实质意见", "B23": "已完成实质审查，未发现需要整改的问题。",
        }
        for index, item in enumerate(general["checklist"], start=12):
            cells.update({"A%d" % index: item["id"],
                          "B%d" % index: item["text"],
                          "C%d" % index: "通过"})
        receipt = self.root / "chinese-general-review.xlsx"
        self.xlsx(receipt, {"通用审查": cells, "问题记录": {}})
        transcription = self.root / "chinese-general-review.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-27T10:00:00+08:00"))
        ingest_receipt(
            self.pipeline, "general_review", receipt, transcription, self.tasks)
        stored = self.pipeline._load()["review_cycle"]["phase1"]["receipts"]
        self.assertEqual(stored["general_review"]["record"]["verdict"], "Pass")

        expert_cells = {
            "A3": "任务 ID", "B3": state["task_id"],
            "A7": "评分标准版本", "B7": "v1",
            "A8": "候选包 SHA-256", "B8": candidate_sha,
            "A11": "建议职业映射", "B11": expert["mapping"]["proposed"],
            "A12": "角色边界", "B12": expert["mapping"]["boundary"],
            "A13": "判断", "B13": "接受",
            "A14": "判断理由", "B14": "该映射符合任务所述角色边界。",
            "A25": "结论", "B25": "通过",
            "A26": "实质意见", "B26": "已完成专业核对，未发现需要整改的问题。",
        }
        for index, item in enumerate(expert["checklist"], start=18):
            expert_cells.update({"A%d" % index: item["id"],
                                 "B%d" % index: item["text"],
                                 "C%d" % index: "通过"})
        rubric_cells = {}
        for index, (code, item) in enumerate(zip(self.codes, self.items), start=4):
            rubric_cells.update({
                "A%d" % index: code, "B%d" % index: item["rubric_item_id"],
                "C%d" % index: True, "D%d" % index: 4,
                "E%d" % index: item["criterion"],
                "F%d" % index: (item["verification"] + "\n机器结果: " +
                                 expert["rubrics"][index - 4]["machine_result"]),
                "G%d" % index: "采纳", "I%d" % index: 4,
                "J%d" % index: "已在 Gold 中定位对应证据。",
            })
        expert_receipt = self.root / "chinese-occupational-review.xlsx"
        self.xlsx(expert_receipt, {
            "职业审查": expert_cells, "评分标准与Gold": rubric_cells,
            "问题记录": {},
        })
        expert_transcription = self.root / "chinese-occupational-review.json"
        self.write_json(expert_transcription, self.transcription(
            "occupational_expert_review", "person-b",
            "2026-08-27T10:05:00+08:00"))
        ingest_receipt(self.pipeline, "occupational_expert_review",
                       expert_receipt, expert_transcription, self.tasks)
        stored = self.pipeline._load()["review_cycle"]["phase1"]["receipts"]
        self.assertEqual(
            stored["occupational_expert_review"]["record"]
            ["occupation_mapping_decision"], "Accept")
        self.assertTrue(all(item["adoption"] == "Adopt" for item in
                            stored["occupational_expert_review"]["record"]
                            ["rubric_items"]))

    def test_change_impact_matrix_routes_only_affected_review_layers(self):
        initial = production_basis(self.pipeline, self.tasks)
        (self.task / "prompt.md").write_text(
            "Complete the clarified task.\n", encoding="utf-8")
        prompt_impacts = _review_change_impacts(
            initial, production_basis(self.pipeline, self.tasks),
            self.pipeline.policy)
        self.assertEqual(set(prompt_impacts), {
            "general_review", "occupational_expert_review"})

        (self.task / "prompt.md").write_text("Complete the task.\n", encoding="utf-8")
        initial = production_basis(self.pipeline, self.tasks)
        self.write_json(self.task / "provenance.json", {"review_note": "updated"})
        provenance_impacts = _review_change_impacts(
            initial, production_basis(self.pipeline, self.tasks),
            self.pipeline.policy)
        self.assertEqual(set(provenance_impacts), {"general_review"})

    def test_parser_accepts_ooxml_root_relative_sheet_targets(self):
        workbook = self.root / "root-relative-target.xlsx"
        self.xlsx(workbook, {"通用审查": {"A1": "可打开"}},
                  absolute_targets=True)
        self.assertEqual(_xlsx_cells(workbook)["通用审查"]["A1"], "可打开")

    def test_transcription_cannot_repeat_workbook_conclusion(self):
        receipt = self.root / "general_review.xlsx"
        self.workbook("general_review", receipt)
        transcription = self.root / "general.json"
        value = self.transcription(
            "general_review", "person-a", "2026-08-27T10:00:00+08:00")
        value["verdict"] = "Fail"
        self.write_json(transcription, value)
        with self.assertRaises(PipelineError) as caught:
            ingest_receipt(self.pipeline, "general_review", receipt,
                           transcription, self.tasks)
        self.assertIn("unsupported fields", str(caught.exception))

    def test_transcription_rejects_fields_outside_identity_scope(self):
        receipt = self.root / "general_review.xlsx"
        self.workbook("general_review", receipt)
        transcription = self.root / "general.json"
        value = self.transcription(
            "general_review", "person-a", "2026-08-27T10:00:00+08:00")
        value["credential_evidence_files"] = ["invented.pdf"]
        self.write_json(transcription, value)
        with self.assertRaises(PipelineError) as caught:
            ingest_receipt(self.pipeline, "general_review", receipt,
                           transcription, self.tasks)
        self.assertIn("unsupported fields", str(caught.exception))

    def test_candidate_snapshot_rejects_delivery_task_spec_mismatch(self):
        delivery = self.validation_tree()
        record = json.loads((delivery / "tasks.jsonl").read_text(encoding="utf-8"))
        record["prompt"] = "A different task prompt."
        (delivery / "tasks.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8")
        meta = json.loads((self.task / "task_meta.json").read_text(encoding="utf-8"))
        with self.assertRaises(PipelineError) as caught:
            _candidate_snapshot(
                self.pipeline._load()["task_id"], self.task, meta, delivery,
                None, self.root / "candidate")
        self.assertIn("does not match task data", str(caught.exception))

    def test_incomplete_fixed_validation_registry_is_rejected(self):
        state = self.pipeline._load()
        state["review_cycle"] = None
        self.pipeline._save(state)
        delivery = self.root / "incomplete-delivery"
        evidence = delivery / "validation_evidence" / state["task_id"]
        (delivery / "manifests").mkdir(parents=True)
        evidence.mkdir(parents=True)
        (evidence / "report.json").write_text("{}", encoding="utf-8")
        checks = [{"check": "tasks_jsonl_parses", "status": "passed"}]
        row = {
            "task_id": state["task_id"],
            "validator": "pipeline/validate.py (programmatic self-check)",
            "validator_sha256": _sha256(Path(__file__).with_name("validate.py")),
            "validation_run_nonce": "test-validation-nonce",
            "registry_sha256": validation_registry_digest(
                item["check"] for item in checks),
            "checks": checks,
        }
        (delivery / "manifests" / "validation_status.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.record_validation(delivery, "pre_final")
        self.assertIn("incomplete", str(caught.exception))

    def test_non_passing_phase1_receipt_enters_remediation(self):
        self.ingest("general_review", "person-a",
                    "2026-08-27T10:00:00+08:00")
        receipt = self.root / "occupational_expert_review.xlsx"
        self.workbook("occupational_expert_review", receipt,
                      verdict="Conditional pass")
        transcription = self.root / "occupational.json"
        self.write_json(transcription, self.transcription(
            "occupational_expert_review", "person-b",
            "2026-08-27T10:05:00+08:00"))
        ingest_receipt(self.pipeline, "occupational_expert_review", receipt,
                       transcription, self.tasks)
        self.assertEqual(self.pipeline.status()["workflow_stage"],
                         "remediation_required")
        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"], "findings": []})
        with self.assertRaises(PipelineError):
            record_remediation(self.pipeline, closure, self.tasks)

    def test_conditional_pass_can_close_through_supplemental_route(self):
        self.ingest("general_review", "person-a",
                    "2026-08-27T10:00:00+08:00")
        receipt = self.root / "occupational_expert_review.xlsx"
        self.workbook("occupational_expert_review", receipt,
                      verdict="Conditional pass")
        transcription = self.root / "occupational.json"
        self.write_json(transcription, self.transcription(
            "occupational_expert_review", "person-b",
            "2026-08-27T10:05:00+08:00"))
        ingest_receipt(self.pipeline, "occupational_expert_review", receipt,
                       transcription, self.tasks)
        (self.task / "prompt.md").write_text(
            "Complete the clarified task.\n", encoding="utf-8")
        evidence = self.root / "conditional-closure.txt"
        evidence.write_text("clarified", encoding="utf-8")
        closure = self.root / "conditional-closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"],
            "findings": [{
                "finding_id": "E-VERDICT", "disposition": "closed",
                "rationale": "Clarified the condition in the prompt.",
                "closed_at": "2026-08-27T10:10:00+08:00",
                "evidence_files": ["conditional-closure.txt"],
            }],
        })
        record_remediation(self.pipeline, closure, self.tasks)
        self.assertEqual(self.pipeline.status()["workflow_stage"],
                         "supplemental_review_kit_required")

    def add_general_finding(self, requires_confirmation):
        receipt = self.root / "general_review.xlsx"
        self.workbook(
            "general_review", receipt,
            finding_confirmation="Yes" if requires_confirmation else "No")
        transcription = self.root / "general_review.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-27T10:00:00+08:00"))
        ingest_receipt(
            self.pipeline, "general_review", receipt, transcription, self.tasks)
        self.ingest("occupational_expert_review", "person-b",
                    "2026-08-27T10:05:00+08:00")

    def remediation_record(self, closed_at):
        evidence = self.root / "closure.txt"
        evidence.write_text("closed", encoding="utf-8")
        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"],
            "findings": [{
                "finding_id": "G-F01", "disposition": "closed",
                "rationale": "Corrected and checked.", "closed_at": closed_at,
                "evidence_files": ["closure.txt"],
            }],
        })
        return closure

    def test_remediation_must_follow_source_review_time(self):
        self.add_general_finding(False)
        with self.assertRaises(PipelineError) as caught:
            record_remediation(
                self.pipeline,
                self.remediation_record("2026-08-27T09:59:00+08:00"),
                self.tasks)
        self.assertIn("strictly after", str(caught.exception))

    def test_remediation_cannot_close_at_same_instant_as_source_review(self):
        self.add_general_finding(False)
        with self.assertRaises(PipelineError) as caught:
            record_remediation(
                self.pipeline,
                self.remediation_record("2026-08-27T02:00:00+00:00"),
                self.tasks)
        self.assertIn("strictly after", str(caught.exception))

    def test_phase1_receipt_cannot_be_replaced_to_remove_confirmation(self):
        self.add_general_finding(True)
        replacement = self.root / "general-replacement.xlsx"
        self.workbook("general_review", replacement)
        transcription = self.root / "general-replacement.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-27T10:06:00+08:00"))
        with self.assertRaises(PipelineError) as caught:
            ingest_receipt(self.pipeline, "general_review", replacement,
                           transcription, self.tasks)
        self.assertIn("immutable", str(caught.exception))
        stored = self.pipeline._load()["review_cycle"]["phase1"]["receipts"]
        self.assertTrue(
            stored["general_review"]["record"]["findings"][0]
            ["requires_confirmation"])

    def test_requested_confirmation_routes_to_changed_items_only(self):
        self.add_general_finding(True)
        (self.task / "prompt.md").write_text(
            "Complete the corrected task.\n", encoding="utf-8")
        record_remediation(
            self.pipeline,
            self.remediation_record("2026-08-27T10:10:00+08:00"),
            self.tasks)
        self.assertEqual(self.pipeline.status()["workflow_stage"],
                         "supplemental_review_kit_required")

    def test_changed_items_only_receipt_closes_phase1_without_full_rerun(self):
        self.add_general_finding(True)
        self.write_json(self.task / "provenance.json", {
            "review_note": "Corrected the general-review provenance issue."})
        record_remediation(
            self.pipeline,
            self.remediation_record("2026-08-27T10:10:00+08:00"),
            self.tasks)
        delivery = self.validation_tree()

        def fake_builder(_config, output, _node, _modules):
            Path(output).write_bytes(b"supplemental workbook placeholder")

        with mock.patch("review_kits._run_builder", side_effect=fake_builder):
            create_supplemental(
                self.pipeline, delivery, self.tasks, self.root / "supp-output",
                self.root / "node", self.root / "modules")
        state = self.pipeline._load()
        package = state["review_cycle"]["remediation"]["supplemental_package"]
        meta = json.loads((self.task / "task_meta.json").read_text(encoding="utf-8"))
        config = _supplemental_config(
            meta, self.items, self.codes,
            production_basis(self.pipeline, self.tasks),
            package["candidate_sha256"], state["review_cycle"], "general_review")
        main = self.metadata(package["candidate_sha256"])
        for index, item in enumerate(config["requirements"], start=12):
            main.update({
                "A%d" % index: item["requirement_id"],
                "B%d" % index: item["kind"],
                "C%d" % index: item["summary"],
                "D%d" % index: item["remediation_display"],
                "E%d" % index: "Confirmed",
                "G%d" % index: "N/A", "H%d" % index: "N/A",
                "I%d" % index: "N/A",
            })
        last = 11 + len(config["requirements"])
        main.update({"A%d" % (last + 3): "Conclusion",
                     "B%d" % (last + 3): "Pass",
                     "A%d" % (last + 4): "Substantive opinion",
                     "B%d" % (last + 4): "The corrected prompt closes my finding."})
        receipt = self.root / "general-supplemental.xlsx"
        self.xlsx(receipt, {"Supplemental Review": main})
        transcription = self.root / "general-supplemental.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-29T10:00:00+08:00"))
        result = ingest_supplemental(
            self.pipeline, "general_review", receipt, transcription, self.tasks)
        self.assertEqual(result["workflow_stage"], "pre_final_validation_required")
        receipt_state = self.pipeline._load()["review_cycle"]["phase1"]["receipts"]["general_review"]
        self.assertEqual(receipt_state["release_verdict"], "Pass")

    def test_failed_supplemental_receipt_cannot_be_replaced_without_remediation(self):
        self.add_general_finding(True)
        (self.task / "prompt.md").write_text(
            "Complete the corrected task.\n", encoding="utf-8")
        record_remediation(
            self.pipeline,
            self.remediation_record("2026-08-27T10:10:00+08:00"),
            self.tasks)
        delivery = self.validation_tree()
        task_row = json.loads((delivery / "tasks.jsonl").read_text(encoding="utf-8"))
        task_row["prompt"] = "Complete the corrected task."
        (delivery / "tasks.jsonl").write_text(
            json.dumps(task_row) + "\n", encoding="utf-8")

        def fake_builder(_config, output, _node, _modules):
            Path(output).write_bytes(b"supplemental workbook placeholder")

        with mock.patch("review_kits._run_builder", side_effect=fake_builder):
            create_supplemental(
                self.pipeline, delivery, self.tasks, self.root / "supp-output",
                self.root / "node", self.root / "modules")
        state = self.pipeline._load()
        state["review_cycle"]["remediation"]["supplemental_receipts"] = {
            "general_review": {"verdict": "Fail"},
        }
        state["review_cycle"]["status"] = "supplemental_review_failed"
        self.pipeline._save(state)

        replacement = self.root / "supplemental-replacement.xlsx"
        self.workbook("general_review", replacement)
        transcription = self.root / "supplemental-replacement.json"
        self.write_json(transcription, self.transcription(
            "general_review", "person-a", "2026-08-29T10:00:00+08:00"))
        with self.assertRaises(PipelineError) as caught:
            ingest_supplemental(
                self.pipeline, "general_review", replacement,
                transcription, self.tasks)
        self.assertIn("immutable", str(caught.exception))
        self.assertEqual(self.pipeline.status()["workflow_stage"],
                         "supplemental_review_failed")

    def test_failed_supplemental_reenters_remediation_then_passes(self):
        self.add_general_finding(True)
        self.write_json(self.task / "provenance.json", {
            "review_note": "First general-review correction."})
        record_remediation(
            self.pipeline,
            self.remediation_record("2026-08-27T10:10:00+08:00"),
            self.tasks)
        first_delivery = self.validation_tree()

        def fake_builder(_config, output, _node, _modules):
            Path(output).write_bytes(b"supplemental workbook placeholder")

        with mock.patch("review_kits._run_builder", side_effect=fake_builder):
            create_supplemental(
                self.pipeline, first_delivery, self.tasks,
                self.root / "first-supp-output", self.root / "node",
                self.root / "modules")
        failed_receipt = self.root / "general-supplemental-fail.xlsx"
        self.supplemental_workbook(
            failed_receipt, "Issue", "Fail",
            "The first correction does not fully resolve the finding.")
        failed_transcription = self.root / "general-supplemental-fail.json"
        self.write_json(failed_transcription, self.transcription(
            "general_review", "person-a", "2026-08-29T10:00:00+08:00"))
        failed = ingest_supplemental(
            self.pipeline, "general_review", failed_receipt,
            failed_transcription, self.tasks)
        self.assertEqual(failed["workflow_stage"], "supplemental_review_failed")
        failed_sha = failed["receipt_sha256"]

        self.write_json(self.task / "provenance.json", {
            "review_note": "Second and complete general-review correction."})
        second_evidence = self.root / "second-round-closure.txt"
        second_evidence.write_text("fully corrected", encoding="utf-8")
        second_closure = self.root / "second-round-closure.json"
        self.write_json(second_closure, {
            "task_id": self.pipeline._load()["task_id"],
            "findings": [{
                "finding_id": "SUP-G-G-F01", "disposition": "closed",
                "rationale": "Completed the correction requested in re-review.",
                "closed_at": "2026-08-29T10:10:00+08:00",
                "evidence_files": ["second-round-closure.txt"],
            }, {
                "finding_id": "SUP-G-G-DEPENDENCY-CHANGE",
                "disposition": "closed",
                "rationale": "Updated and rechecked the affected review input.",
                "closed_at": "2026-08-29T10:10:00+08:00",
                "evidence_files": ["second-round-closure.txt"],
            }],
        })
        record_remediation(self.pipeline, second_closure, self.tasks)
        state = self.pipeline._load()
        history = state["review_cycle"]["remediation_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(
            history[0]["supplemental_receipts"]["general_review"]["verdict"],
            "Fail")
        self.assertEqual(
            history[0]["supplemental_receipts"]["general_review"]
            ["source_receipt_sha256"], failed_sha)
        self.assertEqual(
            history[0]["supplemental_receipts"]["general_review"]
            ["record"]["items"][0]["decision"], "Issue")
        self.assertTrue(history[0]["artifact_digest"])
        self.assertEqual(history[0]["requirements"][0]["requirement_id"],
                         "G-F01")
        self.assertEqual(
            state["review_cycle"]["remediation"]["requirements"][0]
            ["requirement_id"], "SUP-G-G-F01")

        second_delivery = first_delivery
        with mock.patch("review_kits._run_builder", side_effect=fake_builder):
            create_supplemental(
                self.pipeline, second_delivery, self.tasks,
                self.root / "second-supp-output", self.root / "node",
                self.root / "modules")
        passed_receipt = self.root / "general-supplemental-pass.xlsx"
        self.supplemental_workbook(
            passed_receipt, "Confirmed", "Pass",
            "The second correction fully resolves the finding.")
        passed_transcription = self.root / "general-supplemental-pass.json"
        self.write_json(passed_transcription, self.transcription(
            "general_review", "person-a", "2026-08-29T10:20:00+08:00"))
        passed = ingest_supplemental(
            self.pipeline, "general_review", passed_receipt,
            passed_transcription, self.tasks)
        self.assertEqual(passed["workflow_stage"],
                         "pre_final_validation_required")
        state = self.pipeline._load()
        self.assertEqual(len(state["review_cycle"]["remediation_history"]), 1)
        self.assertEqual(
            state["review_cycle"]["remediation_history"][0]
            ["supplemental_receipts"]["general_review"]["verdict"], "Fail")
        self.assertEqual(
            state["review_cycle"]["remediation"]["supplemental_receipts"]
            ["general_review"]["verdict"], "Pass")

    def test_final_validation_rejects_delivery_changed_after_freeze(self):
        self.ingest("general_review", "person-a", "2026-08-27T10:00:00+08:00")
        self.ingest("occupational_expert_review", "person-b",
                    "2026-08-27T10:05:00+08:00")
        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"], "findings": []})
        record_remediation(self.pipeline, closure, self.tasks)
        self.pipeline.record_validation(self.validation_tree(), "pre_final")
        final_delivery = self.prepare_final_package()
        self.ingest("final_review", "person-c", "2026-08-27T11:05:00+08:00")
        record = json.loads((final_delivery / "tasks.jsonl").read_text(
            encoding="utf-8"))
        record["prompt"] = "Changed after final review."
        (final_delivery / "tasks.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.record_validation(final_delivery, "final")
        self.assertIn("changed after", str(caught.exception))

    def test_final_validation_allows_nonce_bearing_evidence_to_change(self):
        self.ingest("general_review", "person-a", "2026-08-27T10:00:00+08:00")
        self.ingest("occupational_expert_review", "person-b",
                    "2026-08-27T10:05:00+08:00")
        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"], "findings": []})
        record_remediation(self.pipeline, closure, self.tasks)
        self.pipeline.record_validation(self.validation_tree(), "pre_final")
        final_delivery = self.prepare_final_package()
        self.ingest("final_review", "person-c", "2026-08-27T11:05:00+08:00")
        (final_delivery / "validation_evidence" /
         self.pipeline._load()["task_id"] / "new-nonce.json").write_text(
             "{}", encoding="utf-8")
        self.pipeline.record_validation(final_delivery, "final")

    def test_final_package_contains_all_frozen_review_materials(self):
        self.ingest("general_review", "person-a", "2026-08-27T10:00:00+08:00")
        self.ingest("occupational_expert_review", "person-b",
                    "2026-08-27T10:05:00+08:00")
        closure = self.root / "closure.json"
        self.write_json(closure, {
            "task_id": self.pipeline._load()["task_id"], "findings": []})
        record_remediation(self.pipeline, closure, self.tasks)
        delivery = self.validation_tree()
        self.pipeline.record_validation(delivery, "pre_final")

        def fake_builder(_config, output, _node, _modules):
            Path(output).write_bytes(b"review workbook")

        with mock.patch("review_kits._run_builder", side_effect=fake_builder):
            result = create_final(
                self.pipeline, delivery, self.tasks, self.root / "output",
                self.root / "node", self.root / "modules")
        with zipfile.ZipFile(result["final_review_package"]) as archive:
            names = set(archive.namelist())
        expected_fragments = [
            "Final-Review.xlsx",
            "Read-Only-Materials/Post-Remediation-Candidate/tasks.jsonl",
            "Read-Only-Materials/Phase-1-Receipts/general_review/",
            "Read-Only-Materials/Phase-1-Receipts/occupational_expert_review/",
            "Read-Only-Materials/Remediation/",
            "Read-Only-Materials/Pre-Final-Validation/",
            "Read-Only-Materials/final_review_manifest.json",
        ]
        for fragment in expected_fragments:
            self.assertTrue(any(fragment in name for name in names), fragment)


if __name__ == "__main__":
    unittest.main()

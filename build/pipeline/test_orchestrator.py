"""Contract tests for the v2 pipeline orchestration.

These exercise the three things the evidence is supposed to prove: that a role
only ever sees what its contract allows, that a role cannot be signed off by the
agent it is meant to be independent of, and that a change upstream invalidates
everything downstream. The guards added in v2 — real-deliverable gold,
placeholder-free prompts, separating power, check coverage — are tested by the
rejection cases rather than by reading the code.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator import (Pipeline, PipelineError, DEFAULT_POLICY,
                          sample_rubric_item_count)


def rubric_items(count=40):
    """A rubric that satisfies policy: total 100 and at least 25 items."""
    items = [{"score": 2, "criterion": "Item %d states one checkable thing" % i,
              "rubric_item_id": str(uuid4()), "required": True} for i in range(30)]
    items += [{"score": 4, "criterion": "Judgement item %d" % i,
               "rubric_item_id": str(uuid4()), "required": True} for i in range(10)]
    return with_checks(items)


def with_checks(items, executable=24):
    """The accepted package ships each item's check inside the item. Items past
    the executable ones are settled by a person and say how."""
    for n, item in enumerate(items):
        if n < executable:
            item["check"] = {"type": "xlsx_cell_value",
                             "params": {"file": "wb.xlsx", "sheet": "S",
                                        "cell": "A1", "expected": 1}}
        else:
            item["verification"] = "Read the deliverable and judge the quality."
    return items


GOOD_PROVENANCE = {
    "source_type": "desensitization",
    "production_method": "supplier work record; de-identified by hand",
    "is_real_deliverable": True,
    "real_deliverable_files": [{
        "filename": "Evidence.pdf",
        "source_url": "https://example.test/Evidence.pdf",
        "source_sha256": "a" * 64,
    }],
    "rights_holder": "Supplier Ltd",
    "license": "Supplier grants GDPval evaluation and redistribution rights",
    "usage_scope": "GDPval evaluation and redistribution",
}
GOOD_BLUEPRINT = {
    "sector": "Retail Trade",
    "occupation": "General and Operations Managers",
    "language": "en",
    "output_contract": ["Vendor Comparison.xlsx"],
}
GOOD_NOTES = {
    "reasoning_points": ["an unquoted migration line", "a quotation that expires"],
    "guards": {}, "column_maps": {},
}
GOOD_SPEC = [{"filename": "Procurement Policy PS-2026-04.md"},
             {"filename": "Store Profile - Chaoyang Stores.xlsx"}]
GOOD_PROMPT = ("You are the operations manager. Using Procurement Policy "
               "PS-2026-04.md, produce Vendor Comparison.xlsx.")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "work"
        self.pipeline = Pipeline.initialise(self.workspace, str(uuid4()))
        self.inputs = self.base / "inputs"
        self.inputs.mkdir()
        for category in ("coverage", "source_manifest", "occupation_standard",
                         "material_pool", "references"):
            self.intake(category, category)
        # The rubric run is bound to the policy digest on purpose: changing a
        # threshold has to make the rubric stale, not silently stand.
        self.pipeline.add_artifact("policy", [DEFAULT_POLICY])

    def tearDown(self):
        self.tmp.cleanup()

    def intake(self, category, text):
        folder = self.inputs / category
        folder.mkdir(exist_ok=True)
        (folder / (category + ".txt")).write_text(text, encoding="utf-8")
        return self.pipeline.add_artifact(category, [folder])

    def write_outputs(self, run_root, outputs):
        for category, value in outputs.items():
            folder = run_root / "output" / category
            folder.mkdir(exist_ok=True)
            if category == "rubric":
                (folder / "rubric.json").write_text(json.dumps(value), encoding="utf-8")
            elif category == "reference_spec":
                (folder / "spec.json").write_text(json.dumps(value), encoding="utf-8")
            elif category == "prompt":
                (folder / "prompt.md").write_text(value, encoding="utf-8")
            elif isinstance(value, (dict, list)):
                (folder / "report.json").write_text(json.dumps(value), encoding="utf-8")
            else:
                (folder / "artifact.txt").write_text(value, encoding="utf-8")

    def complete(self, role, agent, context, outputs, decision="passed"):
        run_root = self.pipeline.prepare(role, agent, context)
        self.write_outputs(run_root, outputs)
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]
        self.pipeline.submit(run_id, decision)
        return run_root

    # -- building blocks ---------------------------------------------------
    def do_gold(self, provenance=None):
        return self.complete("gold_curator", "curator", "ctx-gold", {
            "gold": "the real deliverable",
            "gold_provenance": provenance or GOOD_PROVENANCE,
            "production_notes": "notes"})

    def do_design(self, spec=None, blueprint=None, notes=None):
        return self.complete("task_designer", "designer", "ctx-design", {
            "task_blueprint": blueprint or GOOD_BLUEPRINT,
            "design_notes": notes or GOOD_NOTES,
            "reference_spec": spec or GOOD_SPEC,
            "lineage_draft": "lineage"})

    def do_prompt(self, text=GOOD_PROMPT):
        return self.complete("prompt_author", "author", "ctx-prompt", {
            "prompt": text,
            "output_contract": {"files": ["Vendor Comparison.xlsx"]}})

    def do_solvers(self, agent="solver-strong", context="ctx-solver"):
        return self.complete("solver", agent, context, {
            "solver_deliverables": "solution",
            "solver_report": {
                "prompt_self_contained": True, "solvable": True,
                "task_multistep": True, "separating_power": "sufficient",
                "difficulty_evidence": ["three dependent calculations"],
                "blocking_ambiguities": []}})

    def do_verifier(self):
        return self.complete("verifier", "verifier", "ctx-verifier", {
            "expected_values": "values",
            "verifier_report": {"recompute_passed": True, "lineage_valid": True,
                                "mismatches": [],
                                "demands_without_landing_place": []}})

    def do_rubric(self, items=None):
        return self.complete("rubric", "rubric", "ctx-rubric",
                             {"rubric": rubric_items() if items is None else items})

    def run_script_gates(self):
        state = json.loads((self.workspace / "workflow.json").read_text())
        delivery = self.base / "delivery"
        manifests = delivery / "manifests"
        evidence = delivery / "validation_evidence" / state["task_id"]
        manifests.mkdir(parents=True)
        evidence.mkdir(parents=True)
        (evidence / "report.json").write_text('{"passed": true}', encoding="utf-8")
        status = {"task_id": state["task_id"],
                  "checks": [{"check": "fixed_registry", "status": "passed"}]}
        (manifests / "validation_status.jsonl").write_text(
            json.dumps(status) + "\n", encoding="utf-8")
        self.pipeline.record_validation(delivery)

    def build_to_rubric(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        solver = self.do_solvers()
        self.do_verifier()
        self.do_rubric()
        return solver

    def build_through_review(self):
        self.build_to_rubric()
        self.run_script_gates()

    # -- tests -------------------------------------------------------------
    def test_visibility_independence_and_staleness(self):
        self.do_gold()
        self.do_design()
        # The designer may not also write the prompt: distinct_from is what
        # makes the two judgements independent rather than one long thought.
        with self.assertRaises(PipelineError):
            self.pipeline.prepare("prompt_author", "designer", "ctx-prompt")
        self.do_prompt()

        solver = self.pipeline.prepare("solver", "solver-strong", "ctx-solver")
        self.assertEqual(sorted(p.name for p in (solver / "input").iterdir()),
                         ["prompt", "references"])
        with self.assertRaises(PipelineError):
            self.pipeline.prepare("solver_weak", "solver-strong", "ctx-other")
        self.pipeline.submit(
            json.loads((solver / "run_contract.json").read_text())["run_id"],
            "failed", "T13.SCENARIO_UNSOLVABLE")

        # The declared route repairs the design and prompt before a fresh
        # downstream wave can be started.
        self.do_design()
        self.do_prompt()
        # The retry needs a context that never saw the first attempt.
        self.do_solvers(agent="solver-second", context="ctx-solver-2")
        verifier = self.do_verifier()
        seen = [p.name for p in (verifier / "input").iterdir()]
        self.assertNotIn("production_notes", seen)
        self.assertNotIn("design_notes", seen)
        self.assertNotIn("rubric", seen)
        self.do_rubric()
        self.assertEqual(self.pipeline.status()["roles"]["rubric"], "current")

        replacement = self.base / "revised-prompt.md"
        replacement.write_text("revised", encoding="utf-8")
        self.pipeline.add_artifact("prompt", [replacement])
        after = self.pipeline.status()
        self.assertEqual(after["roles"]["prompt_author"], "stale_or_failed")
        self.assertEqual(after["roles"]["solver"], "stale_or_failed")
        self.assertEqual(after["roles"]["rubric"], "stale_or_failed")

    def test_generated_gold_is_rejected(self):
        with self.assertRaises(PipelineError) as caught:
            self.do_gold(dict(GOOD_PROVENANCE,
                              source_type="generated_deliverable",
                              production_method="drafted by a script",
                              is_real_deliverable=False))
        self.assertIn("accepted path", str(caught.exception))

    def test_gold_may_not_understate_reconstruction(self):
        with self.assertRaises(PipelineError):
            self.do_gold(dict(GOOD_PROVENANCE,
                              source_type="real_input_and_real_deliverable",
                              production_method="", is_real_deliverable=True))

    def test_rubric_required_defaults_true_and_explicit_false_is_preserved(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        self.do_solvers()
        self.do_verifier()
        items = rubric_items()
        for item in items:
            item.pop("required")
        run_root = self.do_rubric(items)
        written = json.loads((run_root / "output" / "rubric" /
                              "rubric.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["required"] is True for item in written))

        bad = rubric_items()
        bad[0]["required"] = False
        run_root = self.complete("rubric", "rubric-2", "ctx-rubric-2", {"rubric": bad})
        written = json.loads((run_root / "output" / "rubric" /
                              "rubric.json").read_text(encoding="utf-8"))
        self.assertIs(written[0]["required"], False)

    def test_rubric_item_count_sampling_rounds_and_lower_truncates(self):
        class StubRng:
            def __init__(self, value):
                self.value = value

            def gauss(self, mean, stddev):
                return self.value

        self.assertEqual(sample_rubric_item_count(StubRng(30.49)), 30)
        self.assertEqual(sample_rubric_item_count(StubRng(30.50)), 31)
        self.assertEqual(sample_rubric_item_count(StubRng(2.0)), 25)

    def test_gold_preflight_rejects_a_secret_and_writes_evidence(self):
        run_root = self.pipeline.prepare("gold_curator", "curator", "ctx-gold")
        self.write_outputs(run_root, {
            "gold": "api_key=abcdefghijklmnop\nsource=/Users/alice/private.txt",
            "gold_provenance": GOOD_PROVENANCE,
            "production_notes": "notes"})
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]

        with self.assertRaises(PipelineError) as caught:
            self.pipeline.submit(run_id, "passed")

        self.assertIn("preflight", str(caught.exception))
        report = json.loads((run_root / "t10_preflight.json").read_text())
        statuses = {item["check"]: item["status"] for item in report["checks"]}
        self.assertEqual(statuses["secrets"], "failed")
        self.assertEqual(statuses["absolute_or_traversal_paths"], "failed")
        state = json.loads((self.workspace / "workflow.json").read_text())
        self.assertFalse(state["runs"][-1]["preflight_evidence"]["passed"])

    def test_gold_preflight_rejects_office_metadata(self):
        run_root = self.pipeline.prepare("gold_curator", "curator", "ctx-gold")
        self.write_outputs(run_root, {
            "gold_provenance": GOOD_PROVENANCE,
            "production_notes": "notes"})
        gold = run_root / "output" / "gold"
        gold.mkdir()
        book = openpyxl.Workbook()
        book.active["A1"] = '=WEBSERVICE("https://example.invalid")'
        book.save(gold / "Deliverable.xlsx")
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]

        with self.assertRaises(PipelineError):
            self.pipeline.submit(run_id, "passed")

        report = json.loads((run_root / "t10_preflight.json").read_text())
        statuses = {item["check"]: item["status"] for item in report["checks"]}
        self.assertEqual(statuses["office_pdf_metadata"], "failed")
        self.assertEqual(statuses["malicious_content"], "failed")

    def test_gold_preflight_rejects_unresolved_or_missing_rights(self):
        bad = dict(GOOD_PROVENANCE, usage_scope="",
                   known_blockers_carried_forward=["redistribution unresolved"])
        with self.assertRaises(PipelineError) as caught:
            self.do_gold(bad)
        self.assertIn("provenance_rights", str(caught.exception))

    def test_new_runs_bind_policy_contract_and_preflight_evidence(self):
        run_root = self.do_gold()
        contract = json.loads((run_root / "run_contract.json").read_text())
        self.assertEqual(len(contract["policy_digest"]), 64)
        self.assertEqual(len(contract["policy_scope_digest"]), 64)
        self.assertEqual(len(contract["contracts_digest"]), 64)
        state = json.loads((self.workspace / "workflow.json").read_text())
        run = state["runs"][-1]
        self.assertTrue(run["preflight_evidence"]["passed"])
        run["policy_scope_digest"] = "0" * 64
        (self.workspace / "workflow.json").write_text(json.dumps(state))
        self.assertEqual(self.pipeline.status()["roles"]["gold_curator"],
                         "stale_or_failed")
        # Historical records without digest fields remain compatible.
        state = json.loads((self.workspace / "workflow.json").read_text())
        state["runs"][-1].pop("policy_scope_digest")
        state["runs"][-1].pop("policy_scope_sections")
        state["runs"][-1].pop("policy_digest")
        state["runs"][-1].pop("contracts_digest")
        (self.workspace / "workflow.json").write_text(json.dumps(state))
        self.assertEqual(self.pipeline.status()["roles"]["gold_curator"], "current")

    def test_placeholder_prompt_is_rejected(self):
        self.do_gold()
        self.do_design()
        with self.assertRaises(PipelineError) as caught:
            self.do_prompt("Produce the workbook. Currency: not specified.")
        self.assertIn("placeholder", str(caught.exception))

    def test_reference_spec_format_is_enforced(self):
        self.do_gold()
        with self.assertRaises(PipelineError) as caught:
            self.do_design(spec=[{"filename": "Quotation.pdf"}])
        self.assertIn("allowed extensions", str(caught.exception))

    def test_blueprint_may_not_carry_an_evaluator_only_field(self):
        self.do_gold()
        with self.assertRaises(PipelineError) as caught:
            self.do_design(blueprint=dict(GOOD_BLUEPRINT,
                                          reasoning_points=["the answer is 42"]))
        self.assertIn("evaluator-only", str(caught.exception))

    def test_blueprint_without_reasoning_points_is_rejected(self):
        self.do_gold()
        with self.assertRaises(PipelineError) as caught:
            self.do_design(notes=dict(GOOD_NOTES, reasoning_points=[]))
        self.assertIn("reasoning points", str(caught.exception))

    def test_rubric_item_must_be_judgeable(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        self.do_solvers()
        self.do_verifier()
        items = rubric_items()
        items[0].pop("check", None)
        items[0].pop("verification", None)
        with self.assertRaises(PipelineError) as caught:
            self.do_rubric(items)
        self.assertIn("verification", str(caught.exception))

    def test_rubric_rejects_existence_criteria(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        self.do_solvers()
        self.do_verifier()
        items = rubric_items()
        items[0]["criterion"] = "The workbook opens without repair"
        with self.assertRaises(PipelineError) as caught:
            self.do_rubric(items)
        self.assertIn("excludes", str(caught.exception))

    def test_reported_prompt_leak_fails_the_solver_run(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        run_root = self.pipeline.prepare("solver", "solver-strong", "ctx-solver")
        self.write_outputs(run_root, {
            "solver_deliverables": "solution",
            "solver_report": {
                "prompt_self_contained": True, "solvable": True,
                "task_multistep": True, "separating_power": "sufficient",
                "difficulty_evidence": ["three dependent calculations"],
                "blocking_ambiguities": [
                    "PROMPT LEAK: the required filename states the answer"]}})
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.submit(run_id, "passed")
        self.assertIn("prompt leakage", str(caught.exception))

    def test_a_cold_role_cannot_be_retried_in_a_context_that_saw_the_answer(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        first = self.pipeline.prepare("solver", "solver-strong", "ctx-solver")
        run_id = json.loads((first / "run_contract.json").read_text())["run_id"]
        self.pipeline.submit(run_id, "failed", "T13.SCENARIO_UNSOLVABLE")
        self.do_design()
        self.do_prompt()
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.prepare("solver", "solver-strong", "ctx-solver-2")
        self.assertIn("cold-context role", str(caught.exception))
        # A genuinely fresh attempt is fine.
        self.pipeline.prepare("solver", "solver-second", "ctx-solver-2")

    def test_failure_route_invalidates_the_target_and_its_descendants(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        run_root = self.pipeline.prepare("solver", "solver-strong", "ctx-solver")
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]
        self.pipeline.submit(run_id, "failed", "T13.SCENARIO_UNSOLVABLE")
        state = json.loads((self.pipeline.root / "workflow.json").read_text())
        self.assertEqual(
            {"task_designer", "prompt_author", "solver", "verifier", "rubric"},
            set(state["invalidated_roles"]),
        )
        self.assertEqual("task_designer", state["invalidated_roles"]["solver"]["route_to"])

    def test_a_demand_with_nowhere_to_land_fails_the_verifier(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        self.do_solvers()
        run_root = self.pipeline.prepare("verifier", "verifier", "ctx-verifier")
        self.write_outputs(run_root, {
            "expected_values": "values",
            "verifier_report": {
                "recompute_passed": True, "lineage_valid": True, "mismatches": [],
                "demands_without_landing_place": [
                    "the prompt asks the meeting undertaking to be settled; the "
                    "gold never mentions a meeting"]}})
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.submit(run_id, "passed")
        self.assertIn("nowhere to land", str(caught.exception))

    def test_verifier_must_answer_the_landing_place_question(self):
        self.do_gold()
        self.do_design()
        self.do_prompt()
        self.do_solvers()
        run_root = self.pipeline.prepare("verifier", "verifier", "ctx-verifier")
        self.write_outputs(run_root, {
            "expected_values": "values",
            "verifier_report": {"recompute_passed": True, "lineage_valid": True,
                                "mismatches": []}})
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.submit(run_id, "passed")
        self.assertIn("demands_without_landing_place", str(caught.exception))

    def test_human_review_is_separate_release_gate(self):
        self.build_through_review()
        self.assertFalse(self.pipeline.status()["release_ready"])

        review_dir = self.base / "human"
        review_dir.mkdir()
        for name in ("general.txt", "expert.txt", "final.txt",
                     "closure.txt", "gold-marking.json"):
            (review_dir / name).write_text("signed evidence", encoding="utf-8")
        state = json.loads((self.workspace / "workflow.json").read_text())
        record = {
            "task_id": state["task_id"],
            "layers": [
                {"layer": "general_review", "reviewer_id": "person-a",
                 "reviewed_at": "2026-08-22T10:00:00+00:00", "status": "passed",
                 "opinion": "structure checked", "evidence_files": ["general.txt"],
                 "finding_ids": ["G1"]},
                {"layer": "occupational_expert_review", "reviewer_id": "person-b",
                 "reviewed_at": "2026-08-22T11:00:00+00:00", "status": "passed",
                 "opinion": "professional content checked",
                 "evidence_files": ["expert.txt"],
                 "credential_status": "not_supplied",
                 "rubric_version_reviewed": "v1",
                 "substantive_objections": ["R03 wording was not judgeable"],
                 "adoption_actions": ["R03 rewritten and adopted"],
                 "finding_ids": ["E1"]},
                {"layer": "final_review", "reviewer_id": "person-c",
                 "reviewed_at": "2026-08-22T12:00:00+00:00", "status": "passed",
                 "opinion": "rework closure checked", "evidence_files": ["final.txt"],
                 "open_findings": [],
                 "finding_dispositions": [
                     {"finding_id": "G1", "disposition": "closed",
                      "rationale": "source record corrected",
                      "closed_at": "2026-08-22T11:30:00+00:00",
                      "evidence_files": ["closure.txt"]},
                     {"finding_id": "E1", "disposition": "accepted_without_change",
                      "rationale": "real gold is preserved and scoring records the gap",
                      "closed_at": "2026-08-22T11:45:00+00:00",
                      "evidence_files": ["closure.txt"]}]},
            ],
            "gold_marking_evidence_files": ["gold-marking.json"],
            "independence_statement": (
                "supplier-recorded review; not represented as independent "
                "third-party certification")}
        record_path = review_dir / "review.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.pipeline.record_human_review(record_path)
        self.assertTrue(self.pipeline.status()["release_ready"])

    def test_human_review_policy_change_does_not_stale_production_roles(self):
        policy_path = self.base / "scoped-policy.json"
        policy = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        workspace = self.base / "scoped-policy-work"
        Pipeline.initialise(workspace, str(uuid4()))

        previous_pipeline = self.pipeline
        self.pipeline = Pipeline(workspace, policy_path=policy_path)
        try:
            for category in ("coverage", "source_manifest", "occupation_standard",
                             "material_pool", "references"):
                self.intake(category, category)
            self.pipeline.add_artifact("policy", [policy_path])
            self.build_to_rubric()
            self.assertTrue(all(
                value == "current" for value in
                self.pipeline.status()["roles"].values()))

            policy["human_review"]["credential_evidence_in_package_required"] = not (
                policy["human_review"]["credential_evidence_in_package_required"])
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            reloaded = Pipeline(workspace, policy_path=policy_path)
            self.assertTrue(all(
                value == "current" for value in reloaded.status()["roles"].values()))
        finally:
            self.pipeline = previous_pipeline

    def test_task_design_policy_change_stales_its_downstream_roles(self):
        policy_path = self.base / "design-policy.json"
        policy = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        workspace = self.base / "design-policy-work"
        Pipeline.initialise(workspace, str(uuid4()))

        previous_pipeline = self.pipeline
        self.pipeline = Pipeline(workspace, policy_path=policy_path)
        try:
            for category in ("coverage", "source_manifest", "occupation_standard",
                             "material_pool", "references"):
                self.intake(category, category)
            self.pipeline.add_artifact("policy", [policy_path])
            self.build_to_rubric()

            policy["reference_files"]["forbidden_formats"].append("xlsx")
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            roles = Pipeline(workspace, policy_path=policy_path).status()["roles"]
            self.assertEqual("current", roles["gold_curator"])
            for role in ("task_designer", "prompt_author", "solver",
                         "verifier", "rubric"):
                self.assertEqual("stale_or_failed", roles[role])
        finally:
            self.pipeline = previous_pipeline

    def test_legacy_policy_scope_migration_requires_exact_declared_diff(self):
        old_path = self.base / "legacy-policy.json"
        new_path = self.base / "current-policy.json"
        old_policy = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
        old_policy["human_review"]["credential_evidence_in_package_required"] = True
        old_policy["human_review"].pop("credential_status_when_evidence_absent", None)
        old_path.write_text(json.dumps(old_policy), encoding="utf-8")
        workspace = self.base / "legacy-policy-work"
        Pipeline.initialise(workspace, str(uuid4()))

        previous_pipeline = self.pipeline
        self.pipeline = Pipeline(workspace, policy_path=old_path)
        try:
            for category in ("coverage", "source_manifest", "occupation_standard",
                             "material_pool", "references"):
                self.intake(category, category)
            self.pipeline.add_artifact("policy", [old_path])
            self.build_to_rubric()
            state = json.loads((workspace / "workflow.json").read_text())
            for run in state["runs"]:
                run.pop("policy_scope_digest", None)
                run.pop("policy_scope_sections", None)
                contract_path = workspace / "runs" / run["run_id"] / "run_contract.json"
                contract = json.loads(contract_path.read_text())
                contract.pop("policy_scope_digest", None)
                contract.pop("policy_scope_sections", None)
                contract_path.write_text(json.dumps(contract), encoding="utf-8")
            (workspace / "workflow.json").write_text(
                json.dumps(state), encoding="utf-8")

            new_policy = json.loads(json.dumps(old_policy))
            new_policy["human_review"]["credential_evidence_in_package_required"] = False
            new_policy["human_review"]["credential_status_when_evidence_absent"] = (
                "not_supplied")
            new_path.write_text(json.dumps(new_policy), encoding="utf-8")
            migrated = Pipeline(workspace, policy_path=new_path)
            self.assertTrue(any(
                value == "stale_or_failed" for value in
                migrated.status()["roles"].values()))
            result = migrated.migrate_policy_scopes(old_path, ["human_review"])
            self.assertEqual(6, result["migrated_runs"])
            self.assertTrue(all(
                value == "current" for value in migrated.status()["roles"].values()))
        finally:
            self.pipeline = previous_pipeline

    def test_final_review_requires_a_real_time_gap(self):
        self.build_through_review()
        review_dir = self.base / "human-equal-time"
        review_dir.mkdir()
        for name in ("general.txt", "expert.txt", "final.txt", "credential.txt",
                     "closure.txt", "gold-marking.json"):
            (review_dir / name).write_text("signed evidence", encoding="utf-8")
        task_id = json.loads((self.workspace / "workflow.json").read_text())["task_id"]
        record = {
            "task_id": task_id,
            "layers": [
                {"layer": "general_review", "reviewer_id": "person-a",
                 "reviewed_at": "2026-08-22T10:00:00+00:00", "status": "passed",
                 "opinion": "checked", "evidence_files": ["general.txt"],
                 "finding_ids": ["G1"]},
                {"layer": "occupational_expert_review", "reviewer_id": "person-b",
                 "reviewed_at": "2026-08-22T12:00:00+00:00", "status": "passed",
                 "opinion": "checked", "evidence_files": ["expert.txt"],
                 "qualification_evidence_files": ["credential.txt"],
                 "rubric_version_reviewed": "v1",
                 "substantive_objections": ["R03"],
                 "adoption_actions": ["R03 rejected"],
                 "finding_ids": ["E1"]},
                {"layer": "final_review", "reviewer_id": "person-c",
                 "reviewed_at": "2026-08-22T12:00:00+00:00", "status": "passed",
                 "opinion": "closed", "evidence_files": ["final.txt"],
                 "open_findings": [],
                 "finding_dispositions": [
                     {"finding_id": "G1", "disposition": "closed",
                      "rationale": "closed",
                      "closed_at": "2026-08-22T11:30:00+00:00",
                      "evidence_files": ["closure.txt"]},
                     {"finding_id": "E1", "disposition": "closed",
                      "rationale": "closed",
                      "closed_at": "2026-08-22T11:30:00+00:00",
                      "evidence_files": ["closure.txt"]}]},
            ],
            "gold_marking_evidence_files": ["gold-marking.json"],
            "independence_statement": (
                "supplier-recorded review; not represented as independent "
                "third-party certification")}
        path = review_dir / "review.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.record_human_review(path)
        self.assertIn("strictly later", str(caught.exception))

    def test_validation_not_run_cannot_be_registered(self):
        self.build_to_rubric()
        state = json.loads((self.workspace / "workflow.json").read_text())
        delivery = self.base / "bad-delivery"
        evidence = delivery / "validation_evidence" / state["task_id"]
        (delivery / "manifests").mkdir(parents=True)
        evidence.mkdir(parents=True)
        (evidence / "report.json").write_text("{}", encoding="utf-8")
        row = {"task_id": state["task_id"],
               "checks": [{"check": "human_review", "status": "not_run"}]}
        (delivery / "manifests" / "validation_status.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.record_validation(delivery)
        self.assertIn("not_run", str(caught.exception))

    def test_human_review_requires_current_validation(self):
        self.build_to_rubric()
        review = self.base / "premature-review.json"
        review.write_text("{}", encoding="utf-8")
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.record_human_review(review)
        self.assertIn("current validation gate", str(caught.exception))

    def test_modified_run_input_is_rejected(self):
        self.do_gold()
        self.do_design()
        run_root = self.pipeline.prepare("prompt_author", "author", "ctx-prompt")
        source = next(path for path in
                      (run_root / "input" / "task_blueprint").rglob("*")
                      if path.is_file())
        source.chmod(0o644)
        source.write_text("tampered", encoding="utf-8")
        self.write_outputs(run_root, {
            "prompt": GOOD_PROMPT,
            "output_contract": {"files": ["Vendor Comparison.xlsx"]}})
        run_id = json.loads((run_root / "run_contract.json").read_text())["run_id"]
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.submit(run_id, "passed")
        self.assertIn("modified", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

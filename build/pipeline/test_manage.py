import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from manage import DEFAULT_POLICY, PlanError, format_plan, main, plan_next


class ManageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def workspace(self, name, artifacts=None, runs=None, task_id=None):
        workspace = self.root / name
        workspace.mkdir()
        state = {
            "schema_version": "1.0",
            "task_id": task_id or str(uuid4()),
            "artifacts": artifacts or {},
            "runs": runs or [],
            "gates": {},
            "human_review": None,
        }
        (workspace / "workflow.json").write_text(
            json.dumps(state), encoding="utf-8")
        return workspace, state

    def artifact(self, name, digest=None, produced_by="intake"):
        return {"digest": digest or "digest-" + name,
                "path": "_artifacts/%s/value" % name,
                "produced_by": produced_by}

    def policy(self, **overrides):
        value = json.loads(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
        value["execution"].update(overrides)
        path = self.root / ("policy-%d.json" % len(list(
            self.root.glob("policy-*.json"))))
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def passed_run(self, role, run_id, inputs, outputs, created_at):
        return {
            "run_id": run_id,
            "role": role,
            "status": "completed",
            "decision": "passed",
            "created_at": created_at,
            "input_artifacts": inputs,
            "output_artifacts": outputs,
        }

    def gold_current(self, suffix=""):
        run_id = "gold" + suffix
        artifacts = {
            name: self.artifact(name) for name in
            ("material_pool", "source_manifest", "occupation_standard", "coverage")
        }
        outputs = {}
        for name in ("gold", "gold_provenance", "production_notes"):
            digest = "digest-%s%s" % (name, suffix)
            artifacts[name] = self.artifact(name, digest, run_id)
            outputs[name] = digest
        inputs = {name: artifacts[name]["digest"] for name in
                  ("material_pool", "source_manifest", "occupation_standard",
                   "coverage")}
        run = self.passed_run("gold_curator", run_id, inputs, outputs,
                              "2026-08-23T00:00:00+00:00")
        return artifacts, [run]

    def design_current(self, artifacts, runs, suffix=""):
        run_id = "design" + suffix
        outputs = {}
        for name in ("task_blueprint", "design_notes", "reference_spec",
                     "lineage_draft"):
            digest = "digest-%s%s" % (name, suffix)
            artifacts[name] = self.artifact(name, digest, run_id)
            outputs[name] = digest
        inputs = {name: artifacts[name]["digest"] for name in
                  ("coverage", "occupation_standard", "gold", "gold_provenance")}
        runs.append(self.passed_run("task_designer", run_id, inputs, outputs,
                                    "2026-08-23T00:01:00+00:00"))
        artifacts["references"] = self.artifact("references")

    def prompt_current(self, artifacts, runs, suffix=""):
        run_id = "prompt" + suffix
        artifacts["prompt"] = self.artifact("prompt", "digest-prompt" + suffix,
                                            run_id)
        artifacts["output_contract"] = self.artifact(
            "output_contract", "digest-output-contract" + suffix, run_id)
        inputs = {name: artifacts[name]["digest"] for name in
                  ("occupation_standard", "task_blueprint", "references")}
        runs.append(self.passed_run("prompt_author", run_id, inputs, {
            "prompt": artifacts["prompt"]["digest"],
            "output_contract": artifacts["output_contract"]["digest"],
        }, "2026-08-23T00:02:00+00:00"))

    def test_t10_is_awaiting_human_not_agent_work(self):
        _, state = self.workspace("empty")
        plan = plan_next(self.root, slots=4)
        self.assertEqual(plan["wave"], [])
        self.assertEqual(plan["awaiting_human"][0]["task_id"], state["task_id"])
        self.assertEqual(plan["awaiting_human"][0]["role_id"], "T10")
        self.assertEqual(plan["awaiting_human"][0]["model_tier"], "L0")

    def test_tasks_parallelize_but_slots_limit_wave(self):
        for name in ("b-task", "a-task"):
            artifacts, runs = self.gold_current("-" + name)
            self.workspace(name, artifacts, runs)
        plan = plan_next(self.root, slots=1)
        self.assertEqual(len(plan["wave"]), 1)
        self.assertEqual(plan["wave"][0]["role"], "task_designer")
        self.assertEqual(plan["wave"][0]["instance_id"], "A-001")
        self.assertEqual(plan["queued_count"], 1)

        expanded = plan_next(self.root, slots=2)
        self.assertEqual(len(expanded["wave"]), 2)
        self.assertEqual({item["task_id"] for item in expanded["wave"]},
                         {json.loads((path / "workflow.json").read_text())["task_id"]
                          for path in (self.root / "a-task", self.root / "b-task")})

    def test_single_task_chooses_earliest_noncurrent_role(self):
        artifacts, runs = self.gold_current()
        self.design_current(artifacts, runs)
        self.workspace("task", artifacts, runs)
        plan = plan_next(self.root, slots=3)
        self.assertEqual([item["role"] for item in plan["wave"]], ["prompt_author"])
        item = plan["wave"][0]
        self.assertEqual(item["role_id"], "T12")
        self.assertIn("references", item["required_inputs"])
        self.assertTrue(all(value["mode"] == "read-only"
                            for value in item["readonly_inputs"]))
        self.assertEqual(
            [(route["reason_code"], route["route_to"])
             for route in item["failure_routes"]],
            [("T12.PROMPT_INCOMPLETE", "prompt_author"),
             ("T12.BLUEPRINT_GAP", "task_designer"),
             ("T12.PROMPT_LEAK", "prompt_author")])
        self.assertTrue(all(route["action"] for route in item["failure_routes"]))

    def test_solver_and_verifier_are_dispatched_in_parallel_after_prompt(self):
        artifacts, runs = self.gold_current()
        self.design_current(artifacts, runs)
        self.prompt_current(artifacts, runs)
        self.workspace("task", artifacts, runs)
        plan = plan_next(self.root, slots=3)
        self.assertEqual([item["role"] for item in plan["wave"]],
                         ["solver", "verifier"])
        self.assertEqual(plan["queued_count"], 0)

    def test_changed_digest_rewinds_only_to_stale_role(self):
        artifacts, runs = self.gold_current()
        self.design_current(artifacts, runs)
        artifacts["task_blueprint"]["digest"] = "revised-blueprint"
        self.workspace("task", artifacts, runs)
        plan = plan_next(self.root, slots=2)
        self.assertEqual(plan["wave"][0]["role"], "task_designer")

    def test_prepared_run_occupies_slot_and_keeps_stable_number(self):
        artifacts, runs = self.gold_current("-one")
        prepared = {
            "run_id": "prepared-design",
            "role": "task_designer",
            "status": "prepared",
            "decision": None,
            "created_at": "2026-08-23T00:02:00+00:00",
            "input_artifacts": {name: artifacts[name]["digest"] for name in
                                ("coverage", "occupation_standard", "gold",
                                 "gold_provenance")},
            "output_artifacts": {},
        }
        runs.append(prepared)
        self.workspace("one", artifacts, runs)
        other_artifacts, other_runs = self.gold_current("-two")
        self.workspace("two", other_artifacts, other_runs)

        plan = plan_next(self.root, slots=1)
        self.assertEqual(plan["active"][0]["instance_id"], "A-001")
        self.assertEqual(plan["wave"], [])
        again = plan_next(self.root, slots=2)
        self.assertEqual(again["active"][0]["instance_id"], "A-001")
        self.assertEqual(again["wave"][0]["instance_id"], "A-002")

    def test_only_one_prepared_run_per_task_occupies_a_slot(self):
        artifacts, runs = self.gold_current()
        for number in range(2):
            runs.append({
                "run_id": "prepared-%d" % number,
                "role": "task_designer",
                "status": "prepared",
                "decision": None,
                "created_at": "2026-08-23T00:0%d:00+00:00" % (number + 2),
                "input_artifacts": {name: artifacts[name]["digest"] for name in
                                    ("coverage", "occupation_standard", "gold",
                                     "gold_provenance")},
                "output_artifacts": {},
            })
        self.workspace("task", artifacts, runs)
        plan = plan_next(self.root, slots=2)
        self.assertEqual(len(plan["active"]), 1)
        self.assertEqual(plan["occupied_slots"], 1)
        self.assertEqual(plan["superseded_prepared_count"], 1)
        self.assertEqual(plan["active"][0]["run_id"], "prepared-1")

    def test_later_current_run_supersedes_older_prepared_same_role(self):
        artifacts, runs = self.gold_current()
        runs.append({
            "run_id": "abandoned-design",
            "role": "task_designer",
            "status": "prepared",
            "decision": None,
            "created_at": "2026-08-23T00:01:00+00:00",
            "input_artifacts": {name: artifacts[name]["digest"] for name in
                                ("coverage", "occupation_standard", "gold",
                                 "gold_provenance")},
            "output_artifacts": {},
        })
        self.design_current(artifacts, runs)
        self.workspace("task", artifacts, runs)

        plan = plan_next(self.root, slots=1)
        self.assertEqual([], plan["active"])
        self.assertEqual("prompt_author", plan["wave"][0]["role"])
        self.assertEqual(1, plan["superseded_prepared_count"])

    def test_stale_prepared_input_is_visible(self):
        artifacts, runs = self.gold_current()
        runs.append({
            "run_id": "prepared-design", "role": "task_designer",
            "status": "prepared", "decision": None,
            "created_at": "2026-08-23T00:02:00+00:00",
            "input_artifacts": {"gold": "old-gold"}, "output_artifacts": {},
        })
        self.workspace("task", artifacts, runs)
        item = plan_next(self.root, slots=1)["active"][0]
        self.assertFalse(item["input_current"])
        self.assertIn("stale", item["completion"])

    def test_prepared_at_role_attempt_limit_remains_the_last_legal_attempt(self):
        artifacts, runs = self.gold_current("-blocked")
        for number in range(3):
            runs.append({
                "run_id": "prepared-design-%d" % number,
                "role": "task_designer", "status": "prepared",
                "decision": None,
                "created_at": "2026-08-23T00:0%d:00+00:00" % (number + 1),
                "input_artifacts": {name: artifacts[name]["digest"] for name in
                                    ("coverage", "occupation_standard", "gold",
                                     "gold_provenance")},
                "output_artifacts": {},
            })
        self.workspace("blocked", artifacts, runs)
        other_artifacts, other_runs = self.gold_current("-ready")
        self.workspace("ready", other_artifacts, other_runs)

        plan = plan_next(self.root, slots=1)
        self.assertEqual(1, len(plan["active"]))
        self.assertEqual("prepared-design-2", plan["active"][0]["run_id"])
        self.assertEqual(1, plan["occupied_slots"])
        self.assertEqual([], plan["wave"])

    def test_prepared_at_task_run_limit_remains_active(self):
        artifacts, runs = self.gold_current()
        runs.append({
            "run_id": "prepared-design", "role": "task_designer",
            "status": "prepared", "decision": None,
            "created_at": "2026-08-23T00:01:00+00:00",
            "input_artifacts": {name: artifacts[name]["digest"] for name in
                                ("coverage", "occupation_standard", "gold",
                                 "gold_provenance")},
            "output_artifacts": {},
        })
        self.workspace("task", artifacts, runs)
        policy = self.policy(max_agent_runs_per_task=1)
        plan = plan_next(self.root, slots=1, policy_path=policy)
        self.assertEqual(1, len(plan["active"]))
        self.assertEqual("prepared-design", plan["active"][0]["run_id"])
        self.assertEqual(0, plan["available_slots"])
        self.assertEqual([], plan["blocked"])

    def test_json_cli_and_text_format(self):
        artifacts, runs = self.gold_current()
        self.workspace("task", artifacts, runs)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["next", str(self.root), "--slots", "1", "--json"])
        self.assertEqual(code, 0)
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["wave"][0]["role_id"], "T11")
        text = format_plan(value)
        self.assertIn("NEXT WAVE", text)
        self.assertIn("read-only:", text)
        self.assertIn("outputs:", text)
        self.assertIn("T11.INPUT_FACT_GAP", text)

    def test_t10_stop_route_is_visible_while_awaiting_human(self):
        self.workspace("task")
        item = plan_next(self.root, slots=1)["awaiting_human"][0]
        stop = [route for route in item["failure_routes"]
                if route["route_to"] == "stop"]
        self.assertEqual(stop[0]["reason_code"], "T10.DELIVERABLE_NOT_REAL")
        self.assertTrue(stop[0]["action"])

    def test_role_attempt_budget_blocks_redispatch(self):
        artifacts, runs = self.gold_current()
        for number in range(3):
            runs.append({
                "run_id": "design-failed-%d" % number,
                "role": "task_designer",
                "status": "completed",
                "decision": "failed",
                "created_at": "2026-08-23T00:0%d:00+00:00" % (number + 1),
                "input_artifacts": {}, "output_artifacts": {},
            })
        self.workspace("task", artifacts, runs)
        plan = plan_next(self.root, slots=2)
        self.assertEqual(plan["wave"], [])
        block = plan["blocked"][0]
        self.assertEqual(block["reason_code"],
                         "BUDGET.ROLE_ATTEMPTS_EXHAUSTED")
        self.assertEqual(block["route_to"], "stop")
        self.assertEqual(block["budget_exhaustions"][0]["used"], 3)

    def test_task_agent_run_budget_blocks_redispatch(self):
        artifacts, runs = self.gold_current()
        runs.append({
            "run_id": "design-failed", "role": "task_designer",
            "status": "completed", "decision": "failed",
            "created_at": "2026-08-23T00:01:00+00:00",
            "input_artifacts": {}, "output_artifacts": {},
        })
        self.workspace("task", artifacts, runs)
        policy = self.policy(max_agent_runs_per_task=1)
        plan = plan_next(self.root, slots=2, policy_path=policy)
        self.assertEqual(plan["wave"], [])
        self.assertEqual(plan["blocked"][0]["reason_code"],
                         "BUDGET.TASK_AGENT_RUNS_EXHAUSTED")

    def test_t10_attempt_budget_blocks_more_human_work(self):
        runs = [{
            "run_id": "gold-failed-%d" % number,
            "role": "gold_curator", "status": "completed",
            "decision": "failed", "created_at": "2026-08-23T00:00:00+00:00",
            "input_artifacts": {}, "output_artifacts": {},
        } for number in range(2)]
        self.workspace("task", runs=runs)
        plan = plan_next(self.root, slots=2)
        self.assertEqual(plan["awaiting_human"], [])
        self.assertEqual(plan["blocked"][0]["reason_code"],
                         "BUDGET.ROLE_ATTEMPTS_EXHAUSTED")

    def test_rejects_zero_slots(self):
        with self.assertRaises(PlanError):
            plan_next(self.root, slots=0)


if __name__ == "__main__":
    unittest.main()

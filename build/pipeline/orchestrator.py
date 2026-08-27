"""Evidence-bearing orchestration for the GDPval multi-agent pipeline.

This module does not call an LLM. It creates a sealed input packet for a fresh
agent run, registers only declared outputs, invalidates downstream evidence when
an upstream artifact changes, and runs deterministic script gates. The caller is
responsible for launching each agent with only its run directory mounted.

CLI examples are available through ``python3 pipeline/orchestrator.py --help``.
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


HERE = Path(__file__).resolve().parent
# The role contracts and the tunable policy are single-sourced in 产线规范/.
# Keeping a second copy under pipeline/ is the failure this project has already
# paid for once: two descriptions of the same fact, only one of them updated.
SPEC_DIR = HERE.parent.parent / "产线规范"
DEFAULT_CONTRACTS = SPEC_DIR / "agent_roles.json"
DEFAULT_POLICY = SPEC_DIR / "policy.json"
STATE_FILE = "workflow.json"
DECISIONS = {"passed", "failed", "rework"}
PRODUCTION_ROLES = ["gold_curator", "task_designer", "prompt_author", "solver",
                    "verifier", "rubric"]
GATE_REQUIREMENTS = {"validation": PRODUCTION_ROLES}
GATE_INPUTS = {
    "validation": ["references", "prompt", "gold", "lineage_draft",
                   "expected_values", "verifier_report", "rubric"],
    "release": ["references", "prompt", "gold", "gold_provenance", "lineage_draft",
                "solver_report", "verifier_report", "expected_values", "rubric",
                "human_review_record", "validation_evidence"],
}
GATE_OUTPUT = {"validation": "validation_evidence"}
ROLE_POLICY_SECTIONS = {
    "gold_curator": ("gold_source",),
    "task_designer": ("coverage", "reference_files"),
    "prompt_author": ("language",),
    "solver": ("gates",),
    "verifier": (),
    "rubric": ("rubric",),
}


class PipelineError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sample_rubric_item_count(rng=None, policy=None):
    """Sample the rubric size from the client contract.

    The draw is N(mean, stddev), rounded to the nearest integer and lower
    truncated at ``lower_bound``.  ``rng`` is injectable so a run can record a
    deterministic seed/draw in its evaluator evidence without changing the
    production rule.
    """
    rules = ((policy or {}).get("rubric") or {})
    dist = rules.get("item_count_distribution") or {}
    mean = float(dist.get("mean", 30))
    stddev = float(dist.get("stddev", 10))
    lower = int(dist.get("lower_bound", 25))
    if stddev < 0 or not math.isfinite(mean) or not math.isfinite(stddev):
        raise PipelineError("rubric item-count distribution is invalid")
    draw = (rng or random).gauss(mean, stddev)
    rounding = dist.get("rounding", "nearest_integer")
    if rounding != "nearest_integer":
        raise PipelineError("unsupported rubric item-count rounding: %s" % rounding)
    return max(lower, math.floor(draw + 0.5))


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid4().hex)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(str(tmp), str(path))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _policy_scope_digest(policy, role):
    sections = ROLE_POLICY_SECTIONS.get(role, ())
    scoped = {name: policy.get(name) for name in sections}
    encoded = json.dumps(scoped, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bundle_manifest(root):
    root = Path(root)
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        entries.append({"path": rel, "sha256": _sha256(path),
                        "bytes": path.stat().st_size})
    if not entries:
        raise PipelineError("artifact bundle is empty: %s" % root)
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), entries


def _copy_sources(sources, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    used = set()
    for raw in sources:
        source = Path(raw).resolve()
        if not source.exists():
            raise PipelineError("artifact source does not exist: %s" % source)
        if source.is_dir():
            children = sorted(source.iterdir())
            if not children:
                raise PipelineError("artifact source directory is empty: %s" % source)
            for child in children:
                if child.name in used:
                    raise PipelineError("artifact sources collide at: %s" % child.name)
                used.add(child.name)
                target = destination / child.name
                if child.is_dir():
                    shutil.copytree(str(child), str(target))
                else:
                    shutil.copy2(str(child), str(target))
        else:
            name = source.name
            if name in used:
                raise PipelineError("two artifact sources have the same basename: %s" % name)
            used.add(name)
            target = destination / name
            shutil.copy2(str(source), str(target))


def _validate_name(value, label):
    agent_instance = (label == "agent_id" and value.startswith("A-") and
                      len(value) >= 5 and value[2:].isdigit())
    conventional = (value and value not in {".", ".."} and not any(c not in
                    "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in value))
    if not agent_instance and not conventional:
        raise PipelineError("invalid %s %r; use lowercase letters, digits, _ or -" %
                            (label, value))


class Pipeline:
    """The orchestration interface used by the CLI and tests."""

    def __init__(self, workspace, contracts_path=DEFAULT_CONTRACTS,
                 policy_path=DEFAULT_POLICY):
        self.root = Path(workspace).resolve()
        self.state_path = self.root / STATE_FILE
        self.contracts_path = Path(contracts_path).resolve()
        self.policy_path = Path(policy_path).resolve()
        contracts = _read_json(contracts_path)
        if contracts.get("schema_version") != "1.0":
            raise PipelineError("unsupported role-contract schema")
        self.roles = contracts["roles"]
        self.model_tiers = contracts.get("model_tiers", {})
        self.contracts_digest = _sha256(self.contracts_path)
        # Thresholds live in policy.json because the client has changed their
        # mind on several of them. A threshold compiled into this file is a
        # threshold that gets edited in a hurry and then disagrees with the
        # document that is supposed to define it.
        self.policy = _read_json(policy_path) if Path(policy_path).is_file() else {}
        self.policy_digest = (_sha256(self.policy_path) if self.policy_path.is_file()
                              else hashlib.sha256(b"{}").hexdigest())

    def _role_policy_digest(self, role):
        return _policy_scope_digest(self.policy, role)

    def _run_policy_is_current(self, run):
        scoped = run.get("policy_scope_digest")
        if scoped is not None:
            return scoped == self._role_policy_digest(run["role"])
        # Legacy runs fail closed until migrate-policy-scope verifies the old
        # and current policies differ only in explicitly declared sections.
        return run.get("policy_digest") in (None, self.policy_digest)

    @classmethod
    def initialise(cls, workspace, task_id, contracts_path=DEFAULT_CONTRACTS):
        try:
            canonical = str(UUID(task_id))
        except ValueError as exc:
            raise PipelineError("task_id must be a UUID") from exc
        root = Path(workspace).resolve()
        root.mkdir(parents=True, exist_ok=True)
        state_path = root / STATE_FILE
        if state_path.exists():
            raise PipelineError("workflow already exists: %s" % state_path)
        for directory in ("_artifacts", "runs", "gates"):
            (root / directory).mkdir()
        _write_json(state_path, {
            "schema_version": "1.0",
            "task_id": canonical,
            "created_at": _now(),
            "artifacts": {},
            "runs": [],
            "gates": {},
            "human_review": None,
            "invalidated_roles": {},
        })
        return cls(root, contracts_path)

    def _load(self):
        if not self.state_path.is_file():
            raise PipelineError("workflow is not initialised: %s" % self.root)
        return _read_json(self.state_path)

    def _save(self, state):
        _write_json(self.state_path, state)

    def add_artifact(self, category, sources, produced_by="intake"):
        _validate_name(category, "artifact category")
        state = self._load()
        tmp = self.root / "_artifacts" / (".tmp-" + uuid4().hex)
        try:
            _copy_sources(sources, tmp)
            digest, files = _bundle_manifest(tmp)
            final = self.root / "_artifacts" / category / digest
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                shutil.rmtree(str(tmp))
            else:
                os.replace(str(tmp), str(final))
        except Exception:
            if tmp.exists():
                shutil.rmtree(str(tmp))
            raise
        state["artifacts"][category] = {
            "digest": digest,
            "path": final.relative_to(self.root).as_posix(),
            "files": files,
            "produced_by": produced_by,
            "registered_at": _now(),
        }
        self._save(state)
        return state["artifacts"][category]

    def _run_is_current(self, state, run):
        if run.get("decision") != "passed":
            return False
        if run.get("role") in (state.get("invalidated_roles") or {}):
            return False
        # Runs written before configuration binding remain readable. Every new
        # run carries both digests and becomes stale if either source changes.
        if not self._run_policy_is_current(run):
            return False
        if ("contracts_digest" in run and
                run["contracts_digest"] != self.contracts_digest):
            return False
        preflight = run.get("preflight_evidence")
        if preflight:
            evidence = self.root / preflight.get("path", "")
            if (preflight.get("passed") is not True or not evidence.is_file() or
                    _sha256(evidence) != preflight.get("sha256")):
                return False
        for category, digest in run.get("input_artifacts", {}).items():
            current = state["artifacts"].get(category)
            if not current or current["digest"] != digest:
                return False
        for category, digest in run.get("output_artifacts", {}).items():
            current = state["artifacts"].get(category)
            if (not current or current["digest"] != digest or
                    current.get("produced_by") != run["run_id"]):
                return False
        contract = self.roles.get(run["role"], {})
        for required_role in contract.get("requires_roles", []):
            if not self._current_run(state, required_role):
                return False
        return True

    def _current_run(self, state, role):
        for run in reversed(state["runs"]):
            if run["role"] == role and self._run_is_current(state, run):
                return run
        return None

    def _identity_run(self, state, role):
        """Return a live identity claim, including a concurrent prepared run."""
        for run in reversed(state["runs"]):
            if run["role"] != role:
                continue
            if run.get("status") == "prepared" or self._run_is_current(state, run):
                return run
        return None

    def _gate_is_current(self, state, gate):
        record = state["gates"].get(gate)
        if not record or record.get("status") != "passed":
            return False
        inputs_current = all(
            state["artifacts"].get(category, {}).get("digest") == digest
            for category, digest in record.get("input_artifacts", {}).items())
        outputs_current = all(
            state["artifacts"].get(category, {}).get("digest") == digest and
            state["artifacts"].get(category, {}).get("produced_by") ==
            "gate:" + record["gate_id"]
            for category, digest in record.get("output_artifacts", {}).items())
        return inputs_current and outputs_current

    def _attempt_budget(self, state, role):
        execution = self.policy.get("execution") or {}
        limits = (execution.get("max_attempts_by_role") or
                  execution.get("max_attempts_per_role") or {})
        limit = limits.get(role, execution.get("max_attempts_default", 3))
        used = sum(1 for run in state["runs"] if run["role"] == role)
        return used, limit

    def prepare(self, role, agent_id, context_id, include_inputs=None,
                override_budget=False):
        if role not in self.roles:
            raise PipelineError("unknown role: %s" % role)
        _validate_name(agent_id, "agent_id")
        _validate_name(context_id, "context_id")
        state = self._load()
        contract = self.roles[role]
        used, limit = self._attempt_budget(state, role)
        if limit is not None and used >= limit and not override_budget:
            raise PipelineError(
                "role %s exhausted its attempt budget (%d/%d); diagnose the "
                "upstream cause or pass --override-budget to record an explicit "
                "human override" % (role, used, limit))
        execution = self.policy.get("execution") or {}
        total_limit = (execution.get("max_total_agent_runs_per_task") or
                       execution.get("max_agent_runs_per_task"))
        agent_runs = sum(1 for run in state["runs"]
                         if self.roles[run["role"]].get("kind") != "human_registration")
        if (contract.get("kind") != "human_registration" and
                total_limit is not None and agent_runs >= total_limit and
                not override_budget):
            raise PipelineError(
                "task exhausted its agent-run budget (%d/%d); pass "
                "--override-budget only after recording the diagnosis"
                % (agent_runs, total_limit))
        missing_roles = [name for name in contract.get("requires_roles", [])
                         if not self._current_run(state, name)]
        if missing_roles:
            raise PipelineError("role %s requires current roles: %s" %
                                (role, ", ".join(missing_roles)))
        missing_gates = [name for name in contract.get("requires_gates", [])
                         if not self._gate_is_current(state, name)]
        if missing_gates:
            raise PipelineError("role %s requires passed gates: %s" %
                                (role, ", ".join(missing_gates)))
        missing = [name for name in contract["required_inputs"]
                   if name not in state["artifacts"]]
        if missing:
            raise PipelineError("role %s is missing inputs: %s" %
                                (role, ", ".join(missing)))

        # A cold-context role cannot be re-run in a context that already saw the
        # answer. distinct_from keeps roles apart from each other; it says
        # nothing about a second attempt at the same role, and a solver retried
        # after a prompt fix is exactly where that matters — the second run
        # would "independently" reach the conclusion it already knows.
        if contract.get("kind") == "independent_judgment":
            for run in state["runs"]:
                if run["role"] != role:
                    continue
                if run["agent_id"] == agent_id or run["context_id"] == context_id:
                    raise PipelineError(
                        "%s is a cold-context role: a re-run needs an agent and a "
                        "context that have not attempted it before (%s / %s already "
                        "did)" % (role, run["agent_id"], run["context_id"]))

        for other_role in contract.get("distinct_from", []):
            other = self._identity_run(state, other_role)
            if not other:
                continue
            if other["agent_id"] == agent_id:
                raise PipelineError("%s must use an agent distinct from %s" %
                                    (role, other_role))
            if other["context_id"] == context_id:
                raise PipelineError("%s must use a context distinct from %s" %
                                    (role, other_role))

        run_id = str(uuid4())
        run_root = self.root / "runs" / run_id
        input_root = run_root / "input"
        output_root = run_root / "output"
        input_root.mkdir(parents=True)
        output_root.mkdir()
        requested = set(include_inputs or [])
        permitted = set(contract["allowed_inputs"]) | set(contract.get("optional_inputs", []))
        illegal = requested - permitted
        if illegal:
            raise PipelineError("role %s cannot include input(s): %s" %
                                (role, ", ".join(sorted(illegal))))
        selected = set(contract["required_inputs"])
        selected.update(contract.get("default_inputs", []))
        selected.update(requested)
        inputs = {}
        ordered_inputs = list(contract["allowed_inputs"])
        ordered_inputs += [name for name in contract.get("optional_inputs", [])
                           if name not in ordered_inputs]
        for category in ordered_inputs:
            if category not in selected:
                continue
            artifact = state["artifacts"].get(category)
            if not artifact:
                continue
            shutil.copytree(str(self.root / artifact["path"]),
                            str(input_root / category))
            inputs[category] = artifact["digest"]
        for path in input_root.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        for path in sorted((p for p in input_root.rglob("*") if p.is_dir()),
                           key=lambda p: len(p.parts), reverse=True):
            path.chmod(0o555)
        input_root.chmod(0o555)

        visible = sorted(inputs)
        instruction_lines = [
            "# %s" % contract["name"], "",
            "Use only files below `input/`. Write each output below the named",
            "category directory under `output/`.", "",
            "Required outputs: %s." % ", ".join(contract["required_outputs"]), "",
        ]
        instruction_lines += ["- " + item for item in contract["instructions"]]
        (run_root / "agent_instructions.md").write_text(
            "\n".join(instruction_lines) + "\n", encoding="utf-8")
        packet = {
            "schema_version": "1.0", "task_id": state["task_id"],
            "run_id": run_id, "role": role, "agent_id": agent_id,
            "context_id": context_id, "created_at": _now(),
            "execution_mode": ("human" if contract.get("kind") ==
                               "human_registration" else "agent"),
            "model_tier": contract.get("model_tier"),
            "policy_digest": self.policy_digest,
            "policy_scope_digest": self._role_policy_digest(role),
            "policy_scope_sections": list(ROLE_POLICY_SECTIONS.get(role, ())),
            "contracts_digest": self.contracts_digest,
            "visible_categories": visible,
            "input_artifacts": inputs,
            "required_outputs": contract["required_outputs"],
            "allowed_outputs": contract["allowed_outputs"],
            "isolation_note": ("This directory is an evidence packet, not an OS sandbox. "
                               "Launch the agent with only this run directory mounted."),
            "budget_override": bool(override_budget),
        }
        _write_json(run_root / "run_contract.json", packet)
        state["runs"].append(dict(packet, status="prepared", decision=None,
                                  completed_at=None, output_artifacts={}))
        self._save(state)
        return run_root

    def _validate_role_output(self, role, output_root):
        def report(category):
            path = output_root / category / "report.json"
            if not path.is_file():
                raise PipelineError("%s must contain report.json" % category)
            return _read_json(path)

        if role == "gold_curator":
            value = report("gold_provenance")
            accepted = ((self.policy.get("gold_source") or {})
                        .get("accepted_paths") or [])
            if value.get("source_type") not in accepted:
                raise PipelineError(
                    "gold provenance source_type must be one of %s; a generated "
                    "deliverable is not an accepted path" % (accepted or "<policy missing>"))
            if not value.get("production_method"):
                raise PipelineError("gold provenance must state its production method")
            # Understating the reconstruction is the fatal version of this
            # mistake: recording it honestly is what passes review.
            if value.get("is_real_deliverable") is not True:
                raise PipelineError(
                    "gold must be a real work deliverable; set is_real_deliverable "
                    "only when that is true, and route generated gold through the "
                    "policy fallback instead")
            sources = value.get("real_deliverable_files")
            if not isinstance(sources, list) or not sources:
                raise PipelineError(
                    "gold provenance must list every untouched source file in "
                    "real_deliverable_files")
            for entry in sources:
                if not isinstance(entry, dict):
                    raise PipelineError("real_deliverable_files entries must be objects")
                missing = [key for key in ("filename", "source_url", "source_sha256")
                           if not str(entry.get(key) or "").strip()]
                digest = str(entry.get("source_sha256") or "")
                if missing or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise PipelineError(
                        "each real deliverable needs filename, source_url, and a "
                        "lowercase SHA-256; invalid entry: %r" % entry)
        elif role == "task_designer":
            blueprint = report("task_blueprint")
            notes = report("design_notes")
            # The blueprint is read by the prompt author; the design notes are
            # not. Anything that states an answer belongs on the far side of
            # that line — reasoning points name the closing date, the comment
            # count and the correct identifier rendering. Holding both in one
            # artifact leaked twice: once through the deliverable's filename and
            # once through the task summary.
            EVALUATOR_ONLY = ("reasoning_points", "figure_pattern", "guards",
                              "column_maps", "item_codes")
            stray = [key for key in EVALUATOR_ONLY if key in blueprint]
            if stray:
                raise PipelineError(
                    "task_blueprint carries evaluator-only field(s) %s; the prompt "
                    "author reads this file. Move them to design_notes."
                    % ", ".join(stray))
            if not notes.get("reasoning_points"):
                raise PipelineError(
                    "design_notes must record the reasoning points that stop the "
                    "task degenerating into transcription")
            if not blueprint.get("output_contract"):
                raise PipelineError("task blueprint must state an output contract")
            missing = [key for key in ("sector", "occupation") if not blueprint.get(key)]
            if missing:
                raise PipelineError("task blueprint must declare %s; the assembly "
                                    "step reads them from here" % ", ".join(missing))
            spec_path = output_root / "reference_spec" / "spec.json"
            spec = _read_json(spec_path) if spec_path.is_file() else None
            if not isinstance(spec, list) or not spec:
                raise PipelineError("reference_spec/spec.json must be a non-empty array")
            allowed = ((self.policy.get("reference_files") or {})
                       .get("allowed_formats") or [])
            forbidden = ((self.policy.get("reference_files") or {})
                          .get("forbidden_formats") or [])
            for entry in spec:
                name = str(entry.get("filename", ""))
                suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if (not name or suffix in forbidden or
                        (allowed and suffix not in allowed)):
                    raise PipelineError(
                        "reference file %r must carry one of the allowed extensions %s"
                        % (name, allowed))
        elif role == "prompt_author":
            prompt_dir = output_root / "prompt"
            texts = [path.read_text(encoding="utf-8", errors="replace")
                     for path in sorted(prompt_dir.rglob("*")) if path.is_file()]
            body = "\n".join(texts).strip()
            if not body:
                raise PipelineError("prompt is empty")
            # Attachment 1 of the specification rejects a prompt that still
            # carries a placeholder, so the check is a class sweep rather than
            # one banned spelling.
            lowered = body.lower()
            for marker in ("not specified", "tbd", "todo", "<placeholder>",
                           "{{", "xxx", "待补", "占位", "待定"):
                if marker in lowered:
                    raise PipelineError(
                        "prompt still contains the placeholder %r" % marker)
            contract = report("output_contract")
            if not contract.get("files"):
                raise PipelineError("output_contract must name the deliverable files")
        elif role == "solver":
            value = report("solver_report")
            required = {"prompt_self_contained", "solvable", "task_multistep",
                        "separating_power", "difficulty_evidence",
                        "blocking_ambiguities"}
            if not required <= set(value):
                raise PipelineError("solver report is missing required fields")
            if not all(value.get(k) is True for k in
                       ("prompt_self_contained", "solvable", "task_multistep")):
                raise PipelineError("solver gate failed: prompt is not self-contained, "
                                    "solvable and multi-step")
            if value.get("separating_power") != "sufficient":
                raise PipelineError("solver gate failed: separating_power is insufficient")
            if not value.get("difficulty_evidence"):
                raise PipelineError("solver gate requires difficulty evidence")
            # A solver that says "I could read the answer off the prompt" has
            # reported the one thing this role exists to detect. Letting the run
            # pass because the other four fields are true would file the finding
            # and act on none of it.
            leaks = [item for item in value.get("blocking_ambiguities") or []
                     if str(item).strip().upper().startswith("PROMPT LEAK")]
            if leaks:
                raise PipelineError("solver reported prompt leakage: %s"
                                    % "; ".join(str(x)[:160] for x in leaks))
        elif role == "verifier":
            value = report("verifier_report")
            if (value.get("recompute_passed") is not True or
                    value.get("lineage_valid") is not True or
                    value.get("mismatches")):
                raise PipelineError("verifier gate failed")
            # Reconstructibility runs both ways. A demand with nowhere to land
            # in the gold does not merely fail to score — it inverts: the
            # executor who reasons correctly writes a sentence the gold lacks,
            # and the one who never considered it matches. No leakage audit can
            # see this, because leakage audits only ask whether the inputs give
            # too much away.
            if "demands_without_landing_place" not in value:
                raise PipelineError(
                    "verifier_report must answer demands_without_landing_place: "
                    "which prompt requirements have no counterpart in the gold")
            orphans = value.get("demands_without_landing_place") or []
            if orphans:
                raise PipelineError(
                    "%d prompt requirement(s) have nowhere to land in the gold: %s"
                    % (len(orphans), "; ".join(str(x)[:120] for x in orphans)))
        elif role == "rubric":
            path = output_root / "rubric" / "rubric.json"
            items = _read_json(path) if path.is_file() else None
            if not isinstance(items, list) or not items:
                raise PipelineError("rubric/rubric.json must be a non-empty array")
            rules = self.policy.get("rubric") or {}
            # `required` is a semantic hard-gate designation, not a global
            # importance flag. Missing values get the documented default;
            # explicit booleans are preserved so reviewers can mark ordinary
            # quality criteria false.
            required_default = rules.get("required_field_default",
                                         rules.get("required_field_value", True))
            allowed_required = set(rules.get("required_field_allowed") or
                                   (True, False))
            changed = False
            for item in items:
                if "required" not in item:
                    item["required"] = required_default
                    changed = True
            if changed:
                _write_json(path, items)
            wrong_required = [item.get("rubric_item_id") for item in items
                              if item.get("required") not in allowed_required]
            if wrong_required:
                raise PipelineError(
                    "rubric required must be one of %r; wrong on %s"
                    % (sorted(allowed_required, key=repr), wrong_required[:8]))
            total = rules.get("total_score")
            invalid_scores = [item.get("rubric_item_id") for item in items
                              if (isinstance(item.get("score"), bool)
                                  or not isinstance(item.get("score"), int)
                                  or item.get("score") < 0)]
            if invalid_scores:
                raise PipelineError("rubric scores must be non-negative integers; "
                                    "wrong on %s" % invalid_scores[:8])
            if total is not None and sum(item["score"] for item in items) != total:
                raise PipelineError("rubric scores must total %s" % total)
            floor = rules.get("item_count_hard_min")
            if floor is not None and len(items) < floor:
                raise PipelineError("rubric has %d items against a floor of %d"
                                    % (len(items), floor))
            forbidden = tuple(rules.get("forbidden_criterion_terms") or ())
            ids = []
            for item in items:
                criterion = str(item.get("criterion", "")).lower()
                if not criterion:
                    raise PipelineError("rubric contains an empty criterion")
                hit = next((t for t in forbidden if t.lower() in criterion), None)
                if hit:
                    raise PipelineError("rubric criterion scores %r, which the "
                                        "specification excludes" % hit)
                item_id = item.get("rubric_item_id")
                if not item_id:
                    raise PipelineError("every rubric item needs a rubric_item_id")
                ids.append(item_id)
            if len(set(ids)) != len(ids):
                raise PipelineError("rubric_item_id values must be unique")
            # An item with no check must say how a person settles it. "No
            # check" and "passed" are different statements, and the delivery has
            # been rejected once for conflating them.
            unjudgeable = [item.get("rubric_item_id") for item in items
                           if not item.get("check")
                           and not str(item.get("verification") or "").strip()]
            if unjudgeable:
                raise PipelineError(
                    "%d rubric item(s) have neither a check nor a verification "
                    "note" % len(unjudgeable))

    def _gold_preflight(self, run, output_root):
        """Run the fixed T10 technical checks and persist their evidence."""
        import officestrip
        import security_scans

        gold_root = output_root / "gold"
        files = sorted(path for path in gold_root.rglob("*") if path.is_file()) \
            if gold_root.is_dir() else []
        subjects = [{"path": path.relative_to(gold_root).as_posix(),
                     "bytes": path.stat().st_size, "sha256": _sha256(path)}
                    for path in files]
        checks = []

        def add(name, passed, detail, findings=None):
            item = {"check": name, "status": "passed" if passed else "failed",
                    "detail": detail}
            if findings:
                item["findings"] = findings
            checks.append(item)

        empty = [item["path"] for item in subjects if item["bytes"] == 0]
        add("gold_files_nonempty", bool(files) and not empty,
            "%d file(s); %d empty" % (len(files), len(empty)), empty)

        residue, metadata_errors = [], []
        for path in files:
            rel = path.relative_to(gold_root).as_posix()
            try:
                leftovers = officestrip.residue(str(path))
                if leftovers:
                    residue.append({"file": rel, "parts": leftovers})
            except Exception as exc:  # A scanner that cannot run is not green.
                metadata_errors.append({"file": rel,
                                        "error": "%s: %s" %
                                                 (type(exc).__name__, exc)})
        metadata_findings = residue + metadata_errors
        add("office_pdf_metadata", not metadata_findings,
            "%d metadata residue/error finding(s)" % len(metadata_findings),
            metadata_findings)

        declared = [item["path"] for item in subjects]
        text_cache = {}
        for path in files:
            if path.suffix.lower() not in (".docx", ".xlsx", ".xlsm", ".pptx", ".pdf"):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text_cache[os.fspath(path)] = fh.read()

        def scan(name, function):
            try:
                result = function()
                findings = (result.get("hits") or result.get("content_hits") or [])
                findings += result.get("declared_path_violations") or []
                add(name, result.get("passed") is True,
                    "%d finding(s)" % len(findings), findings)
            except Exception as exc:  # Missing parser/corrupt input must fail closed.
                add(name, False, "scanner error: %s: %s" %
                    (type(exc).__name__, exc))

        scan("malicious_content",
             lambda: security_scans.scan_malicious(files, text_cache))
        scan("absolute_or_traversal_paths",
             lambda: security_scans.scan_paths(files, declared, text_cache))
        scan("secrets", lambda: security_scans.scan_secrets(files, text_cache))

        provenance_path = output_root / "gold_provenance" / "report.json"
        rights_findings = []
        try:
            provenance = _read_json(provenance_path)
            for field in ("rights_holder", "license", "usage_scope"):
                if not str(provenance.get(field) or "").strip():
                    rights_findings.append("missing %s" % field)
            blocker_keys = ("open_blockers", "known_blockers_carried_forward",
                            "blockers", "open_issues", "unresolved_issues")
            for field in blocker_keys:
                if provenance.get(field) not in (None, "", [], {}, False):
                    rights_findings.append("%s is not empty" % field)
            rights_text = " ".join(str(provenance.get(field) or "") for field in
                                   ("license", "usage_scope"))
            unresolved = ("no open redistribution", "pending", "unresolved",
                          "not identified", "unclear", "to be confirmed",
                          "未确认", "待确认", "未解决", "阻塞")
            hits = [term for term in unresolved if term.lower() in rights_text.lower()]
            rights_findings.extend("unresolved rights marker %r" % hit for hit in hits)
        except Exception as exc:
            rights_findings.append("unreadable provenance: %s: %s" %
                                   (type(exc).__name__, exc))
        add("provenance_rights", not rights_findings,
            "%d rights/licensing finding(s)" % len(rights_findings), rights_findings)

        report = {
            "schema_version": "1.0", "run_id": run["run_id"],
            "role": "gold_curator", "created_at": _now(),
            "subjects": subjects, "checks": checks,
            "passed": all(item["status"] == "passed" for item in checks),
        }
        evidence_path = self.root / "runs" / run["run_id"] / "t10_preflight.json"
        _write_json(evidence_path, report)
        run["preflight_evidence"] = {
            "path": evidence_path.relative_to(self.root).as_posix(),
            "sha256": _sha256(evidence_path), "passed": report["passed"],
        }
        return report

    def _verify_run_inputs(self, state, run):
        if not self._run_policy_is_current(run):
            raise PipelineError("policy changed after preparation; prepare a new run")
        if ("contracts_digest" in run and
                run["contracts_digest"] != self.contracts_digest):
            raise PipelineError("role contracts changed after preparation; prepare a new run")
        input_root = self.root / "runs" / run["run_id"] / "input"
        for category, expected in run.get("input_artifacts", {}).items():
            path = input_root / category
            if not path.is_dir():
                raise PipelineError("run input disappeared: %s" % category)
            actual, _files = _bundle_manifest(path)
            if actual != expected:
                raise PipelineError(
                    "run input %s was modified after preparation; discard this run"
                    % category)
            current = state["artifacts"].get(category)
            if not current or current["digest"] != expected:
                raise PipelineError(
                    "run input %s is stale; prepare a new run against current artifacts"
                    % category)

    def _invalidate_from_route(self, state, source_run, reason_code, route_to):
        """Make a declared rework route affect planning, not just audit text."""
        if route_to == "stop":
            return []
        affected = {route_to}
        changed = True
        while changed:
            changed = False
            for role, contract in self.roles.items():
                if role not in affected and any(parent in affected for parent in
                                                contract.get("requires_roles", [])):
                    affected.add(role)
                    changed = True
        invalidated = state.setdefault("invalidated_roles", {})
        for role in affected:
            invalidated[role] = {
                "reason_code": reason_code,
                "source_run_id": source_run["run_id"],
                "route_to": route_to,
                "invalidated_at": _now(),
            }
        return sorted(affected)

    def submit(self, run_id, decision, reason_code=None):
        if decision not in DECISIONS:
            raise PipelineError("decision must be one of %s" % sorted(DECISIONS))
        state = self._load()
        run = next((r for r in state["runs"] if r["run_id"] == run_id), None)
        if not run:
            raise PipelineError("unknown run_id: %s" % run_id)
        if run["status"] != "prepared":
            raise PipelineError("run has already been submitted")
        contract = self.roles[run["role"]]
        routes = contract.get("failure_routes") or {}
        route_codes = ({item.get("reason_code") for item in routes}
                       if isinstance(routes, list) else set(routes))
        if decision != "passed":
            if not reason_code:
                raise PipelineError("failed/rework submissions require --reason-code")
            if route_codes and reason_code not in route_codes:
                raise PipelineError("unknown reason_code %r for %s; expected one of %s" %
                                    (reason_code, run["role"], sorted(route_codes)))
        elif reason_code:
            raise PipelineError("passed submissions cannot carry a failure reason")
        self._verify_run_inputs(state, run)
        output_root = self.root / "runs" / run_id / "output"
        present = sorted(p.name for p in output_root.iterdir() if p.is_dir())
        unexpected = sorted(set(present) - set(contract["allowed_outputs"]))
        missing = sorted(set(contract["required_outputs"]) - set(present))
        if decision == "passed" and (unexpected or missing):
            raise PipelineError("output categories invalid; missing=%s unexpected=%s" %
                                (missing, unexpected))
        outputs = {}
        if decision == "passed":
            if run["role"] == "gold_curator":
                report = self._gold_preflight(run, output_root)
                self._save(state)
                if not report["passed"]:
                    failed = [item["check"] for item in report["checks"]
                              if item["status"] != "passed"]
                    raise PipelineError("T10 technical preflight failed: %s" %
                                        ", ".join(failed))
            self._validate_role_output(run["role"], output_root)
            for category in contract["required_outputs"]:
                artifact = self.add_artifact(category, [output_root / category], run_id)
                outputs[category] = artifact["digest"]
            state = self._load()
            run = next(r for r in state["runs"] if r["run_id"] == run_id)
        run["status"] = "completed"
        run["decision"] = decision
        run["reason_code"] = reason_code
        if decision != "passed":
            route = next((item for item in routes
                          if item.get("reason_code") == reason_code), None)
            route_to = route.get("route_to") if route else "stop"
            run["route_to"] = route_to
            run["invalidated_roles"] = self._invalidate_from_route(
                state, run, reason_code, route_to)
        else:
            # A successful rerun repairs only its own state. Descendants stay
            # stale until their own output is rebuilt against this new input.
            state.get("invalidated_roles", {}).pop(run["role"], None)
        run["completed_at"] = _now()
        run["output_artifacts"] = outputs
        self._save(state)
        return run

    def record_validation(self, delivery_root):
        """Register strict validator evidence; callers cannot assert a pass."""
        state = self._load()
        missing = [role for role in GATE_REQUIREMENTS["validation"]
                   if not self._current_run(state, role)]
        if missing:
            raise PipelineError("validation requires current roles: %s" %
                                ", ".join(missing))
        delivery = Path(delivery_root).resolve()
        status_path = delivery / "manifests" / "validation_status.jsonl"
        if not status_path.is_file():
            raise PipelineError("validation status is missing: %s" % status_path)
        records = [json.loads(line) for line in
                   status_path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        record = next((item for item in records
                       if item.get("task_id") == state["task_id"]), None)
        if not record or not record.get("checks"):
            raise PipelineError("no validation record for task %s" % state["task_id"])
        blocking = [item for item in record["checks"]
                    if item.get("status") != "passed"]
        if blocking:
            summary = ", ".join("%s=%s" % (item.get("check"), item.get("status"))
                                for item in blocking[:8])
            raise PipelineError("strict validation is not green: %s" % summary)
        evidence = delivery / "validation_evidence" / state["task_id"]
        if not evidence.is_dir() or not any(p.is_file() for p in evidence.rglob("*")):
            raise PipelineError("validation evidence is empty: %s" % evidence)
        gate_id = str(uuid4())
        gate_root = self.root / "gates" / "validation" / gate_id
        gate_root.mkdir(parents=True)
        (gate_root / "validation_status.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        input_artifacts = {name: state["artifacts"][name]["digest"]
                           for name in GATE_INPUTS["validation"]
                           if name in state["artifacts"]}
        artifact = self.add_artifact("validation_evidence", [evidence],
                                     "gate:" + gate_id)
        state = self._load()
        gate_record = {
            "gate_id": gate_id, "status": "passed", "run_at": _now(),
            "source": str(status_path),
            "input_artifacts": input_artifacts,
            "output_artifacts": {"validation_evidence": artifact["digest"]},
            "logs": gate_root.relative_to(self.root).as_posix(),
        }
        state["gates"]["validation"] = gate_record
        self._save(state)
        return gate_record

    def _validate_human_review(self, record, base):
        if record.get("task_id") != self._load()["task_id"]:
            raise PipelineError("human review task_id does not match workflow")
        layers = record.get("layers") or []
        rules = self.policy.get("human_review") or {}
        expected = rules.get("layers") or [
            "general_review", "occupational_expert_review", "final_review"]
        if [layer.get("layer") for layer in layers] != expected:
            raise PipelineError("human review must contain the three layers in order")
        names = [layer.get("reviewer_id") for layer in layers]
        required_signers = rules.get("distinct_signatories_required", 3)
        if (any(not name for name in names) or
                len(set(names)) != required_signers):
            raise PipelineError("the three human review layers need distinct reviewers")
        times = []
        for layer in layers:
            if layer.get("status") != "passed" or not layer.get("opinion"):
                raise PipelineError("each human review layer needs a passed opinion")
            try:
                stamp = layer["reviewed_at"].replace("Z", "+00:00")
                parsed = datetime.fromisoformat(stamp)
            except (KeyError, ValueError) as exc:
                raise PipelineError("human review timestamps must be ISO-8601") from exc
            if parsed.utcoffset() is None:
                raise PipelineError("human review timestamps must include a timezone")
            times.append(parsed)
            evidence = layer.get("evidence_files") or []
            if not evidence:
                raise PipelineError("each human review layer needs evidence files")
            for rel in evidence:
                if Path(rel).is_absolute() or ".." in Path(rel).parts:
                    raise PipelineError("human review evidence paths must be relative")
                if not (base / rel).is_file() or (base / rel).stat().st_size == 0:
                    raise PipelineError("missing human review evidence: %s" % rel)
        # General and occupational review may run in parallel. Final review is
        # a closure check, so it must occur strictly after both; equality is not
        # a time gap and must not pass merely because sorting is stable.
        if times[2] <= max(times[:2]):
            raise PipelineError(
                "final review must be strictly later than both earlier layers")
        expert = layers[1]
        credentials = expert.get("qualification_evidence_files") or []
        if rules.get("credential_evidence_in_package_required") and not credentials:
            raise PipelineError("occupational expert qualification evidence is required")
        if (not credentials and
                expert.get("credential_status") !=
                rules.get("credential_status_when_evidence_absent", "not_supplied")):
            raise PipelineError(
                "occupational expert without qualification evidence must use "
                "credential_status=not_supplied")
        for rel in credentials:
            if Path(rel).is_absolute() or ".." in Path(rel).parts or not (base / rel).is_file():
                raise PipelineError("invalid occupational qualification evidence: %s" % rel)
        if rules.get("expert_rejection_required"):
            objections = expert.get("substantive_objections") or []
            if not objections:
                raise PipelineError("occupational review needs a substantive objection record")
        if not expert.get("rubric_version_reviewed"):
            raise PipelineError("occupational review must bind to rubric_version_reviewed")
        if not (expert.get("adoption_actions") or []):
            raise PipelineError("occupational review must record adoption_actions")
        final = layers[2]
        if final.get("open_findings") not in (None, []):
            raise PipelineError("final review still has open findings")
        if rules.get("closure_evidence_required"):
            expected_findings = {finding_id for layer in layers[:2]
                                 for finding_id in (layer.get("finding_ids") or [])}
            dispositions = final.get("finding_dispositions") or []
            recorded = {item.get("finding_id") for item in dispositions
                        if item.get("finding_id")}
            if not expected_findings or recorded != expected_findings:
                raise PipelineError(
                    "final review must dispose every earlier finding exactly once; "
                    "expected %s, recorded %s"
                    % (sorted(expected_findings), sorted(recorded)))
            for item in dispositions:
                if item.get("disposition") not in ("closed", "accepted_without_change"):
                    raise PipelineError("final review has an unresolved disposition: %s"
                                        % item.get("finding_id"))
                if not item.get("rationale"):
                    raise PipelineError("each finding disposition needs a rationale")
                evidence = item.get("evidence_files") or []
                if not evidence:
                    raise PipelineError("each finding disposition needs evidence files")
                for rel in evidence:
                    if (Path(rel).is_absolute() or ".." in Path(rel).parts or
                            not (base / rel).is_file() or
                            (base / rel).stat().st_size == 0):
                        raise PipelineError("invalid finding closure evidence: %s" % rel)
                try:
                    closed_at = datetime.fromisoformat(
                        item["closed_at"].replace("Z", "+00:00"))
                except (KeyError, ValueError) as exc:
                    raise PipelineError(
                        "finding closure timestamps must be ISO-8601") from exc
                if closed_at.utcoffset() is None:
                    raise PipelineError(
                        "finding closure timestamps must include a timezone")
                if closed_at >= times[2]:
                    raise PipelineError(
                        "each finding must close before final review begins")
        marking = record.get("gold_marking_evidence_files") or []
        if not marking:
            raise PipelineError("human review needs gold_marking_evidence_files")
        for rel in marking:
            if (Path(rel).is_absolute() or ".." in Path(rel).parts or
                    not (base / rel).is_file() or (base / rel).stat().st_size == 0):
                raise PipelineError("invalid gold marking evidence: %s" % rel)
        expected_claim = rules.get("independence_claim")
        if expected_claim and record.get("independence_statement") != expected_claim:
            raise PipelineError("human review independence_statement must match policy")

    def record_human_review(self, record_path):
        state = self._load()
        missing = [role for role in PRODUCTION_ROLES
                   if not self._current_run(state, role)]
        if missing:
            raise PipelineError("human review requires current roles: %s"
                                % ", ".join(missing))
        if not self._gate_is_current(state, "validation"):
            raise PipelineError("human review requires a current validation gate")
        record_path = Path(record_path).resolve()
        record = _read_json(record_path)
        self._validate_human_review(record, record_path.parent)
        sources = [record_path]
        for layer in record["layers"]:
            sources.extend(record_path.parent / p for p in layer["evidence_files"])
            sources.extend(record_path.parent / p
                           for p in layer.get("qualification_evidence_files", []))
        sources.extend(record_path.parent / p
                       for p in record.get("gold_marking_evidence_files", []))
        artifact = self.add_artifact("human_review_record", sources, "human")
        state = self._load()
        state["human_review"] = {
            "status": "passed", "recorded_at": _now(),
            "artifact_digest": artifact["digest"],
            "basis": {name: state["artifacts"][name]["digest"]
                      for name in GATE_INPUTS["release"]
                      if name in state["artifacts"] and name != "human_review_record"},
            "reviewers": [layer["reviewer_id"] for layer in record["layers"]],
        }
        self._save(state)
        return state["human_review"]

    def migrate_policy_scopes(self, previous_policy_path, changed_sections):
        previous_policy_path = Path(previous_policy_path).resolve()
        previous = _read_json(previous_policy_path)
        allowed = set(changed_sections or [])
        actual = {key for key in set(previous) | set(self.policy)
                  if previous.get(key) != self.policy.get(key)}
        if not actual:
            raise PipelineError("previous and current policies are identical")
        if actual - allowed:
            raise PipelineError(
                "policy migration declared %s but also changes %s" %
                (sorted(allowed), sorted(actual - allowed)))
        previous_digest = _sha256(previous_policy_path)
        state = self._load()
        migrated = []
        for run in state["runs"]:
            source_digest = run.get("policy_digest")
            if source_digest == previous_digest:
                source_policy = previous
            elif source_digest == self.policy_digest:
                source_policy = self.policy
            else:
                continue
            role = run["role"]
            run["policy_scope_digest"] = _policy_scope_digest(source_policy, role)
            run["policy_scope_sections"] = list(
                ROLE_POLICY_SECTIONS.get(role, ()))
            run["policy_scope_migration"] = {
                "migrated_at": _now(),
                "previous_policy_digest": previous_digest,
                "current_policy_digest": self.policy_digest,
                "verified_changed_sections": sorted(actual),
            }
            contract_path = self.root / "runs" / run["run_id"] / "run_contract.json"
            if contract_path.is_file():
                contract = _read_json(contract_path)
                contract["policy_scope_digest"] = run["policy_scope_digest"]
                contract["policy_scope_sections"] = run["policy_scope_sections"]
                contract["policy_scope_migration"] = run["policy_scope_migration"]
                _write_json(contract_path, contract)
            migrated.append(run["run_id"])
        if not migrated:
            raise PipelineError(
                "no runs match previous or current policy digest")
        state.setdefault("policy_scope_migrations", []).append({
            "migrated_at": _now(),
            "previous_policy_digest": previous_digest,
            "current_policy_digest": self.policy_digest,
            "verified_changed_sections": sorted(actual),
            "run_ids": migrated,
        })
        self._save(state)
        return {"migrated_runs": len(migrated),
                "changed_sections": sorted(actual)}

    def _human_review_is_current(self, state):
        review = state.get("human_review")
        if not review or review.get("status") != "passed":
            return False
        current = state["artifacts"].get("human_review_record", {}).get("digest")
        if current != review.get("artifact_digest"):
            return False
        return all(state["artifacts"].get(name, {}).get("digest") == digest
                   for name, digest in review.get("basis", {}).items())

    def status(self):
        state = self._load()
        roles = {}
        for role in self.roles:
            current = self._current_run(state, role)
            attempted = any(r["role"] == role for r in state["runs"])
            roles[role] = "current" if current else ("stale_or_failed" if attempted else "missing")
        gates = {name: ("current" if self._gate_is_current(state, name)
                        else ("stale_or_failed" if name in state["gates"] else "missing"))
                 for name in GATE_REQUIREMENTS}
        # Readiness is defined by what the release gate requires, not by every
        # role in the contract file: the batch-layer roles run in the batch
        # workspace and their outputs arrive here as intake artifacts.
        all_roles = all(roles.get(name) == "current" for name in PRODUCTION_ROLES)
        checks = gates["validation"] == "current"
        human = "current" if self._human_review_is_current(state) else "missing_or_stale"
        return {
            "task_id": state["task_id"], "roles": roles, "gates": gates,
            "human_review": human,
            "release_ready": all_roles and checks and human == "current",
        }


def _parse_artifact(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("use CATEGORY=PATH")
    category, path = value.split("=", 1)
    return category, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="initialise an evidence workspace")
    p.add_argument("workspace")
    p.add_argument("task_id")
    p = sub.add_parser("add-artifact", help="register or replace an artifact category")
    p.add_argument("workspace")
    p.add_argument("artifact", type=_parse_artifact, nargs="+")
    p = sub.add_parser("prepare", help="create a role-specific isolated input packet")
    p.add_argument("workspace")
    p.add_argument("role", choices=sorted(_read_json(DEFAULT_CONTRACTS)["roles"]))
    p.add_argument("--agent-id", required=True)
    p.add_argument("--context-id", required=True)
    p.add_argument("--include", action="append", default=[], dest="include_inputs",
                   help="opt in an allowed non-required input category")
    p.add_argument("--override-budget", action="store_true",
                   help="record a human override of the role/task retry budget")
    p = sub.add_parser("submit", help="validate and register an agent run")
    p.add_argument("workspace")
    p.add_argument("run_id")
    p.add_argument("decision", choices=sorted(DECISIONS))
    p.add_argument("--reason-code")
    p = sub.add_parser("record-validation",
                       help="register a strict validate.py result from a delivery tree")
    p.add_argument("workspace")
    p.add_argument("delivery")
    p = sub.add_parser("record-human-review", help="register three independent human layers")
    p.add_argument("workspace")
    p.add_argument("record")
    p = sub.add_parser(
        "migrate-policy-scope",
        help="bind legacy runs to role-scoped policy after a verified policy change")
    p.add_argument("workspace")
    p.add_argument("previous_policy")
    p.add_argument("--changed-section", action="append", required=True,
                   dest="changed_sections")
    p = sub.add_parser("status", help="derive current readiness from evidence")
    p.add_argument("workspace")
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            Pipeline.initialise(args.workspace, args.task_id)
            result = {"workspace": str(Path(args.workspace).resolve())}
        else:
            pipeline = Pipeline(args.workspace)
            if args.command == "add-artifact":
                result = {category: pipeline.add_artifact(category, [path])
                          for category, path in args.artifact}
            elif args.command == "prepare":
                result = {"run_directory": str(pipeline.prepare(
                    args.role, args.agent_id, args.context_id,
                    args.include_inputs, args.override_budget))}
            elif args.command == "submit":
                result = pipeline.submit(args.run_id, args.decision, args.reason_code)
            elif args.command == "record-validation":
                result = pipeline.record_validation(args.delivery)
            elif args.command == "record-human-review":
                result = pipeline.record_human_review(args.record)
            elif args.command == "migrate-policy-scope":
                result = pipeline.migrate_policy_scopes(
                    args.previous_policy, args.changed_sections)
            else:
                result = pipeline.status()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print("pipeline error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

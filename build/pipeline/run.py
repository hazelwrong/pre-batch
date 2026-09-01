"""Build and check every assembled task.

    python3 pipeline/run.py [task_id ...]

Three phases, in order:

  1. S-REF — regenerate each task's reference files from its `reference_spec.json`,
     where one exists. Tasks whose inputs were carried across from an accepted
     package have no spec and are left alone.
  2. build_delivery — assemble the delivery root from the task data.
    3. validate — run the full check once per task against the combined delivery.
     A failed or not-run check never produces a release archive.
  3. H-REG — after validation, explicitly register each task's already-completed
     human-review record so it is bound to the new validation digest.

What this no longer does is generate gold. The generators that used to run here
are in `pipeline/archive/`, because the client's requirement is that a
deliverable be a real work product — a step that quietly manufactures one is the
single fastest way to fail acceptance.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import build_references as REF                                    # noqa: E402
import taskdata as TD                                             # noqa: E402
from orchestrator import Pipeline, PipelineError                  # noqa: E402


def tasks_to_build(argv):
    if argv:
        return argv
    explicit = os.environ.get("GDPVAL_TASK_ID")
    if explicit:
        return [t.strip() for t in explicit.split(",") if t.strip()]
    return sorted(name for name in os.listdir(TD.TASKS_ROOT)
                  if os.path.isdir(os.path.join(TD.TASKS_ROOT, name)))


def staging_dir(task_id, kind):
    scoped = os.path.join(BASE, "staging", task_id)
    root = scoped if os.path.isdir(scoped) else os.path.join(BASE, "staging")
    return os.path.join(root, kind)


def build_references(task_ids):
    policy = TD.policy()
    for task_id in task_ids:
        spec_path = os.path.join(TD.TASKS_ROOT, task_id, "reference_spec.json")
        if not os.path.isfile(spec_path):
            print("  %s — no reference_spec.json; leaving staged inputs alone"
                  % task_id)
            continue
        with open(spec_path, encoding="utf-8") as fh:
            spec = json.load(fh)
        out = os.path.join(BASE, "staging", task_id, "reference_files")
        written = REF.build(spec, out, policy)
        print("  %s — %d reference file(s) built" % (task_id, len(written)))


def step(label, args, env=None):
    print("\n[%s]" % label)
    proc = subprocess.run(args, cwd=BASE, env=env)
    return proc.returncode


def record_and_check_workflows(task_ids, delivery):
    root = os.environ.get("GDPVAL_WORKBENCH_ROOT", os.path.join(BASE, "workbench"))
    review_root = os.environ.get("GDPVAL_HUMAN_REVIEW_ROOT")
    review_map = {}
    raw_map = os.environ.get("GDPVAL_HUMAN_REVIEW_RECORDS")
    if raw_map:
        try:
            review_map = json.loads(raw_map)
        except ValueError as exc:
            return ["GDPVAL_HUMAN_REVIEW_RECORDS is not valid JSON: %s" % exc]
        if not isinstance(review_map, dict):
            return ["GDPVAL_HUMAN_REVIEW_RECORDS must be a JSON object"]
    problems = []
    for task_id in task_ids:
        workspace = os.path.join(root, task_id)
        if not os.path.isfile(os.path.join(workspace, "workflow.json")):
            problems.append("%s has no workflow" % task_id)
            continue
        pipeline = Pipeline(workspace)
        try:
            pipeline.record_validation(delivery, "final")
        except PipelineError as exc:
            problems.append("%s validation evidence: %s" % (task_id, exc))
            continue
        state = pipeline._load()
        if state.get("review_cycle"):
            try:
                pipeline.record_human_review()
            except PipelineError as exc:
                problems.append("%s H-REG: %s" % (task_id, exc))
                continue
            if not pipeline.status()["release_ready"]:
                problems.append("%s workflow is not release-ready" % task_id)
            continue
        review_path = review_map.get(task_id)
        if not review_path and review_root:
            review_path = os.path.join(review_root, task_id,
                                       "human_review_record.json")
        if not review_path:
            problems.append(
                "%s H-REG: provide GDPVAL_HUMAN_REVIEW_ROOT or "
                "GDPVAL_HUMAN_REVIEW_RECORDS after final validation" % task_id)
            continue
        if not os.path.isfile(review_path):
            problems.append("%s H-REG record is missing: %s" %
                            (task_id, review_path))
            continue
        try:
            pipeline.record_human_review(review_path)
        except PipelineError as exc:
            problems.append("%s H-REG: %s" % (task_id, exc))
            continue
        if not pipeline.status()["release_ready"]:
            problems.append("%s workflow is not release-ready" % task_id)
    return problems


def main(argv):
    task_ids = tasks_to_build(argv)
    if not task_ids:
        sys.exit("no assembled tasks under %s" % TD.TASKS_ROOT)

    print("[1/3] reference files")
    build_references(task_ids)

    env = dict(os.environ, GDPVAL_TASK_ID=",".join(task_ids))
    if step("2/3 assembling delivery tree",
            [sys.executable, os.path.join("pipeline", "build_delivery.py")], env):
        sys.exit("pipeline halted while assembling the delivery tree")

    delivery = os.environ.get("GDPVAL_DELIVERY", os.path.join(BASE, "delivery"))
    codes = []
    for index, task_id in enumerate(task_ids, 1):
        code = step("3/3 full check %d/%d — %s" %
                    (index, len(task_ids), task_id),
                    [sys.executable, os.path.join("pipeline", "validate.py")],
                    dict(env, GDPVAL_DELIVERY=delivery,
                         GDPVAL_VALIDATE_TASK_ID=task_id))
        codes.append(code)
    code = 0 if all(value == 0 for value in codes) else 1

    # A failed validation is a gate, not a warning. Keep the unpacked evidence
    # for diagnosis; never package it.
    archive_to = os.environ.get("GDPVAL_ARCHIVE")
    if code == 0 and archive_to:
        code = step(
            "release hygiene and declared-path audit",
            [sys.executable, os.path.join("pipeline", "audit_remediated_delivery.py"),
             delivery],
            env,
        )
    if code == 0 and archive_to:
        workflow_problems = record_and_check_workflows(task_ids, delivery)
        if workflow_problems:
            code = 1
            print("\n[archive skipped] " + "; ".join(workflow_problems))
    if code == 0 and archive_to:
        sys.path.insert(0, os.path.join(BASE, "pipeline"))
        import package as PKG
        PKG.normalise_tree_mtimes(delivery)
        with tempfile.TemporaryDirectory(prefix="gdpval-package-check-") as tmp:
            first = PKG.write_archive(delivery, os.path.join(tmp, "first.zip"),
                                      "delivery")
            second = PKG.write_archive(delivery, os.path.join(tmp, "second.zip"),
                                       "delivery")
            if first["sha256"] != second["sha256"]:
                print("\n[archive skipped] consecutive archive hashes differ")
                code = 1
            else:
                parent = os.path.dirname(os.path.abspath(archive_to))
                os.makedirs(parent, exist_ok=True)
                shutil.copy2(first["path"], archive_to)
                archive = dict(first, path=archive_to)
    if code == 0 and archive_to:
        print("\n[archive] %s\n          sha256 %s\n          %d files, %d bytes"
              % (archive["path"], archive["sha256"], archive["files"],
                 archive["bytes"]))
    elif archive_to:
        print("\n[archive skipped] the validation gate failed")

    print("\n" + "=" * 74)
    print("PIPELINE %s — %d task(s), delivery at %s"
          % ("VERIFIED" if code == 0 else "COMPLETED WITH FINDINGS",
             len(task_ids), delivery))
    print("=" * 74)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Assemble a task's evaluator data from a completed orchestrator workspace.

This is the step that was missing. The orchestrator writes each role's output
into `runs/<run_id>/output/<category>/` and registers it by digest; the builder
reads `tasks/<task_id>/`. Nothing joined the two, so the only task that could be
built was the one whose data had been reverse-engineered from an accepted
package by hand.

    python3 pipeline/assemble_task.py <workspace>

Refuses to assemble unless every production role has a current run. A task
assembled from a stale prompt or a superseded rubric would build cleanly and be
wrong, which is the failure mode this pipeline keeps having to design against.

What lands where:

    prompt          -> tasks/<id>/prompt.md
    rubric          -> tasks/<id>/rubric.json  (+ rubric_pretty.txt, derived)
    expected_values -> tasks/<id>/expected_values.json
    lineage_draft   -> tasks/<id>/lineage.json
    task_blueprint  -> tasks/<id>/task_meta.json
    reference_spec  -> tasks/<id>/reference_spec.json  (and the file order)
    gold_provenance -> tasks/<id>/gold_provenance.json
    references      -> staging/<task_id>/reference_files/
    gold            -> staging/<task_id>/deliverable_files/

`gold_marking.json` is not written here. It is a person's work, and inventing an
empty one would let the threshold check report a score nobody awarded.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lineage as LN                                              # noqa: E402
from orchestrator import Pipeline, PipelineError, PRODUCTION_ROLES  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
SINGLE_FILE = {"prompt": "prompt.md"}
JSON_ARTIFACTS = {
    "rubric": ("rubric.json", "rubric.json"),
    "expected_values": ("report.json", "expected_values.json"),
    "lineage_draft": ("report.json", "lineage.json"),
    "task_blueprint": ("report.json", None),          # folded into task_meta.json
    "design_notes": ("report.json", None),            # evaluator-only half of it
    "reference_spec": ("spec.json", "reference_spec.json"),
    "gold_provenance": ("report.json", "gold_provenance.json"),
}
STAGED = {"references": "reference_files", "gold": "deliverable_files"}


def rubric_pretty(items):
    """The reviewer-facing rendering, derived rather than stored.

    Verified against the accepted package: `[+N] criterion`, blank line between
    items, reproduces its rubric_pretty exactly. Keeping a second hand-edited
    copy would be one more thing that can disagree with the JSON.
    """
    return "\n\n".join("[+%d] %s" % (item["score"], item["criterion"])
                       for item in items)


def _read(pipeline, category, filename):
    artifact = pipeline._load()["artifacts"].get(category)
    if not artifact:
        raise PipelineError("workspace has no %s artifact" % category)
    candidates = [filename]
    if category == "expected_values" and filename == "report.json":
        candidates.append("expected_values.json")
    path = next((pipeline.root / artifact["path"] / name
                 for name in candidates
                 if (pipeline.root / artifact["path"] / name).is_file()), None)
    if path is None:
        raise PipelineError("%s is missing one of: %s"
                            % (category, ", ".join(candidates)))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _artifact_dir(pipeline, category):
    artifact = pipeline._load()["artifacts"].get(category)
    if not artifact:
        raise PipelineError("workspace has no %s artifact" % category)
    return pipeline.root / artifact["path"]


def _normalise_guards(value):
    if isinstance(value, list):
        return {"rules": value}
    if not isinstance(value, dict):
        raise PipelineError("design_notes.guards must be an object or a rule list")
    return value


def _normalise_file_roles(value):
    if isinstance(value, list):
        return {"source_files": value}
    if not isinstance(value, dict):
        raise PipelineError("task_blueprint.file_roles must be an object or a file-role list")
    return value


def assemble(workspace, tasks_root=None, staging_root=None):
    pipeline = Pipeline(workspace)
    state = pipeline._load()
    task_id = state["task_id"]

    stale = [role for role in PRODUCTION_ROLES
             if not pipeline._current_run(state, role)]
    if stale:
        raise PipelineError(
            "cannot assemble: %s %s not current. Assembling over a stale role "
            "produces a task that builds cleanly and is wrong."
            % (", ".join(stale), "is" if len(stale) == 1 else "are"))

    tasks_root = Path(tasks_root or (BASE / "tasks"))
    staging_root = Path(staging_root or (BASE / "staging"))
    out = tasks_root / task_id
    out.mkdir(parents=True, exist_ok=True)

    def write_json(name, value):
        with open(out / name, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    # -- prompt ------------------------------------------------------------
    prompt_dir = _artifact_dir(pipeline, "prompt")
    texts = [p.read_text(encoding="utf-8")
             for p in sorted(prompt_dir.rglob("*")) if p.is_file()]
    prompt = "\n".join(texts).strip()
    if not prompt:
        raise PipelineError("the prompt artifact is empty")
    (out / "prompt.md").write_text(prompt + "\n", encoding="utf-8")

    # -- json artifacts ----------------------------------------------------
    collected = {}
    for category, (source, target) in JSON_ARTIFACTS.items():
        value = _read(pipeline, category, source)
        collected[category] = value
        if target:
            write_json(target, value)

    items = collected["rubric"]
    (out / "rubric_pretty.txt").write_text(rubric_pretty(items) + "\n",
                                           encoding="utf-8")

    # -- staged files ------------------------------------------------------
    for category, folder in STAGED.items():
        # Per task, not flat. S-REF already writes staging/<task_id>/, and a
        # flat staging directory means two tasks overwrite each other's inputs
        # without a word.
        destination = staging_root / task_id / folder
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for child in sorted(_artifact_dir(pipeline, category).iterdir()):
            if child.name.startswith("."):
                continue
            shutil.copy2(str(child), str(destination / child.name))

    # -- task_meta ---------------------------------------------------------
    blueprint = collected["task_blueprint"]
    notes = collected["design_notes"]
    spec = collected["reference_spec"]
    reference_order = [entry["filename"] for entry in spec]
    gold_order = sorted(p.name for p in
                        (staging_root / task_id / "deliverable_files").iterdir()
                        if not p.name.startswith("."))
    declared_gold = blueprint.get("output_contract")
    if declared_gold and sorted(declared_gold) == sorted(gold_order):
        gold_order = list(declared_gold)          # the order the prompt names them

    existing = {}
    if (out / "task_meta.json").is_file():
        existing = json.loads((out / "task_meta.json").read_text(encoding="utf-8"))
    guards = _normalise_guards(notes.get("guards", {}))
    file_roles = _normalise_file_roles(blueprint.get("file_roles", {}))
    meta = dict(existing, **{
        "task_id": task_id,
        "sector": blueprint["sector"],
        "occupation": blueprint["occupation"],
        "language": blueprint.get("language", "en"),
        "rubric_version": blueprint.get("rubric_version", "v1"),
        "column_maps": notes.get("column_maps", {}),
        "file_roles": file_roles,
        "guards": guards,
        # What a quoted figure looks like in this task's documents. Without it
        # the cross-file numeric check refuses to run rather than guessing a
        # currency's spelling.
        "figure_pattern": notes.get("figure_pattern"),
        "item_codes": notes.get("item_codes")
                      or ["R%02d" % (n + 1) for n in range(len(items))],
        "file_order": {"reference_files": reference_order,
                       "deliverable_files": gold_order},
        "note": "Evaluator-only. Never packaged into the delivery root.",
        "assembled_from": str(Path(workspace).resolve()),
    })
    write_json("task_meta.json", meta)

    # -- checks that are cheaper here than downstream ----------------------
    problems = []
    staged_refs = sorted(p.name for p in
                         (staging_root / task_id / "reference_files").iterdir()
                         if not p.name.startswith("."))
    if staged_refs != sorted(reference_order):
        problems.append("reference_spec declares %s but staging holds %s"
                        % (sorted(reference_order), staged_refs))
    ok, detail = LN.verify(collected["lineage_draft"], staged_refs)
    if not ok:
        problems.append("lineage %s" % detail)
    if problems:
        raise PipelineError("; ".join(problems))

    pending = [] if (out / "gold_marking.json").is_file() else ["gold_marking.json"]
    return {"task_id": task_id, "task_dir": str(out),
            "reference_files": reference_order, "deliverable_files": gold_order,
            "rubric_items": len(items),
            "awaiting_human": pending}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace")
    parser.add_argument("--tasks-root")
    parser.add_argument("--staging-root")
    args = parser.parse_args(argv)
    try:
        result = assemble(args.workspace, args.tasks_root, args.staging_root)
    except (PipelineError, OSError, ValueError, KeyError) as exc:
        print("assemble error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["awaiting_human"]:
        print("\nstill needs a person: %s" % ", ".join(result["awaiting_human"]),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

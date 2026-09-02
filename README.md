# GDPval Pipeline v4

Evidence-bearing orchestration and deterministic validation for GDPval task
packages. The repository contains the reusable pipeline implementation and
its machine-readable contracts; it intentionally does not contain task data,
real deliverables, reviewer records, workbench state, or generated archives.

## Layout

- `build/pipeline/`: orchestrator, planner, assembler, review-kit builder, validator and tests
- `产线规范/`: role contracts, policy, release invariants and Mermaid flowchart

For V1/V2 remediation and public-delivery hygiene, follow
`产线规范/V1V2迭代整改与发布卫生.md`. It separates deterministic QA tooling from
actual AI-production evidence, keeps internal remediation working records outside
`delivery/`, and fails release on dangling declared paths or unsupported review
status claims.
- `human-review-skill/`: separately callable staged human-review skill package

The runtime expects evaluator-only task data under `build/tasks/` and writes
workbench, staging and delivery data outside the source checkout or in paths
configured with `GDPVAL_*` environment variables. Keep those data directories
out of version control.

## Verify

```bash
cd build/pipeline
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q \
  test_manage.py test_orchestrator.py test_assemble_task.py \
  test_multitask_build.py test_spec_checks.py test_build_references.py \
  test_package.py
```

The default release path uses one external Markdown confirmation with three
independent signature rows after A10-A12 conclusions and current byte bindings
are frozen. A validated return first materializes the evaluator-only reviewer
roster, strict validation runs on the resulting candidate, and H-REG then writes
the workbench human-review record. Release hygiene and the deterministic
two-archive hash check follow. The older
two-stage XLSX review path remains available only as an explicitly selected
compatibility workflow for projects that need item-by-item reviewer editing and
supplemental finding closure.

In `staged_xlsx_v1` compatibility mode, review-package generation is fail-closed against `review-input-v1`. Each
reviewer workbook includes a tailored brief and current file inventory. A
Conditional pass, Fail, conditional occupation mapping, Rubric Revise/Reject or
explicit confirmation request enters remediation; only affected original
reviewers receive a changed-items-only supplemental XLSX. Unchanged decisions
are carried forward and no reviewer is required to manufacture an objection.

## Safety boundary

`deliverable_files` must use either the authentic byte-copy path or a
task-scoped desensitization path bound to an authentic source, current-file
hash, explicit transformation record, adopted source record, and hashed
lineage. Do not commit them here. Never
commit API keys, source manifests containing private URLs, client task data,
human-review evidence, or generated ZIP files.

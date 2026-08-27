# GDPval Pipeline v4

Evidence-bearing orchestration and deterministic validation for GDPval task
packages. The repository contains the reusable pipeline implementation and
its machine-readable contracts; it intentionally does not contain task data,
real deliverables, reviewer records, workbench state, or generated archives.

## Layout

- `build/pipeline/`: orchestrator, planner, assembler, builder, validator and tests
- `产线规范/`: role contracts, policy, release invariants and Mermaid flowchart

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

The full validator test additionally depends on the local document/PDF
runtime. The release path must run strict validation, human-review
registration, and the deterministic two-archive hash check before publishing.

## Safety boundary

`deliverable_files` are real Gold files and must be supplied by the task owner
with per-file source URL and SHA-256 evidence. Do not commit them here. Never
commit API keys, source manifests containing private URLs, client task data,
human-review evidence, or generated ZIP files.

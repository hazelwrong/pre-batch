---
name: "human-review-gdpval"
description: "Perform staged human review of GDPval task packages, including general QA, occupational expert review, final review, remediation confirmation, and current-version hash sign-off. Use when a task requires reviewer workbooks, substantive findings, review timing, signatures, or evidence-bound release checks."
metadata:
  short-description: "Run staged human review for GDPval"
---

# Human Review

Use this skill for GDPval human-review work. Treat the repository's
`产线规范/agent_roles.json` and `产线规范/policy.json` as the machine-readable
source of truth; do not invent a parallel policy in prompts or scripts.

## Safety and evidence boundary

- Keep evaluator-only task data, real deliverables, reviewer records, workbench
  state, staging output, and generated ZIPs outside the source repository unless
  the task explicitly requires a local working copy. Never commit them.
- `deliverable_files` are real Gold files. Preserve them byte-for-byte and
  require per-file source URL and SHA-256 evidence. `reference_files` may be
  reconstructed from authentic sources within the allowed formats, but must not
  contain answer-bearing evaluator data.
- Keep prompt and rubric language consistent. Rubric items must total 100,
  satisfy the configured sampled count/lower bound, and default `required` to
  true when omitted.
- Do not mark a workflow release-ready from a boolean or a command exit code.
  Use the fixed validator, bind current human-review evidence, and perform the
  deterministic two-archive hash check.

## Efficient execution

1. Inspect the current workflow with `python3 pipeline/manage.py next ...`.
2. Dispatch every independent ready role up to the configured slot limit. After
   T12, T13 and T14 can run in parallel; T15 waits for both.
3. Ask each agent for a complete first pass near the configured quality target
   (currently 80) and a compact risk ledger. This target reduces rework but is
   not a release gate.
4. On failure, use the declared `reason_code`. Rebuild the earliest routed role
   and all stale descendants; never hand-edit stale status back to current.
5. After production, create one candidate delivery ZIP and one phase-1 review
   kit. The general reviewer and occupational expert each return exactly one
   XLSX; ingest both original files immutably with SHA-256 and keep project-side
   identity/time/credential transcription separate.
6. Close findings, run pre-final validation, and only then generate the final
   reviewer package. The third reviewer returns one XLSX and must complete it
   strictly later than both first-layer reviews, all closure times, validation
   and final-package freeze.
7. Run strict final validation after the final receipt. Register H-REG from the
   staged receipts after validation, then package twice and compare SHA-256.

## Useful commands

```bash
cd build
python3 pipeline/manage.py next workbench --slots 4 --json
python3 pipeline/orchestrator.py status workbench/<task_id>
python3 pipeline/review_kits.py phase1 \
  workbench/<task_id> delivery outputs/<task_id> \
  --tasks-root tasks --staging-root staging \
  --node <bundled-node> --node-modules <bundled-node_modules>
python3 pipeline/orchestrator.py record-validation \
  workbench/<task_id> delivery --stage final
python3 pipeline/orchestrator.py record-human-review \
  workbench/<task_id>
```

Read `产线规范/pipeline设计.md` for the full role isolation matrix, rework
routing, human-review timing rule, and release invariants. Read
`产线规范/人工复核包规范.md` before generating or ingesting review workbooks.
When a user supplies a historical review bundle, asks what upstream must
prepare, or the input layout is not pipeline-native, read
`references/review-input-contract.md` and normalize the package before any
review or signature is recorded.

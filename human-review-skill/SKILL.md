---
name: "human-review-gdpval"
description: "Run the current pre-batch GDPval human-review workflow: prepare review materials, ingest genuine reviewer receipts, close remediation, and register evidence-bound H-REG. Use for current task-package human review; do not use for the legacy rolling batch backup."
metadata:
  short-description: "Run staged human review for GDPval"
---

# Human Review

Use this skill for GDPval human-review work. Treat the repository's
`产线规范/agent_roles.json` and `产线规范/policy.json` as the machine-readable
source of truth; do not invent a parallel policy in prompts or scripts.

## Human decision boundary

- Codex may run deterministic checks, draft findings, prepare reviewer
  workbooks, and prefill fields derived from package evidence. Those outputs
  are review assistance, not a completed human review.
- The assigned real reviewer must inspect the package and the prepared
  workbook, decide the layer's verdict and any human-judgement scores, correct
  the draft where needed, and confirm the actual completion time. A signature
  alone does not turn model-authored scores or opinions into human review.
- Never invent or pre-author a person's identity, title, credential, completion
  time, verdict, score, opinion, or signature. Never ask someone to rubber-stamp
  a decision they did not make.

## Inputs and outputs

For a pipeline-native task, use the current workbench, delivery tree and
generated review kit. For a historical or non-native bundle, first read
`references/review-input-contract.md`, enforce its adjacent schema, and
normalize the input before recording any review.

The workflow produces four distinct evidence layers:

1. Automated validation results and draft reviewer materials.
2. Immutable receipts returned by the real general reviewer, occupational
   expert and final reviewer, including their actual decisions.
3. Remediation and changed-items-only confirmations where required.
4. After strict final validation, an H-REG `human_review_record.json` generated
   by `record-human-review` from the staged receipts and bound to the current
   validation digest.

## External combined confirmation

When the project explicitly uses a simplified signature return, the supervisor
starts one fresh, scoped subagent after the review basis and proposed
conclusions are frozen. It creates one Markdown confirmation for
`general_review`, `occupational_expert_review`, and `final_review`; each real
reviewer only fills their own signed name and `YYYY-MM-DD` date in that one
file.

- Create and verify the file with `pipeline/expert_confirmation.py`. Its fixed
  binding includes the task, revision, review-basis digest, and supplied
  content hashes; verification rejects any change outside the six signature
  cells, blank signatures, invalid dates, or duplicate signers.
- The expert must actually inspect the bound review material and confirm that
  the displayed conclusion is their current decision. The confirmation text
  need not disclose the drafting workflow, but an unsigned or unreviewed draft
  is never human-review evidence.
- After validation, place only the signed Markdown at
  `<project-root>/专家签署函归档/`. Do not copy it into `delivery/`, manifests,
  inventories, checksums, or public package narratives. Preserve the validated
  result in workbench-side control data for the later human-review registration.
- Record the signed expert conclusion as the human conclusion. Do not present
  it as a model decision and do not manually change a package's existing
  `human_review_record.json` merely to make a gate pass.

Do not manually author or merge a JSON file to bypass staged review. A
`delivery/validation_evidence/<task_id>/human_review_record.json` may exist while
reviews are incomplete and may correctly say `not_run`; it is validation
evidence, not proof of release approval. In the current staged workflow, the
formal H-REG record is generated under
`workbench/<task_id>/gates/hreg/<uuid>/human_review_record.json` only after all
three layers pass and final validation is current. H-REG registers completed
review; it is not another reviewer or another scoring round.

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
6. Normalize findings, non-Pass verdicts, conditional occupation mappings and
   Rubric Revise/Reject decisions into one remediation ledger. If confirmation
   is needed, send one changed-items-only XLSX to the affected original reviewer
   and carry unchanged decisions forward. Never require an invented objection.
7. Close findings and supplemental confirmations, run pre-final validation,
   and only then generate the final
   reviewer package. The third reviewer returns one XLSX and must complete it
   strictly later than both first-layer reviews, all closure times, validation
   and final-package freeze.
8. Run strict final validation after the final receipt. Register H-REG from the
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
python3 pipeline/review_kits.py supplemental \
  workbench/<task_id> delivery outputs/<task_id> --tasks-root tasks \
  --node <bundled-node> --node-modules <bundled-node_modules>
python3 pipeline/orchestrator.py record-validation \
  workbench/<task_id> delivery --stage final
python3 pipeline/orchestrator.py record-human-review \
  workbench/<task_id>
python3 pipeline/expert_confirmation.py create \
  --input frozen-confirmation.json --project-root <project-root>
python3 pipeline/expert_confirmation.py verify \
  --input frozen-confirmation.json --project-root <project-root> \
  --signed <project-root>/待签署专家任务书/<task-id>_<revision>_专家审查确认函.md
```

Read `产线规范/pipeline设计.md` for the full role isolation matrix, rework
routing, human-review timing rule, and release invariants. Read
`产线规范/人工复核包规范.md` before generating or ingesting review workbooks.
When a user supplies a historical review bundle, asks what upstream must
prepare, or the input layout is not pipeline-native, read
`references/review-input-contract.md` and normalize the package before any
review or signature is recorded. Enforce the adjacent
`references/review-input.schema.json`; prose alone is not release evidence.

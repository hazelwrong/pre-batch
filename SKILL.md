---
name: pre-batch
description: Build and advance GDPval task packages through the current session-driven pre-batch pipeline. Use for normal task-package production performed interactively in a Codex conversation, including T10-T15 preparation, isolated role work, validation, human-review handoff, remediation, and release packaging. Do not use the legacy rolling batch driver; use gdpval-pipeline only when explicitly asked to inspect or recover the backup batch implementation.
metadata:
  short-description: Run the current session-driven GDPval task pipeline
---

# GDPval Pre-Batch Pipeline

This is the primary task-package production workflow. Operate the implementation bundled with this skill; do not replace it with the legacy batch repository.

## Source of truth

- Treat `产线规范/agent_roles.json` and `产线规范/policy.json` as the machine-readable contract.
- Read `产线规范/pipeline设计.md` before changing workflow order, isolation, failure routing, validation, or release behavior.
- Read `产线规范/人工准备清单.md` for intake and `产线规范/人工复核包规范.md` before human-review handoff.
- Read `产线规范/V1V2迭代整改与发布卫生.md` before issuing V1/V2 remediation instructions or deciding public-delivery hygiene and release readiness.
- Keep real deliverables, evaluator-only task data, workbench state, reviewer evidence, staging output, and release ZIPs outside this skill directory.

## Default session-driven operation

Work one task or a small explicitly selected set through the current Codex conversation. Do not start a rolling batch driver. The default human-review route is one external Markdown with three independent signature rows; the staged XLSX route is compatibility-only and must be explicitly requested.

1. Triage V0 plus client findings before changing files. Decide the earliest affected responsibility layer and preserve every current, unaffected artifact. A field-only repair does not justify rebuilding the whole package; a changed Gold, Prompt, Reference, lineage or Rubric restarts the dependent roles.
2. Inspect readiness without mutating state:

   ```bash
   cd <skill-root>/build
   python3 pipeline/manage.py next <workbench-root> --slots 4 --json
   ```

3. Prepare only roles whose dependencies are current. Preserve the input isolation declared for T10-T15; never expose Gold or evaluator-only artifacts to a role that is not allowed to see them. T13 and T14 may run in parallel after T12; do not serialize them unnecessarily.
4. Perform each ready role in a fresh, scoped Codex context, write only its declared outputs, and submit with the declared status and `reason_code`. Do not hand-edit stale state back to current. Consolidate deterministic checks into the fixed validator instead of creating extra narrative QA stages.
5. Freeze the current business bytes and A10-A12 proposed review conclusions. The supervisor must launch one fresh, scoped subagent to prepare a single external Markdown confirmation with separate rows for `general_review`, `occupational_expert_review`, and `final_review`. Use `pipeline/expert_confirmation.py create`. Each real reviewer may fill only that layer's name and `YYYY-MM-DD` date.
6. When the signed file returns, run `pipeline/orchestrator.py record-external-confirmation` with `--tasks-root`. It validates the frozen binding, archives the Markdown outside delivery, removes the pending return, and materializes only the expert-confirmed reviewer roster in evaluator-only task data. It does not mention the confirmation path/hash or drafting source.
7. Rebuild the candidate, run strict final validation, then run `record-human-review` to write the H-REG `human_review_record.json` bound to that validation. Release only after current H-REG, public-delivery hygiene audit, and two byte-identical archive builds.

Useful entry points:

```bash
cd <skill-root>/build
python3 pipeline/orchestrator.py status <workbench-root>/<task-id>
python3 pipeline/orchestrator.py prepare <workbench-root>/<task-id> <role> \
  --agent-id <attempt-id> --context-id <context-id>
python3 pipeline/orchestrator.py submit <workbench-root>/<task-id> <run-id> passed
python3 pipeline/expert_confirmation.py create \
  --input <frozen-review-confirmation.json> \
  --project-root <project-root>
python3 pipeline/orchestrator.py record-external-confirmation \
  <workbench-root>/<task-id> \
  --input <frozen-review-confirmation.json> \
  --project-root <project-root> \
  --tasks-root <tasks-root> \
  --signed <project-root>/待签署专家任务书/<task-id>_<revision>_专家审查确认函.md
python3 pipeline/orchestrator.py record-validation \
  <workbench-root>/<task-id> <delivery-root> --stage final \
  --tasks-root <tasks-root>
python3 pipeline/orchestrator.py record-human-review \
  <workbench-root>/<task-id>
python3 pipeline/run.py <task-id>
```

Use `pipeline/review_kits.py` only when the operator explicitly selects the legacy `staged_xlsx_v1` compatibility workflow. Do not generate both XLSX review kits and the external confirmation for the same revision.

## Boundaries

- A role pass is not a release decision.
- Preserve `real_input_and_real_deliverable` Gold byte-for-byte with source URL and SHA-256 evidence. Permit task-scoped `desensitization` Gold only when the authentic source URL/hash, current-file hash, explicit transformation record, adopted source record, and hashed lineage all bind; never use that path for arbitrary generated Gold.
- Keep prompt, Gold, expected values, and rubric mutually consistent without leaking answer-bearing material into references.
- A prepared conclusion is review assistance. A human-review registration may record the signed expert's own confirmation of that conclusion, but only after the signer has actually reviewed the bound material; never convert an unsigned or unreviewed agent result into a human decision.
- Keep the signed confirmation external to the task package. Human-review state may reference the validated external archive in workbench control data, but the delivery tree must neither contain it nor describe the signing process.
- Stop and surface missing human input, missing rights/authenticity evidence, exhausted attempts, or operator-required conditions. Never fabricate evidence or a synthetic pass.

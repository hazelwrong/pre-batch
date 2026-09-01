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

## Session-driven operation

Work one task or a small explicitly selected set through the current Codex conversation. Do not start a rolling batch driver.

1. Inspect readiness without mutating state:

   ```bash
   cd <skill-root>/build
   python3 pipeline/manage.py next <workbench-root> --slots 4 --json
   ```

2. Prepare only roles whose dependencies are current. Preserve the input isolation declared for T10-T15; never expose Gold or evaluator-only artifacts to a role that is not allowed to see them.
3. Perform each ready role in a fresh, scoped Codex context, write only its declared outputs, and submit with the declared status and `reason_code`. Do not hand-edit stale state back to current.
4. After T15, build phase-1 review materials. General and occupational review may proceed in parallel; final review waits for remediation closure and pre-final validation.
5. Release only after strict validation, current three-layer human-review evidence, H-REG binding, public-delivery hygiene audit, and two byte-identical archive builds.

Useful entry points:

```bash
cd <skill-root>/build
python3 pipeline/orchestrator.py status <workbench-root>/<task-id>
python3 pipeline/orchestrator.py prepare <workbench-root>/<task-id> <role> \
  --agent-id <attempt-id> --context-id <context-id>
python3 pipeline/orchestrator.py submit <workbench-root>/<task-id> <run-id> passed
python3 pipeline/review_kits.py phase1 \
  <workbench-root>/<task-id> <delivery-root> <output-root>/<task-id> \
  --tasks-root <tasks-root> --staging-root <staging-root> \
  --node <bundled-node> --node-modules <bundled-node-modules>
python3 pipeline/run.py <task-id>
```

## Boundaries

- A role pass is not a release decision.
- Preserve authentic deliverables byte-for-byte with source URL and SHA-256 evidence; references may be reconstructed only within policy.
- Keep prompt, Gold, expected values, and rubric mutually consistent without leaking answer-bearing material into references.
- Stop and surface missing human input, missing rights/authenticity evidence, exhausted attempts, or operator-required conditions. Never fabricate evidence or a synthetic pass.

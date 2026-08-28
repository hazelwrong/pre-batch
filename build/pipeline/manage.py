"""Read-only planner for the next multi-task agent wave.

The planner deliberately does not prepare or submit runs.  It turns the
evidence in each workflow into a compact dispatch plan for the session that is
managing the agents.
"""
import argparse
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_WORKBENCH = HERE.parent / "workbench"
DEFAULT_CONTRACTS = HERE.parent.parent / "产线规范" / "agent_roles.json"
DEFAULT_POLICY = HERE.parent.parent / "产线规范" / "policy.json"
HUMAN_ROLE = "gold_curator"


class PlanError(RuntimeError):
    pass


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise PlanError("cannot read %s: %s" % (path, exc)) from exc


def _workspace_paths(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise PlanError("workbench root is not a directory: %s" % root)
    paths = {path.parent for path in root.rglob("workflow.json")}
    if (root / "workflow.json").is_file():
        paths.add(root)
    return sorted(paths, key=lambda path: path.as_posix())


def _role_order(roles):
    """Stable topological order, retaining contract order for ties."""
    remaining = list(roles)
    ordered = []
    while remaining:
        ready = [name for name in remaining
                 if all(dep in ordered
                        for dep in roles[name].get("requires_roles", []))]
        if not ready:
            raise PlanError("role contracts contain a dependency cycle")
        for name in ready:
            ordered.append(name)
            remaining.remove(name)
    return ordered


def _run_is_current(state, run):
    if run.get("decision") != "passed":
        return False
    if run.get("role") in (state.get("invalidated_roles") or {}):
        return False
    artifacts = state.get("artifacts", {})
    for category, digest in run.get("input_artifacts", {}).items():
        if artifacts.get(category, {}).get("digest") != digest:
            return False
    for category, digest in run.get("output_artifacts", {}).items():
        artifact = artifacts.get(category, {})
        if (artifact.get("digest") != digest or
                artifact.get("produced_by") != run.get("run_id")):
            return False
    return True


def _current_roles(state, roles):
    current = {}
    for role in roles:
        current[role] = next(
            (run for run in reversed(state.get("runs", []))
             if run.get("role") == role and _run_is_current(state, run)),
            None)
    return current


def _run_input_is_current(state, run):
    artifacts = state.get("artifacts", {})
    return all(artifacts.get(name, {}).get("digest") == digest
               for name, digest in run.get("input_artifacts", {}).items())


def _readonly_inputs(workspace, state, names):
    result = []
    for name in names:
        artifact = state.get("artifacts", {}).get(name)
        if not artifact:
            continue
        result.append({
            "category": name,
            "digest": artifact.get("digest"),
            "path": str((workspace / artifact["path"]).resolve()),
            "mode": "read-only",
        })
    return result


def _failure_routes(role, contract, roles):
    routes = []
    for raw in contract.get("failure_routes", []):
        route = {key: raw.get(key)
                 for key in ("reason_code", "action", "route_to")}
        if not all(route.values()):
            raise PlanError("%s has an incomplete failure route" % role)
        if route["route_to"] != "stop" and route["route_to"] not in roles:
            raise PlanError("%s routes %s to unknown role %r" %
                            (role, route["reason_code"], route["route_to"]))
        routes.append(route)
    if not routes:
        raise PlanError("%s has no failure routes" % role)
    return routes


def _work_item(instance_id, workspace, state, role, contract, roles,
               status, run=None):
    required_inputs = list(contract.get("required_inputs", []))
    item = {
        "instance_id": instance_id,
        "task_id": state["task_id"],
        "workspace": str(workspace),
        "status": status,
        "role": role,
        "role_id": contract["id"],
        "role_name": contract["name"],
        "model_tier": contract["model_tier"],
        "model": contract["resolved_model"],
        "thinking": contract["resolved_thinking"],
        "required_inputs": required_inputs,
        "readonly_inputs": _readonly_inputs(workspace, state, required_inputs),
        "required_outputs": list(contract.get("required_outputs", [])),
        "completion": "all required outputs validate and submit decision is passed",
        "failure_routes": _failure_routes(role, contract, roles),
    }
    if run is not None:
        item["run_id"] = run.get("run_id")
        item["input_current"] = _run_input_is_current(state, run)
        if not item["input_current"]:
            item["completion"] = (
                "prepared inputs are stale; close this run as rework before redispatch")
    return item


def _load_contracts(path):
    document = _read_json(path)
    roles = document.get("roles") or {}
    tiers = document.get("model_tiers") or {}
    if not roles:
        raise PlanError("role contracts contain no roles")
    resolved = {}
    for name, raw in roles.items():
        contract = dict(raw)
        tier = tiers.get(contract.get("model_tier"), {})
        contract["resolved_model"] = tier.get("default_model")
        contract["resolved_thinking"] = tier.get("thinking")
        resolved[name] = contract
    return resolved


def _load_execution_policy(path, roles):
    execution = _read_json(path).get("execution") or {}
    total = execution.get("max_agent_runs_per_task")
    limits = execution.get("max_attempts_per_role") or {}
    if not isinstance(total, int) or total < 1:
        raise PlanError("policy.execution.max_agent_runs_per_task must be positive")
    missing = [role for role in roles if role not in limits]
    invalid = [role for role, limit in limits.items()
               if role in roles and (not isinstance(limit, int) or limit < 1)]
    if missing or invalid:
        raise PlanError("invalid policy.execution.max_attempts_per_role; "
                        "missing=%s invalid=%s" % (missing, invalid))
    return {"max_agent_runs_per_task": total,
            "max_attempts_per_role": limits}


def _budget_exhaustions(state, role, roles, execution):
    runs = state.get("runs", [])
    attempts = sum(1 for run in runs if run.get("role") == role)
    role_limit = execution["max_attempts_per_role"][role]
    agent_runs = sum(1 for run in runs
                     if run.get("role") in roles
                     and run.get("role") != HUMAN_ROLE)
    total_limit = execution["max_agent_runs_per_task"]
    exhausted = []
    if attempts >= role_limit:
        exhausted.append({
            "reason_code": "BUDGET.ROLE_ATTEMPTS_EXHAUSTED",
            "action": "stop_and_escalate",
            "route_to": "stop",
            "used": attempts,
            "limit": role_limit,
            "scope": "role",
        })
    if role != HUMAN_ROLE and agent_runs >= total_limit:
        exhausted.append({
            "reason_code": "BUDGET.TASK_AGENT_RUNS_EXHAUSTED",
            "action": "stop_and_escalate",
            "route_to": "stop",
            "used": agent_runs,
            "limit": total_limit,
            "scope": "task",
        })
    return exhausted


def _budget_block(workspace, state, role, contract, roles, execution):
    reasons = _budget_exhaustions(state, role, roles, execution)
    if not reasons:
        return None
    return {
        "task_id": state["task_id"],
        "workspace": str(workspace),
        "role": role,
        "role_id": contract["id"],
        "reason_code": reasons[0]["reason_code"],
        "action": "stop_and_escalate",
        "route_to": "stop",
        "budget_exhaustions": reasons,
        "missing_roles": [],
        "missing_inputs": [],
    }


def plan_next(workbench_root=DEFAULT_WORKBENCH, slots=4,
              contracts_path=DEFAULT_CONTRACTS, policy_path=DEFAULT_POLICY):
    if slots < 1:
        raise PlanError("slots must be at least 1")
    roles = _load_contracts(contracts_path)
    execution = _load_execution_policy(policy_path, roles)
    order = _role_order(roles)
    workspaces = []
    for workspace in _workspace_paths(workbench_root):
        state = _read_json(workspace / "workflow.json")
        if not state.get("task_id") or not isinstance(state.get("runs", []), list):
            raise PlanError("invalid workflow state: %s" % workspace)
        workspaces.append((workspace, state))

    # T10 is human/L0 for planning even if a legacy role contract says L2.
    agent_runs = []
    for workspace, state in workspaces:
        for index, run in enumerate(state.get("runs", [])):
            if run.get("role") == HUMAN_ROLE:
                continue
            agent_runs.append((run.get("created_at", ""), state["task_id"],
                               run.get("run_id", ""), workspace, index, run, state))
    agent_runs.sort(key=lambda value: value[:3])
    run_numbers = {(str(value[3]), value[4]): number
                   for number, value in enumerate(agent_runs, 1)}

    active = []
    superseded_prepared_count = 0
    awaiting_human = []
    blocked = []
    complete = []
    candidates = []

    for workspace, state in workspaces:
        current = _current_roles(state, roles)
        current_indexes = {}
        for index, run in enumerate(state["runs"]):
            if _run_is_current(state, run):
                current_indexes[run.get("role")] = index
        all_prepared = [(index, run) for index, run in enumerate(state["runs"])
                        if run.get("status") == "prepared"
                        and run.get("role") != HUMAN_ROLE]
        prepared = [(index, run) for index, run in all_prepared
                    if current_indexes.get(run.get("role"), -1) <= index]
        superseded_prepared_count += len(all_prepared) - len(prepared)
        for _, run in prepared:
            if run.get("role") not in roles:
                raise PlanError("unknown prepared role %r in %s" %
                                (run.get("role"), workspace))
        if prepared:
            # Legacy workspaces can contain abandoned prepared records.  One
            # task still occupies one slot: prefer current inputs, then the
            # earliest role, then the newest attempt of that role.
            role_rank = {role: index for index, role in enumerate(order)}
            prepared.sort(key=lambda value: (
                not _run_input_is_current(state, value[1]),
                role_rank[value[1]["role"]],
                -value[0]))
            index, run = prepared[0]
            role = run["role"]
            number = run_numbers[(str(workspace), index)]
            # Budget is consumed and enforced when prepare creates the run.
            # Re-checking used >= limit here deadlocks the final legal attempt:
            # attempt 2 of a 2-attempt budget would be prepared but never run.
            item = _work_item(
                "A-%03d" % number, workspace, state, role, roles[role], roles,
                "prepared", run)
            item["superseded_prepared_count"] = len(prepared) - 1
            active.append(item)
            superseded_prepared_count += len(prepared) - 1
        if prepared:
            continue

        if not current.get(HUMAN_ROLE):
            contract = roles[HUMAN_ROLE]
            budget_block = _budget_block(
                workspace, state, HUMAN_ROLE, contract, roles, execution)
            if budget_block:
                blocked.append(budget_block)
                continue
            required = list(contract.get("required_inputs", []))
            awaiting_human.append({
                "task_id": state["task_id"],
                "workspace": str(workspace),
                "status": "awaiting_human",
                "role": HUMAN_ROLE,
                "role_id": contract["id"],
                "role_name": contract["name"],
                "model_tier": "L0",
                "model": None,
                "thinking": None,
                "required_inputs": required,
                "missing_inputs": [name for name in required
                                   if name not in state.get("artifacts", {})],
                "required_outputs": list(contract.get("required_outputs", [])),
                "failure_routes": _failure_routes(
                    HUMAN_ROLE, contract, roles),
            })
            continue

        outstanding = [role for role in order
                       if role != HUMAN_ROLE and not current.get(role)]
        if not outstanding:
            cycle = state.get("review_cycle")
            stage = ((cycle or {}).get("status") or
                     "phase1_review_kit_required")
            if stage == "release_ready":
                complete.append({"task_id": state["task_id"],
                                 "workspace": str(workspace),
                                 "status": "release_ready"})
                continue
            actions = {
                "phase1_review_kit_required": ["create_phase1_review_kit"],
                "awaiting_phase1_reviews": [
                    "collect_general_review_xlsx",
                    "collect_occupational_expert_review_xlsx"],
                "phase1_review_failed": [
                    "apply_phase1_findings_then_create_new_phase1_review_kit"],
                "remediation_required": ["apply_findings_and_record_remediation"],
                "supplemental_review_kit_required": [
                    "create_changed_items_only_review_kit"],
                "awaiting_supplemental_reviews": [
                    "collect_only_affected_reviewer_xlsx"],
                "supplemental_review_failed": [
                    "apply_supplemental_feedback_and_repeat_changed_items_only"],
                "pre_final_validation_required": ["run_pre_final_validation"],
                "final_review_kit_required": ["create_final_review_kit"],
                "awaiting_final_review": ["collect_final_review_xlsx"],
                "final_review_failed": [
                    "apply_final_findings_then_repeat_pre_final_validation"],
                "final_review_complete": ["run_strict_final_validation"],
                "hreg_required": ["register_human_review_evidence"],
            }.get(stage, ["inspect_review_cycle"])
            awaiting_human.append({
                "task_id": state["task_id"],
                "workspace": str(workspace),
                "status": stage,
                "role": "human_review_cycle",
                "role_id": "H01-H03",
                "role_name": "分阶段真人审核",
                "model_tier": "L0",
                "model": None,
                "thinking": None,
                "required_inputs": [],
                "missing_inputs": [],
                "required_outputs": actions,
                "failure_routes": [],
                "parallel_external_actions": actions if stage ==
                "awaiting_phase1_reviews" else [],
            })
            continue
        ready = []
        for role in outstanding:
            contract = roles[role]
            missing_roles = [name for name in contract.get("requires_roles", [])
                             if not current.get(name)]
            missing_inputs = [name for name in contract.get("required_inputs", [])
                              if name not in state.get("artifacts", {})]
            if not missing_roles and not missing_inputs:
                budget_block = _budget_block(
                    workspace, state, role, contract, roles, execution)
                if budget_block:
                    # A later independent role might still be usable.  Do not
                    # turn one exhausted branch into a task-wide deadlock.
                    if role == outstanding[0]:
                        blocked.append(budget_block)
                    continue
                ready.append((state["task_id"], str(workspace), workspace,
                              state, role, contract))
        if ready:
            candidates.extend(ready)
            continue
        # Preserve a single, actionable block for a task with no ready branch.
        role = outstanding[0]
        contract = roles[role]
        missing_roles = [name for name in contract.get("requires_roles", [])
                         if not current.get(name)]
        missing_inputs = [name for name in contract.get("required_inputs", [])
                          if name not in state.get("artifacts", {})]
        if missing_roles or missing_inputs:
            blocked.append({
                "task_id": state["task_id"],
                "workspace": str(workspace),
                "role": role,
                "role_id": contract["id"],
                "missing_roles": missing_roles,
                "missing_inputs": missing_inputs,
            })

    active.sort(key=lambda item: item["instance_id"])
    candidates.sort(key=lambda value: (value[0], value[1]))
    available = max(0, slots - len(active))
    wave = []
    next_number = len(agent_runs) + 1
    for offset, (_, _, workspace, state, role, contract) in enumerate(
            candidates[:available]):
        wave.append(_work_item(
            "A-%03d" % (next_number + offset), workspace, state, role,
            contract, roles, "ready"))

    return {
        "workbench_root": str(Path(workbench_root).resolve()),
        "slots": slots,
        "execution_budget": execution,
        "occupied_slots": len(active),
        "available_slots": available,
        "active": active,
        "wave": wave,
        "queued_count": max(0, len(candidates) - len(wave)),
        "superseded_prepared_count": superseded_prepared_count,
        "awaiting_human": awaiting_human,
        "blocked": blocked,
        "complete": complete,
    }


def _format_item(item):
    lines = [
        "%s | %s | %s/%s | %s | %s" % (
            item["instance_id"], item["task_id"], item["role_id"],
            item["role"], item["model"] or "no-model",
            item["thinking"] or "none"),
        "  read-only: %s" % (", ".join(item["required_inputs"]) or "-"),
        "  outputs: %s" % (", ".join(item["required_outputs"]) or "-"),
        "  complete: %s" % item["completion"],
        "  failure: %s" % ", ".join(
            "%s -> %s: %s" % (route["reason_code"], route["route_to"],
                               route["action"])
            for route in item["failure_routes"]),
    ]
    return "\n".join(lines)


def format_plan(plan):
    lines = ["Slots: %d total, %d occupied, %d available" % (
        plan["slots"], plan["occupied_slots"], plan["available_slots"])]
    if plan["active"]:
        lines.append("\nACTIVE")
        lines.extend(_format_item(item) for item in plan["active"])
    if plan["wave"]:
        lines.append("\nNEXT WAVE")
        lines.extend(_format_item(item) for item in plan["wave"])
    else:
        lines.append("\nNEXT WAVE\n(none)")
    if plan["awaiting_human"]:
        lines.append("\nAWAITING HUMAN")
        for item in plan["awaiting_human"]:
            missing = ", ".join(item["missing_inputs"]) or "none"
            lines.append("%s | %s/%s | missing inputs: %s" % (
                item["task_id"], item["role_id"], item["role"], missing))
    if plan["blocked"]:
        lines.append("\nBLOCKED")
        for item in plan["blocked"]:
            if item.get("route_to") == "stop":
                budgets = ", ".join(
                    "%s %s/%s" % (reason["scope"], reason["used"],
                                   reason["limit"])
                    for reason in item.get("budget_exhaustions", []))
                lines.append("%s | %s/%s | budget_exhausted: %s | "
                             "%s -> stop: %s" % (
                    item["task_id"], item["role_id"], item["role"],
                    budgets or "yes", item["reason_code"], item["action"]))
            else:
                lines.append("%s | %s/%s | roles: %s | inputs: %s" % (
                    item["task_id"], item["role_id"], item["role"],
                    ", ".join(item["missing_roles"]) or "none",
                    ", ".join(item["missing_inputs"]) or "none"))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("next", help="show the next parallel agent wave")
    command.add_argument("workbench_root", nargs="?", default=str(DEFAULT_WORKBENCH))
    command.add_argument("--slots", type=int, default=4)
    command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = plan_next(args.workbench_root, args.slots)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_plan(plan), end="")
        return 0
    except PlanError as exc:
        print("planner error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Staged, evidence-bound human-review kits for GDPval task packages.

The reviewer-facing unit is one XLSX per person.  The original returned XLSX is
kept immutable; identity, time and credential fields are a separate project-side
transcription that points back to the receipt SHA-256.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

from orchestrator import (Pipeline, PipelineError, PRODUCTION_ROLES,
                          _bundle_manifest, _now, _review_payload_digest,
                          _sha256)
from package import write_archive
from review_contract import ReviewContractError, prepare_review_input


LAYERS = ("general_review", "occupational_expert_review", "final_review")
PHASE1_LAYERS = LAYERS[:2]
REVIEW_ARTIFACTS = {
    "general_review": "general_review_receipt",
    "occupational_expert_review": "occupational_review_receipt",
    "final_review": "final_review_receipt",
}
SUPPLEMENTAL_ARTIFACTS = {
    "general_review": "general_supplemental_review_receipt",
    "occupational_expert_review": "occupational_supplemental_review_receipt",
}
DEFAULT_CHANGE_IMPACT = {
    "general_review": {
        "task_files": {
            "task_meta.json", "prompt.md", "rubric.json", "rubric_pretty.txt",
            "provenance.json", "source_inventory.json", "gold_provenance.json",
            "lineage.json", "expert_profiles.json",
        },
        "artifacts": {
            "references", "prompt", "gold", "gold_provenance", "lineage_draft",
            "rubric", "coverage", "output_contract",
        },
    },
    "occupational_expert_review": {
        "task_files": {
            "task_meta.json", "prompt.md", "rubric.json", "rubric_pretty.txt",
            "expected_values.json", "source_inventory.json", "gold_provenance.json",
            "lineage.json", "expert_profiles.json",
        },
        "artifacts": {
            "coverage", "occupation_standard", "references", "prompt", "gold",
            "gold_provenance", "lineage_draft", "expected_values", "verifier_report",
            "rubric", "output_contract",
        },
    },
}
TRANSCRIPTION_FIELDS = {
    "task_id", "layer", "reviewer_id", "reviewer_title", "reviewed_at",
    "credential_status",
}
HERE = Path(__file__).resolve().parent
BUILDER = HERE / "review_workbooks.mjs"


_REVIEW_UI = {
    "en": {
        "sheet_names": {
            "general_review": "General Review",
            "occupational_expert_review": "Occupation Review",
            "final_review": "Final Review",
            "supplemental_review": "Supplemental Review",
            "findings": "Findings",
            "rubric_gold": "Rubric and Gold",
            "finding_closure": "Finding Closure",
        },
        "labels": {
            "task_id": "Task ID", "rubric_version": "Rubric version",
            "candidate_sha256": "Candidate SHA-256", "conclusion": "Conclusion",
            "opinion": "Substantive opinion", "decision": "Decision",
            "mapping": "Proposed mapping", "boundary": "Boundary",
            "mapping_reason": "Substantive reason", "id": "ID",
        },
        "choices": {
            "pass": "Pass", "conditional_pass": "Conditional pass", "fail": "Fail",
            "issue": "Issue", "na": "N/A", "confirmed": "Confirmed",
            "accept": "Accept", "conditional_accept": "Conditional accept",
            "reject": "Reject", "adopt": "Adopt", "revise": "Revise",
            "yes": "Yes", "no": "No", "blocker": "Blocker",
            "major": "Major", "minor": "Minor",
        },
    },
    "zh": {
        "sheet_names": {
            "general_review": "通用审查",
            "occupational_expert_review": "职业审查",
            "final_review": "最终审查",
            "supplemental_review": "补充复核",
            "findings": "问题记录",
            "rubric_gold": "评分标准与Gold",
            "finding_closure": "问题闭环",
        },
        "labels": {
            "task_id": "任务 ID", "rubric_version": "评分标准版本",
            "candidate_sha256": "候选包 SHA-256", "conclusion": "结论",
            "opinion": "实质意见", "decision": "判断",
            "mapping": "建议职业映射", "boundary": "角色边界",
            "mapping_reason": "判断理由", "id": "编号",
        },
        "choices": {
            "pass": "通过", "conditional_pass": "有条件通过", "fail": "不通过",
            "issue": "有问题", "na": "不适用", "confirmed": "已确认",
            "accept": "接受", "conditional_accept": "有条件接受",
            "reject": "拒绝", "adopt": "采纳", "revise": "修改",
            "yes": "是", "no": "否", "blocker": "阻断",
            "major": "重大", "minor": "轻微",
        },
    },
}


def _review_locale(language):
    value = str(language or "").strip().lower()
    if value.startswith("zh") or value in {"chinese", "中文", "汉语", "普通话"}:
        return "zh"
    return "en"


def _review_ui(meta):
    locale = _review_locale(meta.get("language"))
    return locale, _REVIEW_UI[locale]


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


def _canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _iso_time(value, label):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PipelineError("%s must be ISO-8601" % label) from exc
    if parsed.utcoffset() is None:
        raise PipelineError("%s must include a timezone" % label)
    return parsed


def _task_dir(tasks_root, task_id):
    task = Path(tasks_root).resolve() / task_id
    if not task.is_dir():
        raise PipelineError("task data is missing: %s" % task)
    for name in ("task_meta.json", "rubric.json", "rubric_pretty.txt", "prompt.md"):
        if not (task / name).is_file():
            raise PipelineError("task data is missing %s" % name)
    return task


def _task_data(tasks_root, task_id):
    task = _task_dir(tasks_root, task_id)
    meta = _read_json(task / "task_meta.json")
    rubric = _read_json(task / "rubric.json")
    codes = meta.get("item_codes")
    if not isinstance(codes, list) or len(codes) != len(rubric):
        codes = ["R%02d" % (index + 1) for index in range(len(rubric))]
    return task, meta, rubric, codes


def _rubric_snapshot(rubric, codes):
    result = {}
    for code, item in zip(codes, rubric):
        result[code] = {
            "rubric_item_id": item.get("rubric_item_id"),
            "digest": _canonical_digest({
                key: item.get(key) for key in
                ("rubric_item_id", "score", "required", "criterion", "verification")
            }),
            "max_score": item.get("score"),
        }
    return result


def production_basis(pipeline, tasks_root):
    state = pipeline._load()
    missing = [role for role in PRODUCTION_ROLES
               if not pipeline._current_run(state, role)]
    if missing:
        raise PipelineError("review kit requires current roles: %s" %
                            ", ".join(missing))
    task, meta, rubric, codes = _task_data(tasks_root, state["task_id"])
    artifacts = {name: value["digest"] for name, value in state["artifacts"].items()
                 if name not in set(REVIEW_ARTIFACTS.values()) |
                 set(SUPPLEMENTAL_ARTIFACTS.values()) |
                 {"candidate_delivery_package", "phase1_review_kit",
                  "supplemental_review_kit",
                  "final_review_package", "review_remediation",
                  "pre_final_validation_evidence", "validation_evidence",
                  "human_review_record"}}
    task_files = []
    for name in ("task_meta.json", "prompt.md", "rubric.json",
                 "rubric_pretty.txt", "expected_values.json", "provenance.json",
                 "source_inventory.json", "gold_provenance.json", "lineage.json",
                 "expert_profiles.json"):
        path = task / name
        if path.is_file():
            task_files.append({"path": name, "sha256": _sha256(path),
                               "bytes": path.stat().st_size})
    basis = {
        "task_id": state["task_id"],
        "artifacts": artifacts,
        "task_files": task_files,
        "sector": meta.get("sector"),
        "occupation": meta.get("occupation"),
        "language": meta.get("language"),
        "rubric_version": meta.get("rubric_version", "unversioned"),
        "rubric_sha256": _sha256(task / "rubric.json"),
        "prompt_sha256": _sha256(task / "prompt.md"),
        "rubric_snapshot": _rubric_snapshot(rubric, codes),
    }
    basis["digest"] = _canonical_digest(basis)
    return basis


def _basis_changes(previous, current):
    previous_files = {item["path"]: item["sha256"]
                      for item in previous.get("task_files") or []}
    current_files = {item["path"]: item["sha256"]
                     for item in current.get("task_files") or []}
    changed_files = {name for name in set(previous_files) | set(current_files)
                     if previous_files.get(name) != current_files.get(name)}
    previous_artifacts = previous.get("artifacts") or {}
    current_artifacts = current.get("artifacts") or {}
    changed_artifacts = {
        name for name in set(previous_artifacts) | set(current_artifacts)
        if previous_artifacts.get(name) != current_artifacts.get(name)
    }
    return changed_files, changed_artifacts


def _review_change_impacts(previous, current, policy):
    changed_files, changed_artifacts = _basis_changes(previous, current)
    configured = ((policy.get("human_review") or {})
                  .get("change_impact_layers") or {})
    impacts = {}
    for layer in PHASE1_LAYERS:
        rules = configured.get(layer) or DEFAULT_CHANGE_IMPACT[layer]
        task_files = set(rules.get("task_files") or [])
        artifacts = set(rules.get("artifacts") or [])
        labels = (["task_file:" + name for name in sorted(changed_files & task_files)] +
                  ["artifact:" + name for name in
                   sorted(changed_artifacts & artifacts)])
        if labels:
            impacts[layer] = labels
    return impacts


def _copy_file(source, target):
    source = Path(source)
    if not source.is_file() or source.stat().st_size == 0:
        raise PipelineError("review material is missing or empty: %s" % source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))


def _copy_registered_artifact(pipeline, category, destination):
    artifact = pipeline._load()["artifacts"].get(category)
    if not artifact:
        return False
    source = pipeline.root / artifact["path"]
    shutil.copytree(str(source), str(destination))
    return True


def _candidate_snapshot(task_id, task, meta, delivery_root, staging_root,
                        destination):
    destination.mkdir(parents=True)
    delivery_root = Path(delivery_root).resolve()
    tasks_jsonl = delivery_root / "tasks.jsonl"
    if not tasks_jsonl.is_file():
        raise PipelineError("candidate delivery is missing tasks.jsonl: %s" %
                            tasks_jsonl)
    records = [json.loads(line) for line in
               tasks_jsonl.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    matches = [item for item in records if item.get("task_id") == task_id]
    if len(matches) != 1:
        raise PipelineError("candidate delivery has no task %s" % task_id)
    record = matches[0]
    try:
        delivered_rubric = json.loads(record.get("rubric_json", ""))
    except (TypeError, ValueError) as exc:
        raise PipelineError("candidate delivery rubric_json is invalid") from exc
    expected = {
        "sector": meta.get("sector"),
        "occupation": meta.get("occupation"),
        "prompt": (task / "prompt.md").read_text(encoding="utf-8").rstrip("\n"),
        "rubric_pretty": (task / "rubric_pretty.txt").read_text(
            encoding="utf-8").rstrip("\n"),
    }
    mismatched = [name for name, value in expected.items()
                  if record.get(name) != value]
    if delivered_rubric != _read_json(task / "rubric.json"):
        mismatched.append("rubric_json")
    if mismatched:
        raise PipelineError(
            "candidate delivery does not match task data: %s" %
            ", ".join(sorted(mismatched)))
    (destination / "tasks.jsonl").write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    inventory = []
    for kind in ("reference_files", "deliverable_files"):
        declared = record.get(kind) or []
        for rel in declared:
            relpath = Path(rel)
            if relpath.is_absolute() or ".." in relpath.parts:
                raise PipelineError("candidate delivery contains an unsafe path: %s" % rel)
            source = delivery_root / relpath
            _copy_file(source, destination / relpath)
            inventory.append({
                "kind": kind, "path": relpath.as_posix(),
                "sha256": _sha256(source), "bytes": source.stat().st_size,
            })
    _write_json(destination / "manifests" / "candidate_manifest.json", {
        "task_id": task_id,
        "sector": meta.get("sector"),
        "occupation": meta.get("occupation"),
        "rubric_version": meta.get("rubric_version"),
        "files": inventory,
    })


def _run_builder(config, output, node, node_modules):
    node = Path(node).resolve()
    node_modules = Path(node_modules).resolve()
    if not node.is_file():
        raise PipelineError("Node executable is missing: %s" % node)
    if not node_modules.is_dir():
        raise PipelineError("bundled node_modules is missing: %s" % node_modules)
    with tempfile.TemporaryDirectory(prefix="gdpval-review-xlsx-") as raw:
        work = Path(raw)
        shutil.copy2(str(BUILDER), str(work / BUILDER.name))
        os.symlink(str(node_modules), str(work / "node_modules"),
                   target_is_directory=True)
        config_path = work / "config.json"
        _write_json(config_path, config)
        proc = subprocess.run(
            [str(node), str(work / BUILDER.name), "--config", str(config_path),
             "--output", str(Path(output).resolve())],
            cwd=str(work), capture_output=True, text=True)
        if proc.returncode:
            raise PipelineError("review workbook build failed: %s" %
                                (proc.stderr.strip() or proc.stdout.strip()))


def _base_task_config(meta, basis, candidate_sha):
    locale = _review_locale(meta.get("language"))
    return {
        "task_id": basis["task_id"],
        "sector": meta.get("sector", ""),
        "occupation": meta.get("occupation", ""),
        "language": ("中文" if locale == "zh" else
                     (meta.get("language", "") or "English")),
        "rubric_version": basis["rubric_version"],
        "candidate_sha256": candidate_sha,
    }


def _role_review_brief(input_manifest, layer):
    if not input_manifest:
        return None
    profile = next(item for item in input_manifest["expert_profiles"]
                   if item["review_layer"] == layer)
    safe_profile = {name: profile.get(name) for name in
                    ("required_industry", "required_occupation", "review_scope",
                     "expert_profile", "strengths", "first_thought")}
    task_files = {"task_meta.json", "prompt.md", "rubric.json",
                  "rubric_pretty.txt", "provenance.json", "gold_provenance.json",
                  "source_inventory.json"}
    files = [item for item in input_manifest["files"]
             if item["scope"] != "task_input" or item["path"] in task_files]
    return {
        "task": input_manifest["task"], "reviewer_profile": safe_profile,
        "rights": input_manifest["rights"],
        "deliverable_sources": input_manifest["deliverable_sources"],
        "reference_sources": input_manifest["reference_sources"],
        "files": files,
    }


def _phase1_configs(meta, rubric, codes, basis, candidate_sha,
                    input_manifest=None):
    task = _base_task_config(meta, basis, candidate_sha)
    locale, ui = _review_ui(meta)
    general_checks = {
        "en": [
            "All required files open and the package inventory matches the supplied files.",
            "Agent-visible references contain no Gold, expected values, rubric execution or validation evidence.",
            "Prompt, rubric and file language are internally consistent and contain no placeholders.",
            "The task is solvable from the supplied references without external research or invented facts.",
            "Source URLs, provenance, usage scope and redistribution restrictions are clearly disclosed.",
            "No personal data, secret, local path, hidden answer or unsupported credential claim is exposed.",
            "Deliverables render legibly without clipping, overlap, blank pages or broken formulas.",
            "Any issue is recorded once in the Findings sheet with severity, location and recommendation.",
        ],
        "zh": [
            "所有必需文件均可打开，包内文件清单与实际文件一致。",
            "审核者可见的参考文件不含 Gold、预期值、评分标准执行过程或验证证据。",
            "任务说明、评分标准和文件语言一致，且不含占位内容。",
            "仅使用随包参考文件即可完成任务，无需外部检索或虚构事实。",
            "来源链接、溯源、使用范围和再分发限制均已清楚披露。",
            "未暴露个人数据、秘密、本地路径、隐藏答案或无依据的资质声明。",
            "交付物显示清晰，无裁切、重叠、空白页或公式损坏。",
            "如发现问题，仅在问题记录表登记一次，并填写严重度、位置和建议。",
        ],
    }[locale]
    general = {
        "layer": "general_review",
        "title": "GDPval General Review" if locale == "en" else "GDPval 通用审查",
        "locale": locale, "ui": ui,
        "task": task,
        "brief": _role_review_brief(input_manifest, "general_review"),
        "checklist": [{"id": "G%02d" % (index + 1), "text": text}
                      for index, text in enumerate(general_checks)],
    }
    items = []
    for code, item in zip(codes, rubric):
        check = item.get("check") or {}
        machine = "human" if locale == "en" else "人工审核"
        if isinstance(check, dict) and not check.get("human"):
            machine = check.get("type") or (
                "machine-checkable" if locale == "en" else "可由机器核对")
        items.append({
            "code": code,
            "rubric_item_id": item.get("rubric_item_id"),
            "required": item.get("required", True),
            "max_score": item.get("score"),
            "criterion": item.get("criterion", ""),
            "verification": item.get("verification", ""),
            "machine_result": machine,
        })
    occupational_checks = {
        "en": [
            "The proposed industry and occupation mapping is professionally reasonable within the stated role boundary.",
            "The references support the professional decisions required by the prompt without unsupported assumptions.",
            "Terms, calculations, exceptions and operational boundaries match normal professional practice.",
            "The Gold is usable in the stated work context and does not overclaim authority, approval or credentials.",
            "Every rubric row has been independently adopted, revised or rejected and scored against the Gold.",
        ],
        "zh": [
            "建议的行业和职业映射在既定角色边界内符合专业实际。",
            "参考文件足以支持任务所需的专业判断，无需无依据的假设。",
            "术语、计算、例外情况和操作边界符合通常的专业实践。",
            "Gold 可用于所述工作场景，且未夸大权限、批准状态或资质。",
            "已独立判断每条评分标准应采纳、修改或拒绝，并依据 Gold 完成评分。",
        ],
    }[locale]
    occupational = {
        "layer": "occupational_expert_review",
        "title": ("GDPval Occupational Expert Review" if locale == "en" else
                  "GDPval 职业专家审查"),
        "locale": locale, "ui": ui,
        "task": task,
        "brief": _role_review_brief(input_manifest, "occupational_expert_review"),
        "mapping": {
            "proposed": "%s / %s" % (meta.get("sector", ""), meta.get("occupation", "")),
            "boundary": ((meta.get("guards") or {}).get("occupation_boundary") or
                         ("Accept only within the task role and authority described by the prompt."
                          if locale == "en" else
                          "仅在任务说明规定的角色和权限边界内接受该映射。")),
        },
        "checklist": [{"id": "E%02d" % (index + 1), "text": text}
                      for index, text in enumerate(occupational_checks)],
        "rubrics": items,
    }
    return general, occupational


def _phase1_requirements(cycle):
    """Normalize every actionable phase-1 decision into one remediation list."""
    result = []
    if cycle.get("status") == "supplemental_review_failed":
        supplemental = ((cycle.get("remediation") or {})
                        .get("supplemental_receipts") or {})
        prefixes = {"general_review": "G", "occupational_expert_review": "E"}
        for layer, receipt in supplemental.items():
            record = receipt.get("record") or {}
            blocking = [item for item in record.get("items") or []
                        if item.get("decision") == "Issue" or
                        item.get("adoption") in ("Revise", "Reject")]
            for item in blocking:
                result.append({
                    "requirement_id": "SUP-%s-%s" % (
                        prefixes[layer], item["requirement_id"]),
                    "source_layer": layer, "kind": "supplemental_issue",
                    "summary": item.get("comment") or
                               "Resolve the issue raised in supplemental review.",
                    "requires_confirmation": True,
                    "source_reviewed_at": receipt["reviewed_at"],
                    "rubric_code": item.get("rubric_code"),
                })
            if record.get("verdict") != "Pass" and not blocking:
                result.append({
                    "requirement_id": "SUP-%s-VERDICT" % prefixes[layer],
                    "source_layer": layer, "kind": "supplemental_verdict",
                    "summary": record.get("opinion") or
                               "Resolve the failed supplemental verdict.",
                    "requires_confirmation": True,
                    "source_reviewed_at": receipt["reviewed_at"],
                })
        return result
    receipts = (cycle.get("phase1") or {}).get("receipts") or {}
    prefixes = {"general_review": "G", "occupational_expert_review": "E"}
    for layer in PHASE1_LAYERS:
        receipt = receipts.get(layer) or {}
        record = receipt.get("record") or {}
        for finding in record.get("findings") or []:
            result.append({
                "requirement_id": finding["finding_id"],
                "source_layer": layer, "kind": "finding",
                "summary": finding.get("issue") or finding.get("recommendation"),
                "requires_confirmation": bool(
                    finding.get("requires_confirmation")),
            })
        if record.get("verdict") not in (None, "Pass"):
            result.append({
                "requirement_id": prefixes[layer] + "-VERDICT",
                "source_layer": layer, "kind": "verdict",
                "summary": "Resolve the conditions behind the phase-1 %s verdict." %
                           record.get("verdict"),
                "requires_confirmation": True,
            })
        if layer == "occupational_expert_review":
            mapping = record.get("occupation_mapping_decision")
            if mapping not in (None, "Accept"):
                result.append({
                    "requirement_id": "E-MAPPING",
                    "source_layer": layer, "kind": "occupation_mapping",
                    "summary": record.get("occupation_mapping_reason") or
                               "Resolve the occupational mapping condition.",
                    "requires_confirmation": True,
                })
            for row in record.get("rubric_items") or []:
                if row.get("adoption") == "Adopt":
                    continue
                result.append({
                    "requirement_id": "E-RUBRIC-" + row["code"],
                    "source_layer": layer, "kind": "rubric_item",
                    "rubric_code": row["code"],
                    "summary": row.get("reason_or_revision") or
                               "Revise the rubric item as directed.",
                    "requires_confirmation": True,
                })
    ids = [item["requirement_id"] for item in result]
    if len(ids) != len(set(ids)):
        raise PipelineError("phase-1 review produced duplicate remediation IDs")
    return result


def _supplemental_requirements(cycle, basis, rubric, codes, source_layer,
                               locale="en"):
    remediation = cycle["remediation"]
    items = [dict(item) for item in remediation["requirements"]
             if item["source_layer"] == source_layer and
             item.get("requires_confirmation")]
    changed_dependencies = (remediation.get("change_impacts") or {}).get(
        source_layer) or []
    if changed_dependencies:
        prefix = "G" if source_layer == "general_review" else "E"
        items.append({
            "requirement_id": prefix + "-DEPENDENCY-CHANGE",
            "source_layer": source_layer,
            "kind": "review_dependency_change",
            "summary": (("Confirm the current versions of these changed review inputs: "
                         if locale == "en" else
                         "请确认以下已变更审核输入的当前版本：") +
                        ", ".join(changed_dependencies)),
            "requires_confirmation": True,
            "disposition": "closed",
            "rationale": ("The dependency matrix routed only changed inputs to this layer."
                          if locale == "en" else
                          "依赖矩阵仅将本层实际受影响的输入纳入复核。"),
            "closed_at": remediation["recorded_at"],
            "evidence_sha256": {},
        })
    if source_layer == "occupational_expert_review":
        initial = remediation.get("from_basis") or cycle["initial_basis"]
        initial_snapshot = initial.get("rubric_snapshot") or {}
        current_snapshot = basis.get("rubric_snapshot") or {}
        gold_changed = ((initial.get("artifacts") or {}).get("gold") !=
                        (basis.get("artifacts") or {}).get("gold"))
        existing_codes = {item.get("rubric_code") for item in items}
        for code, rubric_item in zip(codes, rubric):
            changed = (gold_changed or code not in initial_snapshot or
                       initial_snapshot.get(code, {}).get("digest") !=
                       current_snapshot.get(code, {}).get("digest"))
            if changed and code not in existing_codes:
                items.append({
                    "requirement_id": "E-CHANGED-" + code,
                    "source_layer": source_layer,
                    "kind": "rubric_or_gold_change", "rubric_code": code,
                    "summary": (("Re-score this row because the Gold changed." if
                                 gold_changed else
                                 "Adopt and score the changed current rubric row.")
                                if locale == "en" else
                                ("Gold 已变更，请重新评定本行分数。" if gold_changed else
                                 "请采纳并评分当前已变更的评分标准行。")),
                    "requires_confirmation": True,
                    "disposition": "closed",
                    "rationale": ("Included automatically by the change-impact check."
                                  if locale == "en" else
                                  "变更影响检查已自动纳入此项。"),
                    "closed_at": remediation["recorded_at"],
                    "evidence_sha256": {},
                })
    by_code = {code: item for code, item in zip(codes, rubric)}
    zh_fallbacks = {
        "Resolve the issue raised in supplemental review.": "解决补充复核提出的问题。",
        "Resolve the failed supplemental verdict.": "解决补充复核未通过所涉及的问题。",
        "Resolve the general review verdict before release.": "发布前解决通用审查结论涉及的问题。",
        "Resolve the occupational review verdict before release.": "发布前解决职业审查结论涉及的问题。",
        "Resolve the occupational mapping condition.": "解决职业映射的附带条件。",
        "Revise the rubric item as directed.": "按审核意见修改该评分项。",
    }
    zh_kinds = {
        "finding": "问题", "verdict": "审查结论", "mapping": "职业映射",
        "rubric_item": "评分项", "supplemental_issue": "补充复核问题",
        "supplemental_verdict": "补充复核结论",
        "rubric_or_gold_change": "评分项或 Gold 变更",
        "review_dependency_change": "审核依赖变更",
    }
    for item in items:
        if locale == "zh":
            item["summary"] = zh_fallbacks.get(item.get("summary"),
                                                item.get("summary"))
        item["kind_display"] = (zh_kinds.get(item.get("kind"), item.get("kind"))
                                if locale == "zh" else item.get("kind"))
        code = item.get("rubric_code")
        if code:
            current = by_code.get(code)
            if current is None:
                raise PipelineError(
                    "supplemental review references removed rubric code %s" % code)
            item["rubric"] = {
                "code": code, "rubric_item_id": current.get("rubric_item_id"),
                "criterion": current.get("criterion", ""),
                "verification": current.get("verification", ""),
                "max_score": current.get("score"),
            }
        evidence = "; ".join("%s=%s" % pair for pair in sorted(
            (item.get("evidence_sha256") or {}).items())) or (
                "Bound by basis digest" if locale == "en" else "由基线摘要绑定")
        details = (["Resolution: %s" % item.get("rationale", ""),
                    "Closed: %s" % item.get("closed_at", ""),
                    "Evidence: %s" % evidence] if locale == "en" else
                   ["整改说明：%s" % item.get("rationale", ""),
                    "关闭时间：%s" % item.get("closed_at", ""),
                    "证据：%s" % evidence])
        if item.get("rubric"):
            details.insert(1, ("Current rubric: %s" if locale == "en" else
                               "当前评分标准：%s") % item["rubric"]["criterion"])
            details.insert(2, ("Verification: %s" if locale == "en" else
                               "验证方式：%s") % item["rubric"]["verification"])
        item["remediation_display"] = "\n".join(details)
    return items


def _supplemental_config(meta, rubric, codes, basis, candidate_sha, cycle,
                         source_layer, input_manifest=None):
    locale, ui = _review_ui(meta)
    return {
        "layer": "supplemental_review",
        "source_layer": source_layer,
        "title": (("GDPval General Review - Changed Items" if
                   source_layer == "general_review" else
                   "GDPval Occupational Review - Changed Items")
                  if locale == "en" else
                  ("GDPval 通用审查 - 变更项复核" if
                   source_layer == "general_review" else
                   "GDPval 职业审查 - 变更项复核")),
        "locale": locale, "ui": ui,
        "task": _base_task_config(meta, basis, candidate_sha),
        "brief": _role_review_brief(input_manifest, source_layer),
        "requirements": _supplemental_requirements(
            cycle, basis, rubric, codes, source_layer, locale),
    }


def _all_remediation_requirements(cycle):
    rounds = list(cycle.get("remediation_history") or [])
    if cycle.get("remediation"):
        rounds.append(cycle["remediation"])
    return [item for remediation in rounds
            for item in remediation.get("requirements", [])]


FINAL_CHECKLIST = [
    {"id": "F01", "text": "The two earlier original receipts and their project-side transcriptions match the listed SHA-256 values."},
    {"id": "F02", "text": "Every earlier finding has one supported disposition and no blocker or major finding remains open."},
    {"id": "F03", "text": "The reviewed rubric version, Gold marking and current package basis are mutually consistent."},
    {"id": "F04", "text": "Pre-final validation has no failed check; any not-run item is limited to this final review."},
    {"id": "F05", "text": "The final verdict does not expand the declared occupation, authority, credential, licence or redistribution boundary."},
]


def _final_config(meta, basis, candidate_sha, cycle, input_manifest=None):
    locale, ui = _review_ui(meta)
    first = cycle["phase1"]["receipts"]
    evidence_labels = ({
        "general_receipt": "General receipt", "general_time": "General reviewed at",
        "expert_receipt": "Expert receipt", "expert_time": "Expert reviewed at",
        "basis": "Post-remediation basis", "validation_time": "Pre-final validation time",
        "validation": "Pre-final validation",
    } if locale == "en" else {
        "general_receipt": "通用审查回执", "general_time": "通用审查时间",
        "expert_receipt": "职业专家回执", "expert_time": "职业专家审查时间",
        "basis": "整改后基线", "validation_time": "终审前验证时间",
        "validation": "终审前验证",
    })
    evidence = [
        {"label": evidence_labels["general_receipt"], "value": first["general_review"]["source_receipt_sha256"]},
        {"label": evidence_labels["general_time"], "value": first["general_review"]["reviewed_at"]},
        {"label": evidence_labels["expert_receipt"], "value": first["occupational_expert_review"]["source_receipt_sha256"]},
        {"label": evidence_labels["expert_time"], "value": first["occupational_expert_review"]["reviewed_at"]},
    ]
    supplemental = (cycle.get("remediation") or {}).get("supplemental_receipts") or {}
    for layer in PHASE1_LAYERS:
        if supplemental.get(layer):
            evidence.extend([
                {"label": (layer + " supplemental receipt" if locale == "en" else
                           ("通用补充复核回执" if layer == "general_review" else
                            "职业补充复核回执")),
                 "value": supplemental[layer]["source_receipt_sha256"]},
                {"label": (layer + " supplemental reviewed at" if locale == "en" else
                           ("通用补充复核时间" if layer == "general_review" else
                            "职业补充复核时间")),
                 "value": supplemental[layer]["reviewed_at"]},
            ])
    evidence.extend([
        {"label": evidence_labels["basis"], "value": basis["digest"]},
        {"label": evidence_labels["validation_time"], "value": cycle["pre_final_validation"]["run_at"]},
        {"label": evidence_labels["validation"], "value": cycle["pre_final_validation"]["evidence_digest"]},
    ])
    checklist = FINAL_CHECKLIST if locale == "en" else [
        {"id": "F01", "text": "两份前序原始回执及项目侧录入信息与所列 SHA-256 一致。"},
        {"id": "F02", "text": "每项前序问题均有证据支持的处理结果，且没有未关闭的阻断或重大问题。"},
        {"id": "F03", "text": "已审评分标准版本、Gold 评分和当前包基线相互一致。"},
        {"id": "F04", "text": "终审前验证没有失败项；未运行项仅限本次最终审查。"},
        {"id": "F05", "text": "最终结论未扩大已声明的职业、权限、资质、许可或再分发边界。"},
    ]
    return {
        "layer": "final_review",
        "title": "GDPval Final Review" if locale == "en" else "GDPval 最终审查",
        "locale": locale, "ui": ui,
        "task": _base_task_config(meta, basis, candidate_sha),
        "brief": _role_review_brief(input_manifest, "final_review"),
        "final_evidence": evidence,
        "checklist": checklist,
        "finding_closure": [{
            "finding_id": item["requirement_id"],
            "source_layer": item["source_layer"],
            "disposition": item["disposition"], "closed_at": item["closed_at"],
            "rationale": item["rationale"],
            "evidence_sha256": "; ".join(
                "%s=%s" % pair for pair in sorted(
                    (item.get("evidence_sha256") or {}).items())),
        } for item in _all_remediation_requirements(cycle)],
    }


def _write_package(tree, output, prefix):
    return write_archive(str(tree), str(output), prefix)


def create_phase1(pipeline, delivery, tasks_root, staging_root, output_dir,
                  node, node_modules):
    state = pipeline._load()
    basis = production_basis(pipeline, tasks_root)
    task, meta, rubric, codes = _task_data(tasks_root, state["task_id"])
    input_manifest = prepare_review_input(
        task, Path(delivery).resolve(), state["task_id"], pipeline.policy, basis,
        state.get("artifacts", {}).get("occupation_standard"))
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gdpval-phase1-kit-") as raw:
        tmp = Path(raw)
        candidate = tmp / "Candidate-Delivery-Package"
        _candidate_snapshot(state["task_id"], task, meta, delivery,
                            staging_root, candidate)
        candidate_zip = output_dir / "Candidate-Delivery-Package.zip"
        candidate_info = _write_package(candidate, candidate_zip,
                                        "Candidate-Delivery-Package")
        general_cfg, expert_cfg = _phase1_configs(
            meta, rubric, codes, basis, candidate_info["sha256"], input_manifest)

        kit = tmp / "Phase-1-Human-Review-Kit"
        kit.mkdir()
        role_archives = {}
        for label, config, filename in (
                ("general_review", general_cfg, "General-Review.xlsx"),
                ("occupational_expert_review", expert_cfg,
                 "Occupational-Expert-Review.xlsx")):
            role = tmp / ("General-Review-Package" if label == "general_review"
                          else "Occupational-Expert-Review-Package")
            role.mkdir()
            _run_builder(config, role / filename, node, node_modules)
            shutil.copytree(
                str(candidate),
                str(role / "Read-Only-Materials" / "Candidate-Delivery"))
            evidence_root = role / "Read-Only-Materials" / "Review-Evidence"
            brief = _role_review_brief(input_manifest, label)
            _write_json(evidence_root / "review_brief.json", brief)
            for name in ("provenance.json", "source_inventory.json",
                         "gold_provenance.json", "lineage.json"):
                if (task / name).is_file():
                    _copy_file(task / name, evidence_root / name)
            if label == "general_review":
                for name in ("prompt.md", "rubric_pretty.txt"):
                    if (task / name).is_file():
                        _copy_file(task / name, role / "Read-Only-Materials" / name)
            else:
                _copy_registered_artifact(
                    pipeline, "occupation_standard",
                    evidence_root / "occupation_standard")
                for name in ("prompt.md", "rubric.json", "rubric_pretty.txt",
                             "expected_values.json"):
                    if (task / name).is_file():
                        _copy_file(task / name, role / "Read-Only-Materials" / name)
            archive = kit / (role.name + ".zip")
            role_archives[label] = _write_package(role, archive, role.name)

        _write_json(kit / "review_kit_manifest.json", {
            "schema_version": "staged-xlsx-v1",
            "task_id": state["task_id"], "basis_digest": basis["digest"],
            "candidate_sha256": candidate_info["sha256"],
            "review_packages": {name: value["sha256"]
                                for name, value in role_archives.items()},
            "return_contract": "one completed XLSX per reviewer",
        })
        _write_json(kit / "review_input_manifest.json", input_manifest)
        kit_zip = output_dir / "Phase-1-Human-Review-Kit.zip"
        kit_info = _write_package(kit, kit_zip, kit.name)

    candidate_artifact = pipeline.add_artifact(
        "candidate_delivery_package", [candidate_zip], "review-kit")
    kit_artifact = pipeline.add_artifact("phase1_review_kit", [kit_zip], "review-kit")
    state = pipeline._load()
    state["review_cycle"] = {
        "cycle_id": str(uuid4()), "status": "awaiting_phase1_reviews",
        "input_contract_schema": input_manifest["schema_version"],
        "created_at": _now(), "initial_basis": basis,
        "candidate_delivery": {
            "sha256": candidate_info["sha256"],
            "artifact_digest": candidate_artifact["digest"],
        },
        "phase1": {
            "kit_sha256": kit_info["sha256"],
            "artifact_digest": kit_artifact["digest"],
            "packages": {name: value["sha256"] for name, value in role_archives.items()},
            "receipts": {},
        },
        "remediation": None,
        "pre_final_validation": None,
        "final": {"package": None, "receipt": None},
    }
    pipeline._save(state)
    return {
        "candidate_delivery_package": str(candidate_zip),
        "human_review_kit": str(kit_zip),
        "basis_digest": basis["digest"],
    }


def _validate_identity(record):
    missing = [name for name in ("reviewer_id", "reviewer_title", "reviewed_at")
               if not str(record.get(name) or "").strip()]
    if missing:
        raise PipelineError("project-side transcription is missing: %s" %
                            ", ".join(missing))
    _iso_time(record["reviewed_at"], "reviewed_at")
    if not record.get("credential_status"):
        record["credential_status"] = "not_supplied"
    if record["credential_status"] not in ("not_supplied", "supplied_unverified",
                                            "verified"):
        raise PipelineError("invalid credential_status")


def _validate_xlsx_container(path):
    if not zipfile.is_zipfile(path):
        raise PipelineError("returned review file is not a valid XLSX container")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required.issubset(names):
            raise PipelineError("returned review XLSX is missing workbook parts")
        if any(name.lower().endswith("vbaproject.bin") for name in names):
            raise PipelineError("macro-enabled reviewer receipts are not accepted")


_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx_text(node):
    return "".join(value.text or "" for value in
                   node.findall(".//{%s}t" % _SHEET_NS))


def _xlsx_cells(path):
    """Read values from the fixed reviewer workbook without trusting formulas."""
    with zipfile.ZipFile(path) as archive:
        try:
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(
                archive.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ElementTree.ParseError) as exc:
            raise PipelineError("returned review XLSX has invalid workbook XML") from exc
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [_xlsx_text(item)
                          for item in root.findall("{%s}si" % _SHEET_NS)]
            except ElementTree.ParseError as exc:
                raise PipelineError("returned review XLSX has invalid shared strings") from exc
        targets = {
            item.attrib.get("Id"): item.attrib.get("Target")
            for item in relationships.findall("{%s}Relationship" % _PKG_REL_NS)
        }
        result = {}
        for sheet in workbook.findall(".//{%s}sheet" % _SHEET_NS):
            name = sheet.attrib.get("name")
            target = targets.get(sheet.attrib.get("{%s}id" % _REL_NS))
            if not name or not target:
                raise PipelineError("returned review XLSX has an unresolved sheet")
            target_path = Path(target)
            if target_path.is_absolute():
                if not target.startswith("/xl/"):
                    raise PipelineError(
                        "returned review XLSX has an unsafe sheet target")
                normalized = target.lstrip("/")
            else:
                normalized = Path("xl", target_path).as_posix()
            if ".." in Path(normalized).parts:
                raise PipelineError("returned review XLSX has an unsafe sheet target")
            try:
                root = ElementTree.fromstring(archive.read(normalized))
            except (KeyError, ElementTree.ParseError) as exc:
                raise PipelineError("returned review XLSX has invalid sheet XML") from exc
            cells = {}
            for cell in root.findall(".//{%s}c" % _SHEET_NS):
                address = cell.attrib.get("r")
                if not address:
                    continue
                kind = cell.attrib.get("t")
                if kind == "inlineStr":
                    value = _xlsx_text(cell)
                else:
                    raw = cell.find("{%s}v" % _SHEET_NS)
                    value = "" if raw is None else (raw.text or "")
                    if kind == "s" and value:
                        try:
                            value = shared[int(value)]
                        except (IndexError, ValueError) as exc:
                            raise PipelineError(
                                "returned review XLSX has an invalid shared-string index") from exc
                    elif kind == "b":
                        value = value == "1"
                    elif kind not in ("str", "e") and value:
                        try:
                            number = float(value)
                            value = int(number) if number.is_integer() else number
                        except ValueError:
                            pass
                cells[address] = value
            result[name] = cells
        return result


def _cell(cells, address):
    value = cells.get(address, "")
    return value.strip() if isinstance(value, str) else value


def _row_for_label(cells, label):
    for address, value in cells.items():
        if address.startswith("A") and value == label:
            return int(address[1:])
    raise PipelineError("returned review XLSX is missing label: %s" % label)


def _metadata(cells, ui):
    labels = ui["labels"]
    return {label: _cell(cells, "B%d" % _row_for_label(cells, label))
            for label in (labels["task_id"], labels["rubric_version"],
                          labels["candidate_sha256"])}


def _canonical_choice(value, ui):
    for canonical, display in ui["choices"].items():
        if value == display:
            return canonical
    return None


def _findings(sheets, ui):
    cells = sheets.get(ui["sheet_names"]["findings"])
    if cells is None:
        raise PipelineError("returned review XLSX is missing the Findings sheet")
    result = []
    for row in range(4, 24):
        values = [_cell(cells, "%s%d" % (column, row))
                  for column in "ABCDEF"]
        finding_id, severity, location, issue, recommendation, confirmation = values
        if not any(values[1:5]):
            if confirmation not in ("", ui["choices"]["no"]):
                raise PipelineError("unused finding rows must keep confirmation=No")
            continue
        if finding_id not in {"%s-F%02d" % (prefix, index)
                              for prefix in ("G", "E")
                              for index in range(1, 21)}:
            raise PipelineError("finding ID was altered: %s" % finding_id)
        severity_key = _canonical_choice(severity, ui)
        if severity_key not in ("blocker", "major", "minor"):
            raise PipelineError("%s has an invalid severity" % finding_id)
        if not all(str(value).strip() for value in
                   (finding_id, location, issue, recommendation)):
            raise PipelineError("%s is incomplete" % (finding_id or "finding"))
        confirmation_key = _canonical_choice(confirmation, ui)
        if confirmation_key not in ("yes", "no"):
            raise PipelineError("%s has an invalid confirmation decision" % finding_id)
        result.append({
            "finding_id": finding_id, "severity": severity_key.title(),
            "location": location, "issue": issue,
            "recommendation": recommendation,
            "requires_confirmation": confirmation_key == "yes",
        })
    return result


def _check_rows(cells, prefix, decisions, ui):
    result = []
    for address, value in cells.items():
        if not address.startswith("A") or not isinstance(value, str) \
                or not value.startswith(prefix) or not value[len(prefix):].isdigit():
            continue
        row = int(address[1:])
        display_decision = _cell(cells, "C%d" % row)
        decision = _canonical_choice(display_decision, ui)
        if decision not in decisions:
            raise PipelineError("%s has no valid workbook decision" % value)
        comment = _cell(cells, "D%d" % row)
        if decision == "na" and not comment:
            raise PipelineError("%s marked N/A needs a short reason" % value)
        result.append({
            "id": value, "decision": {
                "pass": "Pass", "issue": "Issue", "na": "N/A",
                "confirmed": "Confirmed",
            }[decision],
            "comment": comment,
        })
    return sorted(result, key=lambda item: item["id"])


def _return_fields(cells, ui):
    labels = ui["labels"]
    verdict_key = _canonical_choice(
        _cell(cells, "B%d" % _row_for_label(cells, labels["conclusion"])), ui)
    opinion = _cell(cells, "B%d" % _row_for_label(cells, labels["opinion"]))
    if verdict_key not in ("pass", "conditional_pass", "fail"):
        raise PipelineError("returned review XLSX has no valid conclusion")
    if not opinion:
        raise PipelineError("returned review XLSX needs a substantive opinion")
    return {"pass": "Pass", "conditional_pass": "Conditional pass",
            "fail": "Fail"}[verdict_key], opinion


def _display_literal(value):
    if isinstance(value, str) and len(value) >= 11 and value[4:5] == "-" \
            and value[10:11] == "T":
        return "ISO-8601 " + value
    return value


def _parse_review_workbook(layer, path, task_id, meta, rubric, codes,
                           expected_candidate_sha, expected_config):
    sheets = _xlsx_cells(path)
    ui = expected_config["ui"]
    main_name = ui["sheet_names"][layer]
    cells = sheets.get(main_name)
    if cells is None:
        raise PipelineError("returned review XLSX is missing sheet: %s" % main_name)
    labels = ui["labels"]
    expected_meta = {
        labels["task_id"]: task_id,
        labels["rubric_version"]: meta.get("rubric_version", "unversioned"),
        labels["candidate_sha256"]: expected_candidate_sha,
    }
    if _metadata(cells, ui) != expected_meta:
        raise PipelineError(
            "returned review XLSX metadata does not match the frozen review package")
    verdict, opinion = _return_fields(cells, ui)
    parsed = {"verdict": verdict, "opinion": opinion}
    if layer == "general_review":
        checklist = _check_rows(cells, "G", {"pass", "issue", "na"}, ui)
        expected_checks = expected_config["checklist"]
        if ([item["id"] for item in checklist] !=
                [item["id"] for item in expected_checks]):
            raise PipelineError("general review checklist is incomplete")
        for actual, expected in zip(checklist, expected_checks):
            row = _row_for_label(cells, actual["id"])
            if _cell(cells, "B%d" % row) != expected["text"]:
                raise PipelineError("general review checklist text was altered")
        findings = _findings(sheets, ui)
        if sum(item["decision"] == "Issue" for item in checklist) != len(findings):
            raise PipelineError("general checklist issues must match Findings rows")
        parsed.update({"checklist": checklist, "findings": findings})
    elif layer == "occupational_expert_review":
        mapping_decision = _cell(
            cells, "B%d" % _row_for_label(cells, labels["decision"]))
        mapping_reason = _cell(
            cells, "B%d" % _row_for_label(cells, labels["mapping_reason"]))
        mapping = expected_config["mapping"]
        if (_cell(cells, "B%d" % _row_for_label(cells, labels["mapping"])) !=
                mapping["proposed"] or
                _cell(cells, "B%d" % _row_for_label(cells, labels["boundary"])) !=
                mapping["boundary"]):
            raise PipelineError("occupational mapping basis was altered")
        mapping_key = _canonical_choice(mapping_decision, ui)
        if mapping_key not in ("accept", "conditional_accept", "reject") \
                or not mapping_reason:
            raise PipelineError(
                "occupational mapping needs a decision and substantive reason")
        mapping_decision = {"accept": "Accept", "conditional_accept":
                            "Conditional accept", "reject": "Reject"}[mapping_key]
        checklist = _check_rows(cells, "E", {"pass", "issue", "na"}, ui)
        expected_checks = expected_config["checklist"]
        if ([item["id"] for item in checklist] !=
                [item["id"] for item in expected_checks]):
            raise PipelineError("occupational checklist is incomplete")
        for actual, expected in zip(checklist, expected_checks):
            row = _row_for_label(cells, actual["id"])
            if _cell(cells, "B%d" % row) != expected["text"]:
                raise PipelineError("occupational checklist text was altered")
        rubric_cells = sheets.get(ui["sheet_names"]["rubric_gold"])
        if rubric_cells is None:
            raise PipelineError("returned expert XLSX is missing Rubric and Gold")
        rows = []
        for index, (code, item) in enumerate(zip(codes, rubric), start=4):
            if (_cell(rubric_cells, "A%d" % index) != code or
                    _cell(rubric_cells, "B%d" % index) != item.get("rubric_item_id") or
                    _cell(rubric_cells, "C%d" % index) != item.get("required", True) or
                    _cell(rubric_cells, "D%d" % index) != item.get("score") or
                    _cell(rubric_cells, "E%d" % index) != item.get("criterion", "") or
                    _cell(rubric_cells, "F%d" % index) !=
                    "%s\n%s: %s" % (item.get("verification", ""),
                                      "Machine" if expected_config["locale"] == "en" else "机器结果",
                                      expected_config["rubrics"][index - 4]["machine_result"])):
                raise PipelineError("expert workbook rubric row %s was altered" % code)
            adoption_display = _cell(rubric_cells, "G%d" % index)
            adoption_key = _canonical_choice(adoption_display, ui)
            reason = _cell(rubric_cells, "H%d" % index)
            score = _cell(rubric_cells, "I%d" % index)
            evidence = _cell(rubric_cells, "J%d" % index)
            if adoption_key not in ("adopt", "revise", "reject"):
                raise PipelineError("%s has no valid adoption decision" % code)
            if adoption_key != "adopt" and not reason:
                raise PipelineError("%s revise/reject needs a reason" % code)
            if not isinstance(score, int) or score < 0 or score > item.get("score"):
                raise PipelineError("%s Gold score is outside its rubric maximum" % code)
            if not evidence:
                raise PipelineError("%s Gold score needs evidence or a reason" % code)
            rows.append({
                "code": code, "adoption": {
                    "adopt": "Adopt", "revise": "Revise", "reject": "Reject",
                }[adoption_key],
                "reason_or_revision": reason, "gold_score": score,
                "gold_evidence_or_reason": evidence,
            })
        workbook_codes = {value for address, value in rubric_cells.items()
                          if address.startswith("A") and isinstance(value, str)
                          and value.startswith("R") and value[1:].isdigit()}
        if workbook_codes != set(codes):
            raise PipelineError("expert workbook has unexpected rubric rows")
        findings = _findings(sheets, ui)
        if sum(item["decision"] == "Issue" for item in checklist) != len(findings):
            raise PipelineError("occupational checklist issues must match Findings rows")
        parsed.update({
            "occupation_mapping_decision": mapping_decision,
            "occupation_mapping_reason": mapping_reason,
            "professional_checklist": checklist,
            "rubric_version_reviewed": meta.get("rubric_version"),
            "rubric_items": rows, "findings": findings,
        })
    else:
        if verdict not in ("Pass", "Fail"):
            raise PipelineError("final workbook conclusion must be Pass or Fail")
        checklist = _check_rows(cells, "F", {"confirmed", "issue"}, ui)
        expected_checks = expected_config["checklist"]
        if ([item["id"] for item in checklist] !=
                [item["id"] for item in expected_checks]):
            raise PipelineError("final checklist is incomplete")
        for actual, expected in zip(checklist, expected_checks):
            row = _row_for_label(cells, actual["id"])
            if _cell(cells, "B%d" % row) != expected["text"]:
                raise PipelineError("final checklist text was altered")
        check_header = _row_for_label(cells, labels["id"])
        evidence_decisions = []
        for row, expected in enumerate(expected_config["final_evidence"], start=12):
            label = _cell(cells, "A%d" % row)
            value = _cell(cells, "B%d" % row)
            if label != expected["label"] or value != _display_literal(expected["value"]):
                raise PipelineError("final frozen evidence rows were altered")
            decision_key = _canonical_choice(_cell(cells, "C%d" % row), ui)
            if decision_key not in ("confirmed", "issue"):
                raise PipelineError("final evidence row %s is incomplete" % label)
            evidence_decisions.append({"label": label, "decision":
                                       "Confirmed" if decision_key == "confirmed" else "Issue"})
        if check_header != 14 + len(expected_config["final_evidence"]):
            raise PipelineError("final frozen evidence section has unexpected rows")
        closure_cells = sheets.get(ui["sheet_names"]["finding_closure"])
        if closure_cells is None:
            raise PipelineError("returned final XLSX is missing Finding Closure")
        expected = expected_config["finding_closure"]
        dispositions = []
        for index, item in enumerate(expected, start=4):
            expected_row = [
                item["finding_id"], item["source_layer"], item["disposition"],
                _display_literal(item["closed_at"]), item["rationale"],
                item["evidence_sha256"],
            ]
            actual_row = [_cell(closure_cells, "%s%d" % (column, index))
                          for column in "ABCDEF"]
            if actual_row != expected_row:
                raise PipelineError("final workbook finding closure rows were altered")
            decision_key = _canonical_choice(_cell(closure_cells, "G%d" % index), ui)
            if decision_key not in ("confirmed", "issue"):
                raise PipelineError("final finding %s is not checked" % item["finding_id"])
            dispositions.append({
                "finding_id": item["finding_id"],
                "disposition": item["disposition"],
                "rationale": item["rationale"],
                "evidence_files": [part.split("=", 1)[0] for part in
                                   item["evidence_sha256"].split("; ") if part],
                "closed_at": item["closed_at"], "final_check":
                "Confirmed" if decision_key == "confirmed" else "Issue",
            })
        empty_closure_issue = []
        if not expected:
            if ([_cell(closure_cells, "%s4" % column) for column in "ABCDEF"] !=
                    (["None", "", "", "", "", ""] if
                     expected_config["locale"] == "en" else
                     ["无", "", "", "", "", ""])):
                raise PipelineError("empty final finding closure row was altered")
            decision_key = _canonical_choice(_cell(closure_cells, "G4"), ui)
            if decision_key not in ("confirmed", "issue"):
                raise PipelineError("final reviewer must confirm that no findings exist")
            if decision_key == "issue":
                empty_closure_issue.append("Finding Closure: None")
        issues = ([item["id"] for item in checklist if item["decision"] == "Issue"] +
                  [item["label"] for item in evidence_decisions
                   if item["decision"] == "Issue"] +
                  [item["finding_id"] for item in dispositions
                   if item["final_check"] == "Issue"] + empty_closure_issue)
        if verdict == "Pass" and issues:
            raise PipelineError("final review cannot pass with Issue decisions")
        parsed.update({
            "sequence_confirmation": evidence_decisions,
            "final_checklist": checklist,
            "finding_dispositions": dispositions,
            "open_findings": issues,
        })
    return parsed


def _validate_transcription(layer, record, task_id, task, meta, rubric, codes):
    if record.get("layer") != layer or record.get("task_id") != task_id:
        raise PipelineError("receipt transcription layer/task_id mismatch")
    _validate_identity(record)
    if record.get("verdict") not in ("Pass", "Conditional pass", "Fail"):
        raise PipelineError("receipt verdict must be Pass, Conditional pass or Fail")
    if not str(record.get("opinion") or "").strip():
        raise PipelineError("receipt transcription needs a substantive opinion")
    findings = record.get("findings") or []
    ids = [item.get("finding_id") for item in findings]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise PipelineError("finding IDs must be present and unique")
    if layer == "occupational_expert_review":
        if record.get("rubric_version_reviewed") != meta.get("rubric_version"):
            raise PipelineError("expert receipt does not bind the current rubric version")
        rows = record.get("rubric_items") or []
        by_code = {row.get("code"): row for row in rows}
        if set(by_code) != set(codes):
            raise PipelineError("full expert receipt must cover every current rubric item")
        max_by_code = {code: item.get("score") for code, item in zip(codes, rubric)}
        id_by_code = {code: item.get("rubric_item_id")
                      for code, item in zip(codes, rubric)}
        for code, row in by_code.items():
            row["rubric_item_id"] = id_by_code[code]
            row["max_score"] = max_by_code[code]
            if row.get("adoption") not in ("Adopt", "Revise", "Reject"):
                raise PipelineError("%s has no valid adoption decision" % code)
            if (row["adoption"] != "Adopt" and
                    not str(row.get("reason_or_revision") or "").strip()):
                raise PipelineError("%s revise/reject needs a reason" % code)
            score = row.get("gold_score")
            if not isinstance(score, int) or score < 0 or score > max_by_code[code]:
                raise PipelineError("%s Gold score must be an integer from 0 to %s" %
                                    (code, max_by_code[code]))
            if not str(row.get("gold_evidence_or_reason") or "").strip():
                raise PipelineError("%s Gold score needs evidence or a reason" % code)
    if layer == "final_review":
        if record.get("verdict") not in ("Pass", "Fail"):
            raise PipelineError("final verdict must be Pass or Fail")
        if record.get("open_findings") not in (None, []):
            raise PipelineError("final receipt has open findings")


def _materialize_task_review(task, layer, record, receipt_sha):
    path = task / "reviewers.json"
    roster = _read_json(path) if path.is_file() else {}
    common = {
        "reviewer": record["reviewer_id"],
        "title": record["reviewer_title"],
        "date": record["reviewed_at"][:10],
        "reviewed_at": record["reviewed_at"],
        "identity_status": "project_side_identity_transcription",
        "credential_status": record.get("credential_status", "not_supplied"),
        "counts_toward_acceptance": record.get("verdict") == "Pass",
        "verdict": record["verdict"], "findings": record["opinion"],
        "source_receipt_sha256": receipt_sha,
        "source_form": "immutable returned XLSX + project-side identity/time transcription",
    }
    if layer == "occupational_expert_review":
        adopted = [row["code"] for row in record["rubric_items"]
                   if row["adoption"] == "Adopt"]
        objected = [row["code"] for row in record["rubric_items"]
                    if row["adoption"] != "Adopt"]
        common.update({
            "rubric_version_reviewed": record["rubric_version_reviewed"],
            "items_reviewed": [row["code"] for row in record["rubric_items"]],
            "adoption_rounds": [{
                "round": 1, "rubric_version": record["rubric_version_reviewed"],
                "adopted": adopted, "objected": objected,
            }],
            "remediated": not objected,
        })
        roster[layer] = [common]
        marking = {
            "_note": "Project-side transcription from immutable returned XLSX.",
            "rubric_version": record["rubric_version_reviewed"],
            "marked_by": record["reviewer_id"], "marked_on": record["reviewed_at"],
            "method": "occupational-expert item-by-item Gold review",
            "independence": "supplier-recorded review; not represented as independent third-party certification",
            "counts_toward_acceptance": record.get("verdict") == "Pass",
            "returned_form_total": sum(row["gold_score"] for row in record["rubric_items"]),
            "source_sha256": receipt_sha,
            "items": [{
                "code": row["code"], "rubric_item_id": row.get("rubric_item_id"),
                "awarded": row["gold_score"],
                "evidence": row["gold_evidence_or_reason"],
                "shortfall": (row["gold_evidence_or_reason"]
                              if row["gold_score"] < row.get("max_score", row["gold_score"])
                              else None),
            } for row in record["rubric_items"]],
        }
        _write_json(task / "gold_marking.json", marking)
    else:
        roster[layer] = common
    _write_json(path, roster)


def ingest_receipt(pipeline, layer, receipt_path, transcription_path, tasks_root):
    if layer not in LAYERS:
        raise PipelineError("unknown review layer: %s" % layer)
    receipt_path = Path(receipt_path).resolve()
    if receipt_path.suffix.lower() != ".xlsx" or not receipt_path.is_file() \
            or receipt_path.stat().st_size == 0:
        raise PipelineError("reviewer must return one non-empty .xlsx file")
    _validate_xlsx_container(receipt_path)
    transcription_path = Path(transcription_path).resolve()
    transcription = _read_json(transcription_path)
    state = pipeline._load()
    cycle = state.get("review_cycle")
    if not cycle:
        raise PipelineError("create the phase-1 review kit before ingesting receipts")
    task, meta, rubric, codes = _task_data(tasks_root, state["task_id"])
    if layer in PHASE1_LAYERS:
        if cycle.get("status") not in (
                "awaiting_phase1_reviews", "remediation_required",
                "phase1_review_failed"):
            raise PipelineError(
                "phase-1 receipt cannot replace downstream evidence; create a new review kit")
        if production_basis(pipeline, tasks_root)["digest"] != \
                cycle["initial_basis"]["digest"]:
            raise PipelineError(
                "production basis changed; create a new phase-1 review kit")
        if (cycle.get("phase1") or {}).get("receipts", {}).get(layer):
            raise PipelineError(
                "phase-1 receipt is immutable within its review cycle; "
                "create a new review kit to replace it")
        expected_candidate_sha = cycle["candidate_delivery"]["sha256"]
        general_config, expert_config = _phase1_configs(
            meta, rubric, codes, cycle["initial_basis"], expected_candidate_sha)
        expected_config = (general_config if layer == "general_review" else
                           expert_config)
    else:
        if cycle.get("status") not in ("awaiting_final_review", "final_review_failed"):
            raise PipelineError("final receipt is not expected at this workflow stage")
        package = (cycle.get("final") or {}).get("package")
        if not package:
            raise PipelineError("final receipt requires a generated final-review package")
        expected_candidate_sha = package["candidate_sha256"]
        current_basis = production_basis(pipeline, tasks_root)
        if current_basis["digest"] != package["basis_digest"]:
            raise PipelineError("production basis changed after final-package freeze")
        expected_config = _final_config(
            meta, current_basis, expected_candidate_sha, cycle)
    parsed = _parse_review_workbook(
        layer, receipt_path, state["task_id"], meta, rubric, codes,
        expected_candidate_sha, expected_config)
    unknown = set(transcription) - TRANSCRIPTION_FIELDS
    if unknown:
        raise PipelineError(
            "project transcription has unsupported fields: %s" %
            ", ".join(sorted(unknown)))
    record = dict(transcription)
    record.update(parsed)
    _validate_transcription(layer, record, state["task_id"], task, meta, rubric, codes)
    receipt_sha = _sha256(receipt_path)
    record["source_receipt_sha256"] = receipt_sha
    record["transcription_status"] = "project_side_transcription"
    if layer in PHASE1_LAYERS:
        record["review_basis_digest"] = cycle["initial_basis"]["digest"]
        if _iso_time(record["reviewed_at"], "reviewed_at") <= \
                _iso_time(cycle["created_at"], "phase-1 package created_at"):
            raise PipelineError(
                "phase-1 review must be strictly later than package creation")
    else:
        record["review_basis_digest"] = cycle["final"]["package"]["basis_digest"]
        earlier = [cycle["phase1"]["receipts"].get(name) for name in PHASE1_LAYERS]
        if any(not value for value in earlier):
            raise PipelineError("final receipt requires both phase-1 receipts")
        final_time = _iso_time(record["reviewed_at"], "final reviewed_at")
        boundaries = [_iso_time(value["reviewed_at"], name + " reviewed_at")
                      for name, value in zip(PHASE1_LAYERS, earlier)]
        remediation = cycle.get("remediation") or {}
        all_requirements = _all_remediation_requirements(cycle)
        expected_findings = {item["requirement_id"] for item in all_requirements}
        dispositions = record.get("finding_dispositions") or []
        recorded_findings = {item.get("finding_id") for item in dispositions}
        if recorded_findings != expected_findings:
            raise PipelineError(
                "final receipt must confirm every remediated finding")
        boundaries.extend(_iso_time(item["closed_at"], "finding closed_at")
                          for item in all_requirements)
        boundaries.extend(_iso_time(item["reviewed_at"], "supplemental reviewed_at")
                          for item in (remediation.get("supplemental_receipts") or {}).values())
        boundaries.append(_iso_time(cycle["pre_final_validation"]["run_at"],
                                    "pre-final validation run_at"))
        boundaries.append(_iso_time(cycle["final"]["package"]["frozen_at"],
                                    "final package frozen_at"))
        if final_time <= max(boundaries):
            raise PipelineError("final review must be strictly later than all prior review, closure and freeze times")
        reviewers = [value["reviewer_id"] for value in earlier] + [record["reviewer_id"]]
        if len(set(reviewers)) != 3:
            raise PipelineError("the three review layers require distinct reviewers")

    normalized_root = pipeline.root / "gates" / "review_receipts" / layer / receipt_sha
    normalized_root.mkdir(parents=True, exist_ok=True)
    normalized_path = normalized_root / "normalized_transcription.json"
    _write_json(normalized_path, record)
    artifact = pipeline.add_artifact(
        REVIEW_ARTIFACTS[layer],
        [receipt_path, transcription_path, normalized_path], "human")
    state = pipeline._load()
    cycle = state["review_cycle"]
    stored = {
        "reviewer_id": record["reviewer_id"], "reviewed_at": record["reviewed_at"],
        "verdict": record["verdict"], "finding_ids": [
            item["finding_id"] for item in record.get("findings", [])],
        "source_receipt_sha256": receipt_sha,
        "source_transcription_sha256": _sha256(transcription_path),
        "transcription_sha256": _sha256(normalized_path),
        "artifact_digest": artifact["digest"],
        "record": record,
    }
    if layer in PHASE1_LAYERS:
        cycle["phase1"]["receipts"][layer] = stored
        if all(cycle["phase1"]["receipts"].get(name) for name in PHASE1_LAYERS):
            cycle["status"] = "remediation_required"
    else:
        cycle["final"]["receipt"] = stored
        cycle["status"] = ("final_review_complete" if record["verdict"] == "Pass"
                           else "final_review_failed")
    state["review_cycle"] = cycle
    pipeline._save(state)
    _materialize_task_review(task, layer, record, receipt_sha)
    return {"layer": layer, "receipt_sha256": receipt_sha,
            "workflow_stage": cycle["status"]}


def record_remediation(pipeline, closure_path, tasks_root):
    closure_path = Path(closure_path).resolve()
    closure = _read_json(closure_path)
    state = pipeline._load()
    cycle = state.get("review_cycle") or {}
    retry = cycle.get("status") == "supplemental_review_failed"
    if cycle.get("status") not in ("remediation_required", "supplemental_review_failed"):
        raise PipelineError("remediation requires two current phase-1 receipts")
    receipts = (cycle.get("phase1") or {}).get("receipts") or {}
    if not all(receipts.get(layer) for layer in PHASE1_LAYERS):
        raise PipelineError("remediation requires both phase-1 receipts")
    if closure.get("task_id") != state["task_id"]:
        raise PipelineError("remediation task_id mismatch")
    requirements = _phase1_requirements(cycle)
    by_id = {item["requirement_id"]: item for item in requirements}
    expected = set(by_id)
    findings = closure.get("findings") or []
    recorded = {item.get("finding_id") for item in findings}
    if recorded != expected:
        raise PipelineError("remediation must dispose every review requirement; expected %s, recorded %s" %
                            (sorted(expected), sorted(recorded)))
    sources = [closure_path]
    for item in findings:
        finding_id = item.get("finding_id")
        requirement = by_id[finding_id]
        source_layer = requirement["source_layer"]
        if item.get("disposition") not in ("closed", "accepted_without_change"):
            raise PipelineError("finding %s is unresolved" % finding_id)
        if not str(item.get("rationale") or "").strip():
            raise PipelineError("finding %s has no rationale" % item.get("finding_id"))
        closed_at = _iso_time(item.get("closed_at"), "finding closed_at")
        reviewed_at = _iso_time(
            requirement.get("source_reviewed_at") or
            receipts[source_layer]["reviewed_at"],
            source_layer + " reviewed_at")
        if closed_at <= reviewed_at:
            raise PipelineError(
                "finding %s must close strictly after its source review" % finding_id)
        evidence = item.get("evidence_files") or []
        if not evidence:
            raise PipelineError("finding %s has no closure evidence" % item.get("finding_id"))
        for rel in evidence:
            relpath = Path(rel)
            if relpath.is_absolute() or ".." in relpath.parts:
                raise PipelineError("closure evidence paths must be relative")
            source = closure_path.parent / relpath
            if not source.is_file() or source.stat().st_size == 0:
                raise PipelineError("closure evidence is missing: %s" % rel)
            item.setdefault("evidence_sha256", {})[rel] = _sha256(source)
            if source not in sources:
                sources.append(source)
        item["requirement_id"] = item.pop("finding_id")
        item["source_layer"] = source_layer
        item["kind"] = requirement["kind"]
        item["summary"] = requirement["summary"]
        item["requires_confirmation"] = requirement["requires_confirmation"]
        if requirement.get("rubric_code"):
            item["rubric_code"] = requirement["rubric_code"]
    basis = production_basis(pipeline, tasks_root)
    from_basis = ((cycle.get("remediation") or {}).get("basis") if retry else
                  cycle["initial_basis"])
    if not from_basis:
        raise PipelineError("previous remediation basis is unavailable")
    if any(item.get("disposition") == "closed" for item in findings) and \
            basis["digest"] == from_basis["digest"]:
        raise PipelineError(
            "review requirements exist but the production basis has not changed")
    initial_rubric = from_basis.get("rubric_snapshot") or {}
    current_rubric = basis.get("rubric_snapshot") or {}
    for item in findings:
        code = item.get("rubric_code")
        if code and current_rubric.get(code, {}).get("digest") == \
                initial_rubric.get(code, {}).get("digest"):
            raise PipelineError(
                "rubric requirement %s was not changed before closure" % code)
    closure["from_basis_digest"] = from_basis["digest"]
    closure["to_basis_digest"] = basis["digest"]
    normalized_root = pipeline.root / "gates" / "remediation" / str(uuid4())
    normalized_root.mkdir(parents=True)
    normalized = normalized_root / "normalized_remediation.json"
    _write_json(normalized, closure)
    sources.append(normalized)
    artifact = pipeline.add_artifact("review_remediation", sources, "remediation")
    state = pipeline._load()
    supplemental_layers = {item["source_layer"] for item in findings
                           if item.get("requires_confirmation")}
    change_impacts = _review_change_impacts(from_basis, basis, pipeline.policy)
    supplemental_layers.update(change_impacts)
    history = list(state["review_cycle"].get("remediation_history") or [])
    if retry and state["review_cycle"].get("remediation"):
        history.append(state["review_cycle"]["remediation"])
    state["review_cycle"]["remediation_history"] = history
    state["review_cycle"]["remediation"] = {
        "recorded_at": _now(), "artifact_digest": artifact["digest"],
        "from_basis_digest": closure["from_basis_digest"],
        "to_basis_digest": closure["to_basis_digest"],
        "from_basis": from_basis,
        "basis": basis,
        "change_impacts": change_impacts,
        "requirements": findings,
        "supplemental_required_layers": sorted(supplemental_layers),
        "supplemental_package": None,
        "supplemental_receipts": {},
    }
    state["review_cycle"]["status"] = (
        "supplemental_review_kit_required" if
        state["review_cycle"]["remediation"]["supplemental_required_layers"] else
        "pre_final_validation_required")
    pipeline._save(state)
    return state["review_cycle"]["remediation"]


def create_supplemental(pipeline, delivery, tasks_root, output_dir, node,
                        node_modules):
    state = pipeline._load()
    cycle = state.get("review_cycle") or {}
    if cycle.get("status") != "supplemental_review_kit_required":
        raise PipelineError("supplemental kit requires recorded remediation")
    remediation = cycle.get("remediation") or {}
    layers = remediation.get("supplemental_required_layers") or []
    if not layers:
        raise PipelineError("no review layer requires supplemental confirmation")
    basis = production_basis(pipeline, tasks_root)
    if basis["digest"] != remediation.get("to_basis_digest"):
        raise PipelineError("production basis changed after remediation")
    task, meta, rubric, codes = _task_data(tasks_root, state["task_id"])
    input_manifest = None
    if cycle.get("input_contract_schema"):
        input_manifest = prepare_review_input(
            task, Path(delivery).resolve(), state["task_id"], pipeline.policy,
            basis, state.get("artifacts", {}).get("occupation_standard"))
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    frozen_at = _now()
    with tempfile.TemporaryDirectory(prefix="gdpval-supplemental-kit-") as raw:
        tmp = Path(raw)
        candidate = tmp / "Post-Remediation-Candidate"
        _candidate_snapshot(state["task_id"], task, meta, delivery, None, candidate)
        candidate_zip = tmp / "Post-Remediation-Candidate.zip"
        candidate_info = _write_package(
            candidate, candidate_zip, "Post-Remediation-Candidate")
        packages = {}
        configs = {}
        for layer in layers:
            config = _supplemental_config(
                meta, rubric, codes, basis, candidate_info["sha256"], cycle,
                layer, input_manifest)
            if not config["requirements"]:
                raise PipelineError(
                    "supplemental layer %s has no changed item" % layer)
            configs[layer] = config
            label = ("General" if layer == "general_review" else "Occupational")
            tree = tmp / (label + "-Supplemental-Review-Package")
            tree.mkdir()
            _run_builder(config, tree / (label + "-Supplemental-Review.xlsx"),
                         node, node_modules)
            shutil.copytree(
                str(candidate),
                str(tree / "Read-Only-Materials" / "Post-Remediation-Candidate"))
            source_artifact = state["artifacts"][REVIEW_ARTIFACTS[layer]]
            shutil.copytree(
                str(pipeline.root / source_artifact["path"]),
                str(tree / "Read-Only-Materials" / "Original-Receipt"))
            remediation_artifact = state["artifacts"]["review_remediation"]
            shutil.copytree(
                str(pipeline.root / remediation_artifact["path"]),
                str(tree / "Read-Only-Materials" / "Remediation"))
            if input_manifest:
                _write_json(tree / "Read-Only-Materials" /
                            "Review-Evidence" / "review_brief.json",
                            _role_review_brief(input_manifest, layer))
            archive = output_dir / (tree.name + ".zip")
            packages[layer] = _write_package(tree, archive, tree.name)
        kit = tmp / "Supplemental-Human-Review-Kit"
        kit.mkdir()
        for package in packages.values():
            _copy_file(package["path"], kit / Path(package["path"]).name)
        _write_json(kit / "supplemental_review_manifest.json", {
            "schema_version": "staged-xlsx-v1",
            "task_id": state["task_id"], "basis_digest": basis["digest"],
            "candidate_sha256": candidate_info["sha256"], "frozen_at": frozen_at,
            "layers": {layer: {
                "package_sha256": packages[layer]["sha256"],
                "requirement_ids": [item["requirement_id"]
                                    for item in configs[layer]["requirements"]],
            } for layer in layers},
            "return_contract": "one completed XLSX per affected original reviewer",
        })
        kit_path = output_dir / "Supplemental-Human-Review-Kit.zip"
        kit_info = _write_package(kit, kit_path, kit.name)
    artifact = pipeline.add_artifact(
        "supplemental_review_kit", [kit_path], "review-kit")
    state = pipeline._load()
    state["review_cycle"]["remediation"]["supplemental_package"] = {
        "sha256": kit_info["sha256"], "artifact_digest": artifact["digest"],
        "basis_digest": basis["digest"],
        "candidate_sha256": candidate_info["sha256"], "frozen_at": frozen_at,
        "layers": {layer: {
            "package_sha256": packages[layer]["sha256"],
            "requirements": configs[layer]["requirements"],
        } for layer in layers},
    }
    state["review_cycle"]["status"] = "awaiting_supplemental_reviews"
    pipeline._save(state)
    return {"supplemental_review_kit": str(kit_path),
            "layers": list(layers), "sha256": kit_info["sha256"]}


def _parse_supplemental_workbook(path, task_id, meta, candidate_sha, config):
    sheets = _xlsx_cells(path)
    ui = config["ui"]
    cells = sheets.get(ui["sheet_names"]["supplemental_review"])
    if cells is None:
        raise PipelineError("supplemental XLSX is missing Supplemental Review")
    labels = ui["labels"]
    expected_meta = {
        labels["task_id"]: task_id,
        labels["rubric_version"]: meta.get("rubric_version", "unversioned"),
        labels["candidate_sha256"]: candidate_sha,
    }
    if _metadata(cells, ui) != expected_meta:
        raise PipelineError("supplemental XLSX does not match its frozen package")
    decisions = []
    for row, expected in enumerate(config["requirements"], start=12):
        evidence = expected["remediation_display"]
        frozen = [_cell(cells, "%s%d" % (column, row)) for column in "ABCD"]
        if frozen != [expected["requirement_id"], expected["kind_display"],
                      expected["summary"], evidence]:
            raise PipelineError("supplemental frozen row was altered: %s" %
                                expected["requirement_id"])
        decision_key = _canonical_choice(_cell(cells, "E%d" % row), ui)
        comment = _cell(cells, "F%d" % row)
        if decision_key not in ("confirmed", "issue"):
            raise PipelineError("supplemental row is incomplete: %s" %
                                expected["requirement_id"])
        if decision_key == "issue" and not comment:
            raise PipelineError("supplemental Issue needs a comment: %s" %
                                expected["requirement_id"])
        item = {"requirement_id": expected["requirement_id"],
                "decision": ("Confirmed" if decision_key == "confirmed" else "Issue"),
                "comment": comment,
                "kind": expected["kind"]}
        rubric_item = expected.get("rubric")
        if rubric_item:
            adoption_key = _canonical_choice(_cell(cells, "G%d" % row), ui)
            score = _cell(cells, "H%d" % row)
            gold_evidence = _cell(cells, "I%d" % row)
            if adoption_key not in ("adopt", "revise", "reject"):
                raise PipelineError("changed rubric row needs an adoption decision")
            if adoption_key != "adopt" and not comment:
                raise PipelineError("Revise/Reject needs a comment")
            if not isinstance(score, int) or score < 0 or \
                    score > rubric_item["max_score"]:
                raise PipelineError("changed rubric row has an invalid Gold score")
            if not gold_evidence:
                raise PipelineError("changed rubric row needs Gold evidence")
            item.update({"rubric_code": rubric_item["code"],
                         "adoption": {"adopt": "Adopt", "revise": "Revise",
                                      "reject": "Reject"}[adoption_key],
                         "gold_score": score,
                         "gold_evidence_or_reason": gold_evidence})
        decisions.append(item)
    verdict, opinion = _return_fields(cells, ui)
    if verdict not in ("Pass", "Fail"):
        raise PipelineError("supplemental conclusion must be Pass or Fail")
    blocking = [item for item in decisions
                if item["decision"] == "Issue" or
                item.get("adoption") in ("Revise", "Reject")]
    if verdict == "Pass" and blocking:
        raise PipelineError("supplemental review cannot pass unresolved changed items")
    return {"verdict": verdict, "opinion": opinion, "items": decisions}


def _materialize_supplemental(task, source_layer, original, supplemental,
                              receipt_sha, meta, rubric, codes, initial_basis,
                              current_basis):
    merged = dict(original["record"])
    merged.update({
        "reviewer_id": supplemental["reviewer_id"],
        "reviewer_title": supplemental["reviewer_title"],
        "reviewed_at": supplemental["reviewed_at"],
        "credential_status": supplemental["credential_status"],
        "verdict": "Pass", "opinion": supplemental["opinion"],
        "findings": [],
    })
    if source_layer == "occupational_expert_review":
        original_rows = {item["code"]: dict(item)
                         for item in original["record"]["rubric_items"]}
        supplemental_rows = {item["rubric_code"]: item
                             for item in supplemental["items"]
                             if item.get("rubric_code")}
        rows = []
        initial_snapshot = initial_basis.get("rubric_snapshot") or {}
        current_snapshot = current_basis.get("rubric_snapshot") or {}
        for code, rubric_item in zip(codes, rubric):
            if code in supplemental_rows:
                supplied = supplemental_rows[code]
                if supplied["adoption"] != "Adopt":
                    raise PipelineError("supplemental rubric row is not adopted: %s" % code)
                row = {"code": code, "adoption": "Adopt",
                       "reason_or_revision": "Confirmed in changed-item supplement.",
                       "gold_score": supplied["gold_score"],
                       "gold_evidence_or_reason": supplied["gold_evidence_or_reason"]}
            else:
                row = original_rows.get(code)
                if not row or row.get("adoption") != "Adopt" or \
                        initial_snapshot.get(code, {}).get("digest") != \
                        current_snapshot.get(code, {}).get("digest"):
                    raise PipelineError(
                        "changed rubric row lacks supplemental adoption: %s" % code)
                row = dict(row)
            row["rubric_item_id"] = rubric_item.get("rubric_item_id")
            row["max_score"] = rubric_item.get("score")
            rows.append(row)
        merged["rubric_items"] = rows
        merged["rubric_version_reviewed"] = meta.get("rubric_version")
        merged["occupation_mapping_decision"] = "Accept"
    _materialize_task_review(task, source_layer, merged, receipt_sha)


def ingest_supplemental(pipeline, source_layer, receipt_path,
                        transcription_path, tasks_root):
    if source_layer not in PHASE1_LAYERS:
        raise PipelineError("supplemental source layer must be phase-1")
    receipt_path = Path(receipt_path).resolve()
    if receipt_path.suffix.lower() != ".xlsx" or not receipt_path.is_file() or \
            receipt_path.stat().st_size == 0:
        raise PipelineError("supplemental reviewer must return one XLSX")
    _validate_xlsx_container(receipt_path)
    transcription_path = Path(transcription_path).resolve()
    transcription = _read_json(transcription_path)
    unknown = set(transcription) - TRANSCRIPTION_FIELDS
    if unknown:
        raise PipelineError("project transcription has unsupported fields: %s" %
                            ", ".join(sorted(unknown)))
    state = pipeline._load()
    cycle = state.get("review_cycle") or {}
    if cycle.get("status") not in (
            "awaiting_supplemental_reviews", "supplemental_review_failed"):
        raise PipelineError("supplemental receipt is not expected at this stage")
    remediation = cycle.get("remediation") or {}
    package = remediation.get("supplemental_package") or {}
    layer_package = (package.get("layers") or {}).get(source_layer)
    if not layer_package:
        raise PipelineError("this review layer has no supplemental package")
    if (remediation.get("supplemental_receipts") or {}).get(source_layer):
        raise PipelineError(
            "supplemental receipt is immutable within its remediation round; "
            "record a new remediation round before re-review")
    basis = production_basis(pipeline, tasks_root)
    if basis["digest"] != package.get("basis_digest"):
        raise PipelineError("production basis changed after supplemental freeze")
    task, meta, rubric, codes = _task_data(tasks_root, state["task_id"])
    config = _supplemental_config(
        meta, rubric, codes, basis, package["candidate_sha256"], cycle,
        source_layer)
    parsed = _parse_supplemental_workbook(
        receipt_path, state["task_id"], meta, package["candidate_sha256"], config)
    record = dict(transcription)
    record.update(parsed)
    if record.get("task_id") != state["task_id"] or \
            record.get("layer") != source_layer:
        raise PipelineError("supplemental transcription layer/task_id mismatch")
    _validate_identity(record)
    original = cycle["phase1"]["receipts"][source_layer]
    if record["reviewer_id"] != original["reviewer_id"]:
        raise PipelineError("changed items must be confirmed by the original reviewer")
    reviewed_at = _iso_time(record["reviewed_at"], "supplemental reviewed_at")
    boundaries = [_iso_time(original["reviewed_at"], "original reviewed_at"),
                  _iso_time(remediation["recorded_at"], "remediation recorded_at"),
                  _iso_time(package["frozen_at"], "supplemental frozen_at")]
    if reviewed_at <= max(boundaries):
        raise PipelineError("supplemental review must be later than remediation and freeze")
    receipt_sha = _sha256(receipt_path)
    record.update({"source_receipt_sha256": receipt_sha,
                   "review_basis_digest": basis["digest"],
                   "transcription_status": "project_side_transcription"})
    normalized_root = (pipeline.root / "gates" / "supplemental_receipts" /
                       source_layer / receipt_sha)
    normalized_root.mkdir(parents=True, exist_ok=True)
    normalized = normalized_root / "normalized_transcription.json"
    _write_json(normalized, record)
    artifact = pipeline.add_artifact(
        SUPPLEMENTAL_ARTIFACTS[source_layer],
        [receipt_path, transcription_path, normalized], "human")
    state = pipeline._load()
    stored = {
        "reviewer_id": record["reviewer_id"], "reviewed_at": record["reviewed_at"],
        "verdict": record["verdict"], "source_receipt_sha256": receipt_sha,
        "transcription_sha256": _sha256(normalized),
        "artifact_digest": artifact["digest"], "record": record,
    }
    state["review_cycle"]["remediation"]["supplemental_receipts"][source_layer] = stored
    required = state["review_cycle"]["remediation"]["supplemental_required_layers"]
    receipts = state["review_cycle"]["remediation"]["supplemental_receipts"]
    if record["verdict"] != "Pass":
        state["review_cycle"]["status"] = "supplemental_review_failed"
    elif all(receipts.get(layer, {}).get("verdict") == "Pass" for layer in required):
        for layer in required:
            state["review_cycle"]["phase1"]["receipts"][layer]["release_verdict"] = "Pass"
        state["review_cycle"]["status"] = "pre_final_validation_required"
    pipeline._save(state)
    if record["verdict"] == "Pass":
        _materialize_supplemental(
            task, source_layer, original, record, receipt_sha, meta, rubric, codes,
            cycle["initial_basis"], basis)
    return {"layer": source_layer, "receipt_sha256": receipt_sha,
            "workflow_stage": state["review_cycle"]["status"]}


def create_final(pipeline, delivery, tasks_root, output_dir, node, node_modules):
    state = pipeline._load()
    cycle = state.get("review_cycle") or {}
    if cycle.get("status") != "final_review_kit_required":
        raise PipelineError(
            "final-review package is only created after current pre-final validation")
    if not cycle.get("remediation"):
        raise PipelineError("final-review package requires remediation closure")
    pre_final = cycle.get("pre_final_validation")
    if not pre_final or pre_final.get("status") != "passed":
        raise PipelineError("final-review package requires current pre-final validation")
    basis = production_basis(pipeline, tasks_root)
    if basis["digest"] != cycle["remediation"]["to_basis_digest"]:
        raise PipelineError("production basis changed after remediation; close the new basis first")
    task, meta, _rubric, _codes = _task_data(tasks_root, state["task_id"])
    delivery = Path(delivery).resolve()
    input_manifest = None
    if cycle.get("input_contract_schema"):
        input_manifest = prepare_review_input(
            task, delivery, state["task_id"], pipeline.policy, basis,
            state.get("artifacts", {}).get("occupation_standard"))
    current_delivery_digest, _delivery_files = _bundle_manifest(delivery)
    review_payload_digest, _review_payload = _review_payload_digest(
        delivery, state["task_id"])
    gate = state.get("gates", {}).get("pre_final_validation") or {}
    if current_delivery_digest != gate.get("delivery_digest"):
        raise PipelineError(
            "delivery changed after pre-final validation; validate the current "
            "snapshot before generating the final-review package")
    first = cycle["phase1"]["receipts"]
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    frozen_at = _now()
    with tempfile.TemporaryDirectory(prefix="gdpval-final-kit-") as raw:
        tree = Path(raw) / "Final-Review-Package"
        tree.mkdir()
        candidate = Path(raw) / "Post-Remediation-Candidate"
        _candidate_snapshot(state["task_id"], task, meta, delivery,
                            None, candidate)
        candidate_zip = Path(raw) / "Post-Remediation-Candidate.zip"
        candidate_info = _write_package(
            candidate, candidate_zip, "Post-Remediation-Candidate")
        config = _final_config(
            meta, basis, candidate_info["sha256"], cycle, input_manifest)
        _run_builder(config, tree / "Final-Review.xlsx", node, node_modules)
        shutil.copytree(str(candidate),
                        str(tree / "Read-Only-Materials" /
                            "Post-Remediation-Candidate"))
        for layer, receipt in first.items():
            artifact = state["artifacts"][REVIEW_ARTIFACTS[layer]]
            shutil.copytree(
                str(pipeline.root / artifact["path"]),
                str(tree / "Read-Only-Materials" / "Phase-1-Receipts" / layer))
        for layer, receipt in ((cycle.get("remediation") or {})
                               .get("supplemental_receipts") or {}).items():
            artifact = state["artifacts"][SUPPLEMENTAL_ARTIFACTS[layer]]
            shutil.copytree(
                str(pipeline.root / artifact["path"]),
                str(tree / "Read-Only-Materials" /
                    "Supplemental-Receipts" / layer))
        remediation_artifact = state["artifacts"]["review_remediation"]
        shutil.copytree(
            str(pipeline.root / remediation_artifact["path"]),
            str(tree / "Read-Only-Materials" / "Remediation"))
        for index, historical in enumerate(cycle.get("remediation_history") or [],
                                           start=1):
            digest = historical.get("artifact_digest")
            source = pipeline.root / "_artifacts" / "review_remediation" / str(digest)
            if not source.is_dir():
                raise PipelineError("historical remediation artifact is missing")
            shutil.copytree(
                str(source), str(tree / "Read-Only-Materials" /
                                 "Remediation-History" / ("round-%02d" % index)))
            for layer, receipt in (historical.get("supplemental_receipts") or {}).items():
                receipt_source = (pipeline.root / "_artifacts" /
                                  SUPPLEMENTAL_ARTIFACTS[layer] /
                                  receipt["artifact_digest"])
                if not receipt_source.is_dir():
                    raise PipelineError("historical supplemental receipt is missing")
                shutil.copytree(
                    str(receipt_source), str(tree / "Read-Only-Materials" /
                                            "Supplemental-Receipt-History" /
                                            ("round-%02d" % index) / layer))
        validation_artifact = state["artifacts"]["pre_final_validation_evidence"]
        shutil.copytree(
            str(pipeline.root / validation_artifact["path"]),
            str(tree / "Read-Only-Materials" / "Pre-Final-Validation"))
        _write_json(tree / "Read-Only-Materials" / "final_review_manifest.json", {
            "task_id": state["task_id"], "basis_digest": basis["digest"],
            "delivery_digest": current_delivery_digest,
            "review_payload_digest": review_payload_digest,
            "candidate_sha256": candidate_info["sha256"], "frozen_at": frozen_at,
            "phase1_receipts": {name: value["source_receipt_sha256"]
                                for name, value in first.items()},
            "supplemental_receipts": {name: value["source_receipt_sha256"]
                                      for name, value in ((cycle.get("remediation") or {})
                                                          .get("supplemental_receipts") or {}).items()},
            "remediation_history": [item.get("artifact_digest")
                                    for item in cycle.get("remediation_history") or []],
            "pre_final_validation": pre_final,
        })
        archive = output_dir / "Final-Review-Package.zip"
        info = _write_package(tree, archive, tree.name)
    artifact = pipeline.add_artifact("final_review_package", [archive], "review-kit")
    state = pipeline._load()
    state["review_cycle"]["final"]["package"] = {
        "sha256": info["sha256"], "artifact_digest": artifact["digest"],
        "basis_digest": basis["digest"],
        "delivery_digest": current_delivery_digest,
        "review_payload_digest": review_payload_digest,
        "candidate_sha256": candidate_info["sha256"], "frozen_at": frozen_at,
    }
    state["review_cycle"]["status"] = "awaiting_final_review"
    pipeline._save(state)
    return {"final_review_package": str(archive), "sha256": info["sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("phase1", help="create candidate delivery and two phase-1 review packages")
    p.add_argument("workspace"); p.add_argument("delivery"); p.add_argument("output")
    p.add_argument("--tasks-root", required=True); p.add_argument("--staging-root", required=True)
    p.add_argument("--node", required=True); p.add_argument("--node-modules", required=True)
    p = sub.add_parser("ingest", help="archive one returned XLSX and its project transcription")
    p.add_argument("workspace"); p.add_argument("layer", choices=LAYERS)
    p.add_argument("receipt"); p.add_argument("transcription"); p.add_argument("--tasks-root", required=True)
    p = sub.add_parser("remediation", help="record complete finding disposition and closure evidence")
    p.add_argument("workspace"); p.add_argument("closure"); p.add_argument("--tasks-root", required=True)
    p = sub.add_parser("supplemental", help="create changed-items-only reviewer packages")
    p.add_argument("workspace"); p.add_argument("delivery"); p.add_argument("output")
    p.add_argument("--tasks-root", required=True)
    p.add_argument("--node", required=True); p.add_argument("--node-modules", required=True)
    p = sub.add_parser("ingest-supplemental", help="ingest one changed-items-only XLSX")
    p.add_argument("workspace"); p.add_argument("layer", choices=PHASE1_LAYERS)
    p.add_argument("receipt"); p.add_argument("transcription"); p.add_argument("--tasks-root", required=True)
    p = sub.add_parser("final", help="create the final-review package after pre-final validation")
    p.add_argument("workspace"); p.add_argument("delivery"); p.add_argument("output")
    p.add_argument("--tasks-root", required=True)
    p.add_argument("--node", required=True); p.add_argument("--node-modules", required=True)
    args = parser.parse_args(argv)
    try:
        pipeline = Pipeline(args.workspace)
        if args.command == "phase1":
            result = create_phase1(pipeline, args.delivery, args.tasks_root,
                                   args.staging_root, args.output,
                                   args.node, args.node_modules)
        elif args.command == "ingest":
            result = ingest_receipt(pipeline, args.layer, args.receipt,
                                    args.transcription, args.tasks_root)
        elif args.command == "remediation":
            result = record_remediation(pipeline, args.closure, args.tasks_root)
        elif args.command == "supplemental":
            result = create_supplemental(
                pipeline, args.delivery, args.tasks_root, args.output,
                args.node, args.node_modules)
        elif args.command == "ingest-supplemental":
            result = ingest_supplemental(
                pipeline, args.layer, args.receipt, args.transcription,
                args.tasks_root)
        else:
            result = create_final(pipeline, args.delivery, args.tasks_root, args.output,
                                  args.node, args.node_modules)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (PipelineError, ReviewContractError, OSError, ValueError, KeyError) as exc:
        print("review-kit error: %s" % exc, file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

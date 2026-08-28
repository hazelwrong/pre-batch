"""Machine-enforced input contract for staged GDPval human review."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from rights_policy import evaluate_usage_rights


SHA256 = re.compile(r"^[0-9a-f]{64}$")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class ReviewContractError(ValueError):
    pass


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise ReviewContractError("invalid or missing review input: %s" % path) from exc


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _language_code(value):
    value = str(value or "").strip().lower()
    if value.startswith("zh") or value in {"chinese", "中文", "汉语", "普通话"}:
        return "zh"
    if value.startswith("en") or value in {"english", "英文", "英语"}:
        return "en"
    return None


def _main_language(text):
    """Conservatively distinguish Chinese from English task-facing prose."""
    text = str(text or "")
    cjk = len(CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk >= 8 and cjk * 5 >= latin:
        return "zh"
    if latin >= 20 and latin * 5 > cjk:
        return "en"
    return None


def _validate_task_language(task, meta):
    expected = _language_code(meta.get("language"))
    if expected is None:
        raise ReviewContractError("task language must be explicitly Chinese/zh or English/en")
    prompt = (task / "prompt.md").read_text(encoding="utf-8")
    prompt_language = _main_language(prompt)
    if prompt_language and prompt_language != expected:
        raise ReviewContractError("prompt language does not match task language")
    rubric = _read_json(task / "rubric.json")
    if not isinstance(rubric, list) or not rubric:
        raise ReviewContractError("rubric.json must be a non-empty list")
    for index, item in enumerate(rubric, start=1):
        for field in ("criterion", "verification"):
            language = _main_language((item or {}).get(field))
            if language and language != expected:
                raise ReviewContractError(
                    "rubric item %d %s language does not match task language" %
                    (index, field))
    pretty_language = _main_language(
        (task / "rubric_pretty.txt").read_text(encoding="utf-8"))
    if pretty_language and pretty_language != expected:
        raise ReviewContractError(
            "rubric_pretty language does not match task language")


def _file_entry(root, path, scope):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ReviewContractError("review input is missing or empty: %s" % path)
    return {"scope": scope, "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _task_record(delivery, task_id):
    path = delivery / "tasks.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    matches = [row for row in rows if row.get("task_id") == task_id]
    if len(matches) != 1:
        raise ReviewContractError(
            "review input needs exactly one tasks.jsonl row for %s" % task_id)
    return matches[0]


def _profiles(task, policy):
    profiles = _read_json(task / "expert_profiles.json")
    required_count = int(((policy.get("human_review") or {})
                          .get("expert_profiles_required_per_task", 3)))
    fields = ((policy.get("human_review") or {})
              .get("expert_profile_required_fields") or [])
    if not isinstance(profiles, list) or len(profiles) != required_count:
        raise ReviewContractError(
            "expert_profiles.json must contain exactly %d profiles" % required_count)
    expected_roles = {"general_review", "occupational_expert_review", "final_review"}
    roles = set()
    for index, profile in enumerate(profiles, start=1):
        missing = [field for field in fields if not profile.get(field)]
        if missing:
            raise ReviewContractError(
                "expert profile %d is missing %s" % (index, ", ".join(missing)))
        role = profile.get("review_layer") or profile.get("expert_role")
        normalized = {
            "通用审查": "general_review", "职业专家审查": "occupational_expert_review",
            "职业专家": "occupational_expert_review", "终审": "final_review",
        }.get(role, role)
        if normalized not in expected_roles:
            raise ReviewContractError(
                "expert profile %d has an invalid review layer" % index)
        roles.add(normalized)
        profile["review_layer"] = normalized
    if roles != expected_roles:
        raise ReviewContractError("expert profiles must cover all three review layers")
    return profiles


def _deliverable_sources(task, delivery, record):
    provenance = _read_json(task / "gold_provenance.json")
    rows = provenance.get("real_deliverable_files")
    if not isinstance(rows, list) or not rows:
        raise ReviewContractError(
            "gold_provenance.real_deliverable_files is required")
    declared = [Path(str(raw)).as_posix()
                for raw in record.get("deliverable_files") or []]
    by_basename = {}
    for rel in declared:
        by_basename.setdefault(Path(rel).name, []).append(rel)
    by_path = {}
    for index, row in enumerate(rows, start=1):
        raw_path = row.get("path")
        if raw_path:
            relpath = Path(str(raw_path))
            key = relpath.as_posix()
            if relpath.is_absolute() or ".." in relpath.parts or key not in declared:
                raise ReviewContractError(
                    "deliverable source %d has an unsafe or undeclared path" % index)
        else:
            name = str(row.get("filename") or "")
            matches = by_basename.get(name) or []
            if len(matches) != 1:
                raise ReviewContractError(
                    "deliverable source %d needs an unambiguous delivery path" % index)
            key = matches[0]
        if key in by_path:
            raise ReviewContractError(
                "duplicate deliverable source registration: %s" % key)
        by_path[key] = row
    result = []
    for rel in record.get("deliverable_files") or []:
        path = delivery / rel
        name = Path(rel).name
        source = by_path.get(Path(str(rel)).as_posix())
        if not source:
            raise ReviewContractError("deliverable source is not registered: %s" % name)
        url = str(source.get("source_url") or "")
        parsed = urlparse(url)
        source_sha = str(source.get("source_sha256") or "").lower()
        required = ("rights_holder", "license", "acquired_at")
        missing = [field for field in required
                   if not str(source.get(field) or "").strip()]
        if missing:
            raise ReviewContractError(
                "deliverable source %s is missing %s" %
                (name, ", ".join(missing)))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ReviewContractError("deliverable needs a real source URL: %s" % name)
        if not SHA256.fullmatch(source_sha):
            raise ReviewContractError("deliverable source SHA-256 is invalid: %s" % name)
        current_sha = _sha256(path)
        if source_sha != current_sha:
            raise ReviewContractError(
                "deliverable is not an exact source-byte copy: %s" % name)
        source_type = str(source.get("source_type") or
                          provenance.get("source_type") or "").strip()
        if not source_type:
            raise ReviewContractError(
                "deliverable source %s is missing source_type" % name)
        result.append({"path": rel, "filename": name, "source_url": url,
                       "source_sha256": source_sha, "current_sha256": current_sha,
                       "source_type": source_type,
                       "rights_holder": source["rights_holder"],
                       "license": source["license"],
                       "acquired_at": source["acquired_at"],
                       "transformation_record": "none; exact source bytes"})
    if set(by_path) != set(declared):
        raise ReviewContractError(
            "gold provenance and current deliverable inventory do not match")
    return result


def prepare_review_input(task_root, delivery_root, task_id, policy, basis,
                         occupation_standard):
    """Validate and normalize everything a reviewer package may rely on."""
    task = Path(task_root)
    delivery = Path(delivery_root)
    meta = _read_json(task / "task_meta.json")
    for field in ("task_id", "sector", "occupation", "language", "rubric_version"):
        if not meta.get(field):
            raise ReviewContractError("task_meta.json is missing %s" % field)
    if meta["task_id"] != task_id:
        raise ReviewContractError("task_meta task_id does not match workflow")
    _validate_task_language(task, meta)
    record = _task_record(delivery, task_id)
    profiles = _profiles(task, policy)
    provenance = _read_json(task / "provenance.json")
    source_inventory = _read_json(task / "source_inventory.json")
    if not isinstance(source_inventory, list) or not source_inventory:
        raise ReviewContractError("source_inventory.json must be a non-empty list")
    for index, source in enumerate(source_inventory, start=1):
        if source.get("adopted") is False:
            if not source.get("rejection_reason"):
                raise ReviewContractError(
                    "rejected source %d needs a rejection reason" % index)
            continue
        missing = [name for name in
                   ("source_id", "source_type", "description", "source_url", "license")
                   if not str(source.get(name) or "").strip()]
        if missing:
            raise ReviewContractError(
                "adopted source %d is missing %s" % (index, ", ".join(missing)))
        source_type = str(source.get("source_type") or "").strip().lower()
        if source_type == "synthetic":
            raise ReviewContractError(
                "adopted reference source %d cannot use source_type synthetic" % index)
        transformed = bool(str(source.get("transformation_record") or "").strip())
        if source_type == "desensitization" and not transformed:
            raise ReviewContractError(
                "desensitized reference source %d needs a transformation record" % index)
        if transformed and source_type != "desensitization":
            raise ReviewContractError(
                "transformed reference source %d must use source_type desensitization" % index)
    gold_provenance = _read_json(task / "gold_provenance.json")
    accepted_types = set(((policy.get("gold_source") or {}).get("accepted_paths") or []))
    if accepted_types and gold_provenance.get("source_type") not in accepted_types:
        raise ReviewContractError("gold provenance source_type is not allowed by policy")
    defaults = provenance.get("defaults") or {}
    for field in ("rights_holder", "license", "usage_scope"):
        if not str(defaults.get(field) or "").strip():
            raise ReviewContractError("provenance defaults are missing %s" % field)
    rights = evaluate_usage_rights(provenance, task_id, task)
    if rights["errors"]:
        raise ReviewContractError("invalid usage rights: %s" %
                                  "; ".join(rights["errors"]))
    authorization = rights["authorization"]
    boundaries = rights["boundaries"]
    if not occupation_standard or not occupation_standard.get("digest"):
        raise ReviewContractError("occupation_standard artifact is required")

    files = []
    for path in sorted(p for p in task.iterdir() if p.is_file()):
        files.append(_file_entry(task, path, "task_input"))
    files.append(_file_entry(delivery, delivery / "tasks.jsonl", "delivery_index"))
    for field in ("reference_files", "deliverable_files"):
        for raw in record.get(field) or []:
            rel = Path(str(raw))
            if rel.is_absolute() or ".." in rel.parts:
                raise ReviewContractError("unsafe delivery path: %s" % raw)
            files.append(_file_entry(delivery, delivery / rel, field))

    return {
        "schema_version": "review-input-v1",
        "task": {
            "task_id": task_id,
            "task_name": meta.get("task_name") or task.name,
            "sector": meta["sector"], "occupation": meta["occupation"],
            "language": meta["language"],
            "current_version": meta.get("task_version") or meta["rubric_version"],
            "rubric_version": meta["rubric_version"],
            "basis_digest": basis["digest"],
        },
        "files": sorted(files, key=lambda item: (item["scope"], item["path"])),
        "deliverable_sources": _deliverable_sources(task, delivery, record),
        "reference_sources": source_inventory,
        "rights": {
            "rights_holder": defaults["rights_holder"],
            "license": defaults["license"], "usage_scope": defaults["usage_scope"],
            "authorization": authorization, "usage_boundaries": boundaries,
        },
        "occupation_standard": occupation_standard,
        "expert_profiles": profiles,
        "review_layers": {
            "general_review": ["checklist", "findings", "verdict", "opinion"],
            "occupational_expert_review": ["occupation_mapping", "rubric_adoption",
                                                "gold_item_scoring", "findings",
                                                "verdict", "opinion"],
            "final_review": ["sequence_confirmation", "finding_closure_confirmation",
                             "verdict", "opinion"],
            "project_transcription": ["reviewer_id", "reviewer_title", "reviewed_at",
                                      "credential_status"],
        },
    }

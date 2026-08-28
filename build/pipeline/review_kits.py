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
                          _bundle_manifest, _now, _sha256)
from package import write_archive


LAYERS = ("general_review", "occupational_expert_review", "final_review")
PHASE1_LAYERS = LAYERS[:2]
REVIEW_ARTIFACTS = {
    "general_review": "general_review_receipt",
    "occupational_expert_review": "occupational_review_receipt",
    "final_review": "final_review_receipt",
}
TRANSCRIPTION_FIELDS = {
    "task_id", "layer", "reviewer_id", "reviewer_title", "reviewed_at",
    "credential_status",
}
HERE = Path(__file__).resolve().parent
BUILDER = HERE / "review_workbooks.mjs"


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
                 {"candidate_delivery_package", "phase1_review_kit",
                  "final_review_package", "review_remediation",
                  "pre_final_validation_evidence", "validation_evidence",
                  "human_review_record"}}
    basis = {
        "task_id": state["task_id"],
        "artifacts": artifacts,
        "rubric_version": meta.get("rubric_version", "unversioned"),
        "rubric_sha256": _sha256(task / "rubric.json"),
        "prompt_sha256": _sha256(task / "prompt.md"),
        "rubric_snapshot": _rubric_snapshot(rubric, codes),
    }
    basis["digest"] = _canonical_digest(basis)
    return basis


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
    return {
        "task_id": basis["task_id"],
        "sector": meta.get("sector", ""),
        "occupation": meta.get("occupation", ""),
        "language": meta.get("language", ""),
        "rubric_version": basis["rubric_version"],
        "candidate_sha256": candidate_sha,
    }


def _phase1_configs(meta, rubric, codes, basis, candidate_sha):
    task = _base_task_config(meta, basis, candidate_sha)
    general = {
        "layer": "general_review", "title": "GDPval General Review",
        "task": task,
        "checklist": [
            {"id": "G01", "text": "All required files open and the package inventory matches the supplied files."},
            {"id": "G02", "text": "Agent-visible references contain no Gold, expected values, rubric execution or validation evidence."},
            {"id": "G03", "text": "Prompt, rubric and file language are internally consistent and contain no placeholders."},
            {"id": "G04", "text": "The task is solvable from the supplied references without external research or invented facts."},
            {"id": "G05", "text": "Source URLs, provenance, usage scope and redistribution restrictions are clearly disclosed."},
            {"id": "G06", "text": "No personal data, secret, local path, hidden answer or unsupported credential claim is exposed."},
            {"id": "G07", "text": "Deliverables render legibly without clipping, overlap, blank pages or broken formulas."},
            {"id": "G08", "text": "Any issue is recorded once in the Findings sheet with severity, location and recommendation."},
        ],
    }
    items = []
    for code, item in zip(codes, rubric):
        check = item.get("check") or {}
        machine = "human"
        if isinstance(check, dict) and not check.get("human"):
            machine = check.get("type") or "machine-checkable"
        items.append({
            "code": code,
            "rubric_item_id": item.get("rubric_item_id"),
            "required": item.get("required", True),
            "max_score": item.get("score"),
            "criterion": item.get("criterion", ""),
            "verification": item.get("verification", ""),
            "machine_result": machine,
        })
    occupational = {
        "layer": "occupational_expert_review",
        "title": "GDPval Occupational Expert Review",
        "task": task,
        "mapping": {
            "proposed": "%s / %s" % (meta.get("sector", ""), meta.get("occupation", "")),
            "boundary": ((meta.get("guards") or {}).get("occupation_boundary") or
                         "Accept only within the task role and authority described by the prompt."),
        },
        "checklist": [
            {"id": "E01", "text": "The proposed industry and occupation mapping is professionally reasonable within the stated role boundary."},
            {"id": "E02", "text": "The references support the professional decisions required by the prompt without unsupported assumptions."},
            {"id": "E03", "text": "Terms, calculations, exceptions and operational boundaries match normal professional practice."},
            {"id": "E04", "text": "The Gold is usable in the stated work context and does not overclaim authority, approval or credentials."},
            {"id": "E05", "text": "Every rubric row has been independently adopted, revised or rejected and scored against the Gold."},
        ],
        "rubrics": items,
    }
    return general, occupational


FINAL_CHECKLIST = [
    {"id": "F01", "text": "The two earlier original receipts and their project-side transcriptions match the listed SHA-256 values."},
    {"id": "F02", "text": "Every earlier finding has one supported disposition and no blocker or major finding remains open."},
    {"id": "F03", "text": "The reviewed rubric version, Gold marking and current package basis are mutually consistent."},
    {"id": "F04", "text": "Pre-final validation has no failed check; any not-run item is limited to this final review."},
    {"id": "F05", "text": "The final verdict does not expand the declared occupation, authority, credential, licence or redistribution boundary."},
]


def _final_config(meta, basis, candidate_sha, cycle):
    first = cycle["phase1"]["receipts"]
    return {
        "layer": "final_review", "title": "GDPval Final Review",
        "task": _base_task_config(meta, basis, candidate_sha),
        "final_evidence": [
            {"label": "General receipt", "value": first["general_review"]["source_receipt_sha256"]},
            {"label": "General reviewed at", "value": first["general_review"]["reviewed_at"]},
            {"label": "Expert receipt", "value": first["occupational_expert_review"]["source_receipt_sha256"]},
            {"label": "Expert reviewed at", "value": first["occupational_expert_review"]["reviewed_at"]},
            {"label": "Post-remediation basis", "value": basis["digest"]},
            {"label": "Pre-final validation time", "value": cycle["pre_final_validation"]["run_at"]},
            {"label": "Pre-final validation", "value": cycle["pre_final_validation"]["evidence_digest"]},
        ],
        "checklist": FINAL_CHECKLIST,
        "finding_closure": [{
            "finding_id": item["finding_id"],
            "source_layer": next(layer for layer in PHASE1_LAYERS
                                 if item["finding_id"] in first[layer]["finding_ids"]),
            "disposition": item["disposition"], "closed_at": item["closed_at"],
            "rationale": item["rationale"],
            "evidence_sha256": "; ".join(
                "%s=%s" % pair for pair in sorted(
                    (item.get("evidence_sha256") or {}).items())),
        } for item in cycle["remediation"]["findings"]],
    }


def _write_package(tree, output, prefix):
    return write_archive(str(tree), str(output), prefix)


def create_phase1(pipeline, delivery, tasks_root, staging_root, output_dir,
                  node, node_modules):
    state = pipeline._load()
    basis = production_basis(pipeline, tasks_root)
    task, meta, rubric, codes = _task_data(tasks_root, state["task_id"])
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
            meta, rubric, codes, basis, candidate_info["sha256"])

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
            archive = output_dir / (role.name + ".zip")
            role_archives[label] = _write_package(role, archive, role.name)

        kit = tmp / "Phase-1-Human-Review-Kit"
        kit.mkdir()
        for archive in role_archives.values():
            _copy_file(archive["path"], kit / Path(archive["path"]).name)
        _write_json(kit / "review_kit_manifest.json", {
            "schema_version": "staged-xlsx-v1",
            "task_id": state["task_id"], "basis_digest": basis["digest"],
            "candidate_sha256": candidate_info["sha256"],
            "review_packages": {name: value["sha256"]
                                for name, value in role_archives.items()},
            "return_contract": "one completed XLSX per reviewer",
        })
        kit_zip = output_dir / "Phase-1-Human-Review-Kit.zip"
        kit_info = _write_package(kit, kit_zip, kit.name)

    candidate_artifact = pipeline.add_artifact(
        "candidate_delivery_package", [candidate_zip], "review-kit")
    kit_artifact = pipeline.add_artifact("phase1_review_kit", [kit_zip], "review-kit")
    state = pipeline._load()
    state["review_cycle"] = {
        "cycle_id": str(uuid4()), "status": "awaiting_phase1_reviews",
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
            normalized = Path("xl", target).as_posix()
            if Path(target).is_absolute() or ".." in Path(normalized).parts:
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


def _metadata(cells):
    return {label: _cell(cells, "B%d" % _row_for_label(cells, label))
            for label in ("Task ID", "Rubric version", "Candidate SHA-256")}


def _findings(sheets):
    cells = sheets.get("Findings")
    if cells is None:
        raise PipelineError("returned review XLSX is missing the Findings sheet")
    result = []
    for row in range(4, 24):
        values = [_cell(cells, "%s%d" % (column, row))
                  for column in "ABCDEF"]
        finding_id, severity, location, issue, recommendation, confirmation = values
        if not any(values[1:5]):
            if confirmation not in ("", "No"):
                raise PipelineError("unused finding rows must keep confirmation=No")
            continue
        if finding_id not in {"%s-F%02d" % (prefix, index)
                              for prefix in ("G", "E")
                              for index in range(1, 21)}:
            raise PipelineError("finding ID was altered: %s" % finding_id)
        if severity not in ("Blocker", "Major", "Minor"):
            raise PipelineError("%s has an invalid severity" % finding_id)
        if not all(str(value).strip() for value in
                   (finding_id, location, issue, recommendation)):
            raise PipelineError("%s is incomplete" % (finding_id or "finding"))
        if confirmation not in ("Yes", "No"):
            raise PipelineError("%s has an invalid confirmation decision" % finding_id)
        result.append({
            "finding_id": finding_id, "severity": severity,
            "location": location, "issue": issue,
            "recommendation": recommendation,
            "requires_confirmation": confirmation == "Yes",
        })
    return result


def _check_rows(cells, prefix, decisions):
    result = []
    for address, value in cells.items():
        if not address.startswith("A") or not isinstance(value, str) \
                or not value.startswith(prefix) or not value[len(prefix):].isdigit():
            continue
        row = int(address[1:])
        decision = _cell(cells, "C%d" % row)
        if decision not in decisions:
            raise PipelineError("%s has no valid workbook decision" % value)
        result.append({
            "id": value, "decision": decision,
            "comment": _cell(cells, "D%d" % row),
        })
    return sorted(result, key=lambda item: item["id"])


def _return_fields(cells):
    verdict = _cell(cells, "B%d" % _row_for_label(cells, "Conclusion"))
    opinion = _cell(cells, "B%d" % _row_for_label(cells, "Substantive opinion"))
    if verdict not in ("Pass", "Conditional pass", "Fail"):
        raise PipelineError("returned review XLSX has no valid conclusion")
    if not opinion:
        raise PipelineError("returned review XLSX needs a substantive opinion")
    return verdict, opinion


def _display_literal(value):
    if isinstance(value, str) and len(value) >= 11 and value[4:5] == "-" \
            and value[10:11] == "T":
        return "ISO-8601 " + value
    return value


def _parse_review_workbook(layer, path, task_id, meta, rubric, codes,
                           expected_candidate_sha, expected_config):
    sheets = _xlsx_cells(path)
    main_name = {
        "general_review": "General Review",
        "occupational_expert_review": "Occupation Review",
        "final_review": "Final Review",
    }[layer]
    cells = sheets.get(main_name)
    if cells is None:
        raise PipelineError("returned review XLSX is missing sheet: %s" % main_name)
    expected_meta = {
        "Task ID": task_id,
        "Rubric version": meta.get("rubric_version", "unversioned"),
        "Candidate SHA-256": expected_candidate_sha,
    }
    if _metadata(cells) != expected_meta:
        raise PipelineError(
            "returned review XLSX metadata does not match the frozen review package")
    verdict, opinion = _return_fields(cells)
    parsed = {"verdict": verdict, "opinion": opinion}
    if layer == "general_review":
        checklist = _check_rows(cells, "G", {"Pass", "Issue", "N/A"})
        expected_checks = expected_config["checklist"]
        if ([item["id"] for item in checklist] !=
                [item["id"] for item in expected_checks]):
            raise PipelineError("general review checklist is incomplete")
        for actual, expected in zip(checklist, expected_checks):
            row = _row_for_label(cells, actual["id"])
            if _cell(cells, "B%d" % row) != expected["text"]:
                raise PipelineError("general review checklist text was altered")
        findings = _findings(sheets)
        if sum(item["decision"] == "Issue" for item in checklist) != len(findings):
            raise PipelineError("general checklist issues must match Findings rows")
        parsed.update({"checklist": checklist, "findings": findings})
    elif layer == "occupational_expert_review":
        mapping_decision = _cell(
            cells, "B%d" % _row_for_label(cells, "Decision"))
        mapping_reason = _cell(
            cells, "B%d" % _row_for_label(cells, "Substantive reason"))
        mapping = expected_config["mapping"]
        if (_cell(cells, "B%d" % _row_for_label(cells, "Proposed mapping")) !=
                mapping["proposed"] or
                _cell(cells, "B%d" % _row_for_label(cells, "Boundary")) !=
                mapping["boundary"]):
            raise PipelineError("occupational mapping basis was altered")
        if mapping_decision not in ("Accept", "Conditional accept", "Reject") \
                or not mapping_reason:
            raise PipelineError(
                "occupational mapping needs a decision and substantive reason")
        checklist = _check_rows(cells, "E", {"Pass", "Issue", "N/A"})
        expected_checks = expected_config["checklist"]
        if ([item["id"] for item in checklist] !=
                [item["id"] for item in expected_checks]):
            raise PipelineError("occupational checklist is incomplete")
        for actual, expected in zip(checklist, expected_checks):
            row = _row_for_label(cells, actual["id"])
            if _cell(cells, "B%d" % row) != expected["text"]:
                raise PipelineError("occupational checklist text was altered")
        rubric_cells = sheets.get("Rubric and Gold")
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
                    "%s\nMachine: %s" % (item.get("verification", ""),
                                           expected_config["rubrics"][index - 4]["machine_result"])):
                raise PipelineError("expert workbook rubric row %s was altered" % code)
            adoption = _cell(rubric_cells, "G%d" % index)
            reason = _cell(rubric_cells, "H%d" % index)
            score = _cell(rubric_cells, "I%d" % index)
            evidence = _cell(rubric_cells, "J%d" % index)
            if adoption not in ("Adopt", "Revise", "Reject"):
                raise PipelineError("%s has no valid adoption decision" % code)
            if adoption != "Adopt" and not reason:
                raise PipelineError("%s revise/reject needs a reason" % code)
            if not isinstance(score, int) or score < 0 or score > item.get("score"):
                raise PipelineError("%s Gold score is outside its rubric maximum" % code)
            if not evidence:
                raise PipelineError("%s Gold score needs evidence or a reason" % code)
            rows.append({
                "code": code, "adoption": adoption,
                "reason_or_revision": reason, "gold_score": score,
                "gold_evidence_or_reason": evidence,
            })
        workbook_codes = {value for address, value in rubric_cells.items()
                          if address.startswith("A") and isinstance(value, str)
                          and value.startswith("R") and value[1:].isdigit()}
        if workbook_codes != set(codes):
            raise PipelineError("expert workbook has unexpected rubric rows")
        findings = _findings(sheets)
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
        checklist = _check_rows(cells, "F", {"Confirmed", "Issue"})
        expected_checks = expected_config["checklist"]
        if ([item["id"] for item in checklist] !=
                [item["id"] for item in expected_checks]):
            raise PipelineError("final checklist is incomplete")
        for actual, expected in zip(checklist, expected_checks):
            row = _row_for_label(cells, actual["id"])
            if _cell(cells, "B%d" % row) != expected["text"]:
                raise PipelineError("final checklist text was altered")
        check_header = _row_for_label(cells, "ID")
        evidence_decisions = []
        for row, expected in enumerate(expected_config["final_evidence"], start=12):
            label = _cell(cells, "A%d" % row)
            value = _cell(cells, "B%d" % row)
            if label != expected["label"] or value != _display_literal(expected["value"]):
                raise PipelineError("final frozen evidence rows were altered")
            decision = _cell(cells, "C%d" % row)
            if decision not in ("Confirmed", "Issue"):
                raise PipelineError("final evidence row %s is incomplete" % label)
            evidence_decisions.append({"label": label, "decision": decision})
        if check_header != 14 + len(expected_config["final_evidence"]):
            raise PipelineError("final frozen evidence section has unexpected rows")
        closure_cells = sheets.get("Finding Closure")
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
            decision = _cell(closure_cells, "G%d" % index)
            if decision not in ("Confirmed", "Issue"):
                raise PipelineError("final finding %s is not checked" % item["finding_id"])
            dispositions.append({
                "finding_id": item["finding_id"],
                "disposition": item["disposition"],
                "rationale": item["rationale"],
                "evidence_files": [part.split("=", 1)[0] for part in
                                   item["evidence_sha256"].split("; ") if part],
                "closed_at": item["closed_at"], "final_check": decision,
            })
        empty_closure_issue = []
        if not expected:
            if ([_cell(closure_cells, "%s4" % column) for column in "ABCDEF"] !=
                    ["None", "", "", "", "", ""]):
                raise PipelineError("empty final finding closure row was altered")
            decision = _cell(closure_cells, "G4")
            if decision not in ("Confirmed", "Issue"):
                raise PipelineError("final reviewer must confirm that no findings exist")
            if decision == "Issue":
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
        expected_findings = {item["finding_id"]
                             for item in remediation.get("findings", [])}
        dispositions = record.get("finding_dispositions") or []
        recorded_findings = {item.get("finding_id") for item in dispositions}
        if recorded_findings != expected_findings:
            raise PipelineError(
                "final receipt must confirm every remediated finding")
        boundaries.extend(_iso_time(item["closed_at"], "finding closed_at")
                          for item in remediation.get("findings", []))
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
            verdicts = [cycle["phase1"]["receipts"][name]["verdict"]
                        for name in PHASE1_LAYERS]
            cycle["status"] = ("remediation_required" if
                               all(value == "Pass" for value in verdicts) else
                               "phase1_review_failed")
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
    if cycle.get("status") != "remediation_required":
        raise PipelineError("remediation requires two current passing phase-1 receipts")
    receipts = (cycle.get("phase1") or {}).get("receipts") or {}
    if not all(receipts.get(layer) for layer in PHASE1_LAYERS):
        raise PipelineError("remediation requires both phase-1 receipts")
    if closure.get("task_id") != state["task_id"]:
        raise PipelineError("remediation task_id mismatch")
    expected = {finding_id for layer in PHASE1_LAYERS
                for finding_id in receipts[layer].get("finding_ids", [])}
    findings = closure.get("findings") or []
    recorded = {item.get("finding_id") for item in findings}
    if recorded != expected:
        raise PipelineError("remediation must dispose every finding; expected %s, recorded %s" %
                            (sorted(expected), sorted(recorded)))
    sources = [closure_path]
    for item in findings:
        finding_id = item.get("finding_id")
        source_layer = next(
            layer for layer in PHASE1_LAYERS
            if finding_id in receipts[layer].get("finding_ids", []))
        source_finding = next(
            value for value in receipts[source_layer]["record"]["findings"]
            if value["finding_id"] == finding_id)
        if source_finding.get("requires_confirmation"):
            raise PipelineError(
                "finding %s requires the original reviewer to confirm the changed "
                "item; create a new phase-1 review kit for the remediated basis" %
                finding_id)
        if item.get("disposition") not in ("closed", "accepted_without_change"):
            raise PipelineError("finding %s is unresolved" % finding_id)
        if not str(item.get("rationale") or "").strip():
            raise PipelineError("finding %s has no rationale" % item.get("finding_id"))
        closed_at = _iso_time(item.get("closed_at"), "finding closed_at")
        reviewed_at = _iso_time(
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
            sources.append(source)
    basis = production_basis(pipeline, tasks_root)
    closure["from_basis_digest"] = cycle["initial_basis"]["digest"]
    closure["to_basis_digest"] = basis["digest"]
    normalized_root = pipeline.root / "gates" / "remediation" / str(uuid4())
    normalized_root.mkdir(parents=True)
    normalized = normalized_root / "normalized_remediation.json"
    _write_json(normalized, closure)
    sources.append(normalized)
    artifact = pipeline.add_artifact("review_remediation", sources, "remediation")
    state = pipeline._load()
    state["review_cycle"]["remediation"] = {
        "recorded_at": _now(), "artifact_digest": artifact["digest"],
        "from_basis_digest": closure["from_basis_digest"],
        "to_basis_digest": closure["to_basis_digest"],
        "findings": findings,
    }
    state["review_cycle"]["status"] = "pre_final_validation_required"
    pipeline._save(state)
    return state["review_cycle"]["remediation"]


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
    current_delivery_digest, _delivery_files = _bundle_manifest(delivery)
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
        config = _final_config(meta, basis, candidate_info["sha256"], cycle)
        _run_builder(config, tree / "Final-Review.xlsx", node, node_modules)
        shutil.copytree(str(candidate),
                        str(tree / "Read-Only-Materials" /
                            "Post-Remediation-Candidate"))
        for layer, receipt in first.items():
            artifact = state["artifacts"][REVIEW_ARTIFACTS[layer]]
            shutil.copytree(
                str(pipeline.root / artifact["path"]),
                str(tree / "Read-Only-Materials" / "Phase-1-Receipts" / layer))
        remediation_artifact = state["artifacts"]["review_remediation"]
        shutil.copytree(
            str(pipeline.root / remediation_artifact["path"]),
            str(tree / "Read-Only-Materials" / "Remediation"))
        validation_artifact = state["artifacts"]["pre_final_validation_evidence"]
        shutil.copytree(
            str(pipeline.root / validation_artifact["path"]),
            str(tree / "Read-Only-Materials" / "Pre-Final-Validation"))
        _write_json(tree / "Read-Only-Materials" / "final_review_manifest.json", {
            "task_id": state["task_id"], "basis_digest": basis["digest"],
            "delivery_digest": current_delivery_digest,
            "candidate_sha256": candidate_info["sha256"], "frozen_at": frozen_at,
            "phase1_receipts": {name: value["source_receipt_sha256"]
                                for name, value in first.items()},
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
        else:
            result = create_final(pipeline, args.delivery, args.tasks_root, args.output,
                                  args.node, args.node_modules)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (PipelineError, OSError, ValueError, KeyError) as exc:
        print("review-kit error: %s" % exc, file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create and validate one external three-layer expert confirmation.

The signed Markdown is intentionally external to the task package. It binds
frozen review material, asks three real reviewers to attest their own current
conclusions, and never writes into delivery/ or its manifests.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


LAYERS = ("general_review", "occupational_expert_review", "final_review")
REQUIRED_BINDINGS = (
    ("review_record_sha256", "审查记录 SHA-256", "sha256"),
    ("prompt_sha256", "Prompt SHA-256", "sha256"),
    ("reference_binding_digest", "Reference binding digest", "sha256"),
    ("deliverable_binding_digest", "Deliverable binding digest", "sha256"),
    ("gold_lineage_binding_digest", "Gold/lineage binding digest", "sha256"),
    ("rubric_version", "Rubric version", "text"),
    ("rubric_json_sha256", "Rubric JSON SHA-256", "sha256"),
    ("rubric_item_set_digest", "Rubric item-set digest", "sha256"),
    ("rubric_item_count", "Rubric 条目数", "integer"),
    ("rubric_total_score", "Rubric 总分", "number"),
    ("rubric_required_config", "Rubric required 配置", "text"),
    ("a10_record_sha256", "A10 record SHA-256", "sha256"),
    ("a11_record_sha256", "A11 record SHA-256", "sha256"),
    ("a12_record_sha256", "A12 record SHA-256", "sha256"),
)
SAFE_FILE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NUMBER = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _single_line(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing %s" % label)
    cleaned = value.strip()
    if "\r" in cleaned or "\n" in cleaned or "|" in cleaned:
        raise ValueError("%s must be one line and cannot contain |" % label)
    return cleaned


def _safe_file_part(value, label):
    cleaned = _single_line(value, label)
    if not SAFE_FILE_PART.fullmatch(cleaned):
        raise ValueError("%s is not safe for an external confirmation filename" % label)
    return cleaned


def _binding_value(value, kind, key):
    cleaned = _single_line(value, "binding %s" % key)
    if kind == "sha256" and not SHA256.fullmatch(cleaned):
        raise ValueError("binding %s must be a lowercase SHA-256" % key)
    if kind == "integer" and not cleaned.isdigit():
        raise ValueError("binding %s must be an integer" % key)
    if kind == "number" and not NUMBER.fullmatch(cleaned):
        raise ValueError("binding %s must be a non-negative decimal" % key)
    return cleaned


def _canonical_input(raw):
    task_package = _single_line(raw.get("task_package"), "task_package")
    task_id = _safe_file_part(raw.get("task_id"), "task_id")
    revision = _safe_file_part(raw.get("revision"), "revision")
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {item[0] for item in REQUIRED_BINDINGS}:
        raise ValueError("bindings must contain exactly the required frozen binding keys")
    checked = []
    for key, label, kind in REQUIRED_BINDINGS:
        checked.append({"key": key, "label": label,
                        "value": _binding_value(bindings[key], kind, key)})
    conclusions = raw.get("conclusions")
    if not isinstance(conclusions, dict) or set(conclusions) != set(LAYERS):
        raise ValueError("conclusions must contain exactly the three review layers")
    return {
        "task_package": task_package,
        "task_id": task_id,
        "revision": revision,
        "bindings": checked,
        "scope": _single_line(raw.get("scope"), "scope"),
        "conclusions": {layer: _single_line(conclusions[layer], layer)
                        for layer in LAYERS},
    }


def _input_digest(data):
    rendered = json.dumps(data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _render(data):
    digest = _input_digest(data)
    lines = [
        "# 专家审查确认函 / Expert Review Confirmation", "",
        "## 固定绑定 / Bound review", "",
        "- 任务包 / Task package: `%s`" % data["task_package"],
        "- Task ID: `%s`" % data["task_id"],
        "- 当前 revision: `%s`" % data["revision"],
        "- 审查绑定摘要 SHA-256: `%s`" % digest,
    ]
    lines.extend("- %s: `%s`" % (item["label"], item["value"])
                 for item in data["bindings"])
    lines.extend([
        "", "## 审查范围", "", data["scope"], "",
        "本函确认当前 Prompt、Reference、Gold、lineage、Rubric 以及 A10–A12 整改闭合结论；后续 A13–A17 不在本函确认范围内。",
        "任一固定绑定内容发生变化，本函即失效。", "",
        "## 三层审查结论", "",
    ])
    lines.extend("- %s：%s" % (layer, data["conclusions"][layer]) for layer in LAYERS)
    lines.extend([
        "", "## 专家确认", "",
        "本人已审阅上述固定绑定内容及审查结论，并确认本层结论为本人当前意见。", "",
        "除下表姓名和日期外，请勿修改本文件任何字符。", "",
        "## 专家签署", "",
        "| 审查层 | Signed name | Date (YYYY-MM-DD) |",
        "|---|---|---|",
    ])
    lines.extend("| %s |  |  |" % layer for layer in LAYERS)
    return "\n".join(lines) + "\n"


def _paths(project_root, data):
    root = Path(project_root).resolve()
    pending = root / "待签署专家任务书" / (data["task_id"] + "_" + data["revision"] + "_专家审查确认函.md")
    archive = root / "专家签署函归档" / pending.name
    delivery_root = root / "delivery"
    for candidate in (pending, archive):
        if candidate.resolve().is_relative_to(delivery_root):
            raise ValueError("external confirmation must not be inside delivery/")
    return pending, archive


def create(args):
    data = _canonical_input(_load(args.input))
    output, _ = _paths(args.project_root, data)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("refusing to overwrite existing confirmation: %s" % output)
    output.write_bytes(_render(data).encode("utf-8"))
    print(json.dumps({"confirmation": str(output), "input_digest": _input_digest(data),
                      "sha256": _sha256(output)}, ensure_ascii=False))


def _parse_signed(expected, actual):
    prefix, marker, _ = expected.rpartition("| general_review |  |  |\n")
    if not marker:
        raise RuntimeError("confirmation template is missing signature marker")
    if not actual.startswith(prefix):
        raise ValueError("signed confirmation changes fixed content")
    tail = actual[len(prefix):]
    row = r"\| (?P<layer>%s) \| (?P<name>[^|\r\n]+) \| (?P<date>\d{4}-\d{2}-\d{2}) \|\n" % "|".join(LAYERS)
    if not re.fullmatch("(?:%s){3}" % row, tail):
        raise ValueError("signed confirmation may change only the three name and date cells")
    signatures = {}
    for match in re.finditer(row, tail):
        layer = match.group("layer")
        if layer in signatures:
            raise ValueError("signed confirmation repeats a review layer")
        name, date = match.group("name").strip(), match.group("date")
        try:
            dt.date.fromisoformat(date)
        except ValueError as exc:
            raise ValueError("%s date must be YYYY-MM-DD" % layer) from exc
        signatures[layer] = {"signed_name": name, "date": date}
    if tuple(signatures) != LAYERS:
        raise ValueError("signed confirmation must retain the three review layers in order")
    names = [signatures[layer]["signed_name"] for layer in LAYERS]
    if len(set(names)) != len(names):
        raise ValueError("three review layers require distinct signatories")
    return signatures


def verify(args):
    data = _canonical_input(_load(args.input))
    pending, archive_path = _paths(args.project_root, data)
    signed = Path(args.signed).resolve()
    if signed != pending.resolve():
        raise ValueError("signed confirmation must be the generated file in 待签署专家任务书/")
    signatures = _parse_signed(_render(data), signed.read_bytes().decode("utf-8"))
    result = {
        "schema_version": "external-combined-confirmation-v1",
        "task_package": data["task_package"], "task_id": data["task_id"],
        "revision": data["revision"], "review_basis_digest": _input_digest(data),
        "signed_confirmation_sha256": _sha256(signed),
        "record_type": "expert-confirmed review conclusions",
        "layers": [{"layer": layer, **signatures[layer], "status": "confirmed"}
                   for layer in LAYERS],
    }
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if _sha256(archive_path) != result["signed_confirmation_sha256"]:
            raise ValueError("archive already has a different file: %s" % archive_path)
    else:
        shutil.copy2(signed, archive_path)
    result["archive_path"] = str(archive_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("create", create), ("verify", verify)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--input", required=True,
                         help="JSON created from frozen review bindings and conclusions")
        cmd.add_argument("--project-root", required=True)
        if name == "verify":
            cmd.add_argument("--signed", required=True)
        cmd.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

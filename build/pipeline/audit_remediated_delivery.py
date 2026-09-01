#!/usr/bin/env python3
"""Read-only structural audit for a remediated GDPval delivery tree.

Exit codes:
  0  all checks passed
  1  the delivery is readable but one or more audit checks failed
  2  command-line usage error or the delivery root cannot be audited

The script uses only the Python standard library and never writes to the
delivery root.  It deliberately recomputes hashes and inspects OOXML parts
instead of trusting validation claims carried inside the package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from uuid import UUID, uuid5
from xml.etree import ElementTree


TASK_FIELDS = {
    "task_id",
    "sector",
    "occupation",
    "prompt",
    "reference_files",
    "reference_file_urls",
    "reference_file_hf_uris",
    "deliverable_files",
    "deliverable_file_urls",
    "deliverable_file_hf_uris",
    "rubric_pretty",
    "rubric_json",
}
PAYLOAD_DIRS = ("reference_files", "deliverable_files")
SELF_REFERENTIAL_MANIFESTS = {
    "manifests/file_inventory_sha256.txt",
    "manifests/provenance_manifest.jsonl",
    "manifests/validation_status.jsonl",
    "manifests/checksums_final.txt",
}
OOXML_SUFFIXES = {
    ".docx", ".docm", ".dotx", ".dotm",
    ".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm",
}
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
HASH_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+(\d+)\s+(.+?)\s*$")
FAIL_STATUSES = {"fail", "failed", "failure", "rejected", "error"}
TEXT_SUFFIXES = {
    ".csv", ".html", ".htm", ".json", ".jsonl", ".md", ".txt", ".xml", ".yaml", ".yml",
}
INTERNAL_WORKFLOW_PATTERNS = (
    re.compile(r"evaluator[-_ ]term", re.I),
    re.compile(r"evaluator[-_ ]side", re.I),
    re.compile(r"_shared_r4(?:[_./-][A-Za-z0-9_.-]+)?", re.I),
    re.compile(r"清理制作口径披露"),
    re.compile(r"对外交付采用中性业务表述"),
    re.compile(r"current_rebuilt_disclosure_cleaned_bytes", re.I),
    re.compile(r"answer[-_ ]artifact terminology", re.I),
)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str


class Audit:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.findings: list[Finding] = []
        self.tasks: list[dict[str, Any]] = []
        self.rubrics: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[str, Path] = {}

    def fail(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message))

    @staticmethod
    def main_language(text: str) -> str | None:
        """A conservative classifier for the only distinction this gate needs."""
        cjk = len(CJK_RE.findall(text))
        latin = len(re.findall(r"[A-Za-z]", text))
        if cjk >= 8 and cjk * 5 >= latin:
            return "zh"
        if latin >= 20 and latin * 5 > cjk:
            return "en"
        return None

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def collect_tree(self) -> None:
        for path in sorted(self.root.rglob("*")):
            rel = self.relative(path)
            if path.is_symlink():
                self.fail("TREE_SYMLINK", f"symbolic links are not permitted: {rel}")
                continue
            if path.is_file():
                self.files[rel] = path
                if path.name == ".DS_Store" or "__MACOSX" in path.parts:
                    self.fail("TREE_JUNK", f"forbidden packaging artefact: {rel}")

    def read_jsonl(self, rel: str, required: bool = True) -> list[Any]:
        path = self.root / rel
        if not path.is_file():
            if required:
                self.fail("MISSING_FILE", f"required file is missing: {rel}")
            return []
        rows: list[Any] = []
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        self.fail(
                            "JSONL_PARSE",
                            f"{rel}:{number}: invalid JSON: {exc.msg}",
                        )
        except (OSError, UnicodeError) as exc:
            self.fail("READ_ERROR", f"cannot read {rel}: {exc}")
        if required and not rows:
            self.fail("JSONL_EMPTY", f"no records found in {rel}")
        return rows

    @staticmethod
    def valid_relative_path(value: Any) -> bool:
        if not isinstance(value, str) or not value or "\\" in value:
            return False
        path = PurePosixPath(value)
        return (
            not path.is_absolute()
            and value == path.as_posix()
            and all(part not in {"", ".", ".."} for part in path.parts)
            and not re.match(r"^[A-Za-z]:", value)
            and not value.startswith("file:")
        )

    def check_tasks(self) -> None:
        rows = self.read_jsonl("tasks.jsonl")
        seen_task_ids: set[str] = set()
        declared: set[str] = set()

        for number, record in enumerate(rows, 1):
            label = f"tasks.jsonl record {number}"
            if not isinstance(record, dict):
                self.fail("TASK_SCHEMA", f"{label} must be a JSON object")
                continue
            self.tasks.append(record)
            actual_fields = set(record)
            if actual_fields != TASK_FIELDS:
                missing = sorted(TASK_FIELDS - actual_fields)
                extra = sorted(actual_fields - TASK_FIELDS)
                self.fail(
                    "TASK_12_FIELDS",
                    f"{label} must have exactly 12 fields; missing={missing}, extra={extra}",
                )

            task_id = record.get("task_id")
            try:
                parsed_id = UUID(task_id) if isinstance(task_id, str) else None
                valid_uuid = parsed_id is not None and str(parsed_id) == task_id
            except (ValueError, AttributeError):
                parsed_id = None
                valid_uuid = False
            if not valid_uuid:
                self.fail("TASK_UUID", f"{label} has invalid canonical UUID task_id: {task_id!r}")
            elif task_id in seen_task_ids:
                self.fail("TASK_UUID_DUPLICATE", f"duplicate task_id: {task_id}")
            else:
                seen_task_ids.add(task_id)

            for key in (
                "reference_files", "reference_file_urls", "reference_file_hf_uris",
                "deliverable_files", "deliverable_file_urls", "deliverable_file_hf_uris",
            ):
                if not isinstance(record.get(key), list):
                    self.fail("TASK_LIST_TYPE", f"{label}.{key} must be a list")

            files = record.get("deliverable_files")
            urls = record.get("deliverable_file_urls")
            if isinstance(files, list) and isinstance(urls, list):
                if len(files) != len(urls):
                    self.fail("DELIVERABLE_URL_COUNT",
                              f"{label} has {len(files)} deliverables but {len(urls)} source URLs")
                elif any(not isinstance(url, str) or not url.startswith(("https://", "http://"))
                         for url in urls):
                    self.fail("DELIVERABLE_URL", f"{label} has missing/invalid deliverable source URL")

            paths_by_kind = (
                ("reference_files", "reference_files", "reference_files"),
                ("deliverable_files", "deliverable_files", "deliverable_files"),
            )
            for key, prefix, uuid_name in paths_by_kind:
                values = record.get(key)
                if not isinstance(values, list):
                    continue
                expected_bundle = uuid5(parsed_id, uuid_name).hex if parsed_id else None
                for value in values:
                    if not self.valid_relative_path(value):
                        self.fail("PAYLOAD_PATH", f"{label}.{key} has unsafe path: {value!r}")
                        continue
                    parts = PurePosixPath(value).parts
                    if len(parts) < 3 or parts[0] != prefix:
                        self.fail("PAYLOAD_LAYOUT", f"unexpected {key} layout: {value}")
                    elif expected_bundle and parts[1] != expected_bundle:
                        self.fail(
                            "BUNDLE_UUID5",
                            f"{value}: bundle must be UUID5 {expected_bundle}",
                        )
                    if value in declared:
                        self.fail("PAYLOAD_DUPLICATE", f"payload declared more than once: {value}")
                    declared.add(value)
                    path = self.root / value
                    if not path.is_file():
                        self.fail("PAYLOAD_MISSING", f"declared payload is missing: {value}")
                    elif path.stat().st_size == 0:
                        self.fail("PAYLOAD_EMPTY", f"declared payload is empty: {value}")

            self.check_rubric(record, label)
            self.check_prompt_references(record, label)

        actual_payload = {
            rel for rel in self.files
            if PurePosixPath(rel).parts and PurePosixPath(rel).parts[0] in PAYLOAD_DIRS
        }
        for rel in sorted(actual_payload - declared):
            self.fail("PAYLOAD_UNDECLARED", f"payload exists but is not declared: {rel}")
        for rel in sorted(declared - actual_payload):
            if (self.root / rel).is_file():
                self.fail("PAYLOAD_NOT_REGULAR", f"declared payload is not a regular tree file: {rel}")

    def check_rubric(self, record: dict[str, Any], label: str) -> None:
        raw = record.get("rubric_json")
        if not isinstance(raw, str):
            self.fail("RUBRIC_JSON_TYPE", f"{label}.rubric_json must be a JSON string")
            return
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail("RUBRIC_JSON_PARSE", f"{label}.rubric_json: {exc.msg}")
            return
        if not isinstance(items, list):
            self.fail("RUBRIC_JSON_SCHEMA", f"{label}.rubric_json must decode to an array")
            return
        task_id = record.get("task_id")
        if isinstance(task_id, str):
            self.rubrics[task_id] = [i for i in items if isinstance(i, dict)]
        if len(items) < 25:
            self.fail("RUBRIC_COUNT", f"{label} has {len(items)} rubric items; minimum is 25")

        total = 0
        scores_valid = True
        ids: set[str] = set()
        pretty_lines: list[str] = []
        for index, item in enumerate(items, 1):
            item_label = f"{label} rubric item {index}"
            if not isinstance(item, dict):
                self.fail("RUBRIC_ITEM_SCHEMA", f"{item_label} must be an object")
                scores_valid = False
                continue
            score = item.get("score")
            if isinstance(score, bool) or not isinstance(score, int) or score < 0:
                self.fail("RUBRIC_SCORE", f"{item_label} has invalid integer score: {score!r}")
                scores_valid = False
            else:
                total += score
                pretty_lines.append(f"[+{score}] {item.get('criterion', '')}")

            rubric_id = item.get("rubric_item_id")
            try:
                valid_id = isinstance(rubric_id, str) and str(UUID(rubric_id)) == rubric_id
            except ValueError:
                valid_id = False
            if not valid_id:
                self.fail("RUBRIC_ITEM_UUID", f"{item_label} has invalid UUID: {rubric_id!r}")
            elif rubric_id in ids:
                self.fail("RUBRIC_ITEM_UUID_DUPLICATE", f"duplicate rubric_item_id: {rubric_id}")
            else:
                ids.add(rubric_id)

            prompt_language = self.main_language(str(record.get("prompt") or ""))
            for key in ("criterion", "verification"):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    self.fail("RUBRIC_TEXT", f"{item_label}.{key} must be non-empty text")
                elif (prompt_language and self.main_language(value)
                      and self.main_language(value) != prompt_language):
                    self.fail("RUBRIC_LANGUAGE", f"{item_label}.{key} language differs from prompt")
            if not isinstance(item.get("required"), bool):
                self.fail("RUBRIC_REQUIRED", f"{item_label}.required must be boolean")

        if scores_valid and total != 100:
            self.fail("RUBRIC_TOTAL", f"{label} rubric scores total {total}, expected 100")

        pretty = record.get("rubric_pretty")
        expected_pretty = "\n\n".join(pretty_lines)
        if not isinstance(pretty, str) or pretty.rstrip("\n") != expected_pretty:
            self.fail("RUBRIC_PRETTY_PARITY", f"{label}.rubric_pretty differs from rubric_json")

    def check_prompt_references(self, record: dict[str, Any], label: str) -> None:
        prompt = record.get("prompt")
        refs = record.get("reference_files")
        if not isinstance(prompt, str):
            self.fail("PROMPT_TYPE", f"{label}.prompt must be a string")
            return
        if not isinstance(refs, list):
            return
        for ref in refs:
            if isinstance(ref, str):
                basename = PurePosixPath(ref).name
                if basename not in prompt:
                    self.fail(
                        "PROMPT_REFERENCE_BASENAME",
                        f"{label}.prompt does not mention reference basename: {basename}",
                    )

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def check_office(self) -> None:
        for rel, path in self.files.items():
            if path.suffix.lower() not in OOXML_SUFFIXES:
                continue
            try:
                with zipfile.ZipFile(path) as package:
                    names = package.namelist()
                    lower_names = {name.lower(): name for name in names}
                    macro_parts = [
                        name for name in names
                        if name.lower().endswith("vbaproject.bin")
                        or "/activex/" in f"/{name.lower()}"
                    ]
                    if macro_parts:
                        self.fail("OFFICE_MACRO", f"{rel} contains macro/ActiveX parts: {macro_parts[:3]}")

                    external_parts = [name for name in names if "/externallinks/" in f"/{name.lower()}"]
                    external_rels: list[str] = []
                    for name in names:
                        if not name.lower().endswith(".rels"):
                            continue
                        try:
                            root = ElementTree.fromstring(package.read(name))
                        except ElementTree.ParseError:
                            self.fail("OFFICE_XML", f"{rel}!{name} is not valid XML")
                            continue
                        for relationship in root.iter():
                            if relationship.attrib.get("TargetMode", "").lower() == "external":
                                external_rels.append(
                                    f"{name} -> {relationship.attrib.get('Target', '')}"
                                )
                    if external_parts or external_rels:
                        evidence = (external_parts + external_rels)[:3]
                        self.fail("OFFICE_EXTERNAL_LINK", f"{rel} contains external links: {evidence}")

                    personal_values: list[str] = []
                    self._collect_personal_docprops(package, lower_names, personal_values)
                    if personal_values:
                        self.fail(
                            "OFFICE_PERSONAL_DOCPROPS",
                            f"{rel} contains personal document properties: {personal_values[:5]}",
                        )
            except (OSError, zipfile.BadZipFile) as exc:
                self.fail("OFFICE_PACKAGE", f"cannot inspect OOXML package {rel}: {exc}")

    def _collect_personal_docprops(
        self,
        package: zipfile.ZipFile,
        lower_names: dict[str, str],
        output: list[str],
    ) -> None:
        property_rules = {
            "docprops/core.xml": {"creator", "lastmodifiedby"},
            "docprops/app.xml": {"company", "manager"},
        }
        for wanted, local_names in property_rules.items():
            actual = lower_names.get(wanted)
            if not actual:
                continue
            try:
                root = ElementTree.fromstring(package.read(actual))
            except ElementTree.ParseError:
                self.fail("OFFICE_XML", f"{actual} is not valid XML")
                continue
            for element in root.iter():
                local = element.tag.rsplit("}", 1)[-1].lower()
                value = (element.text or "").strip()
                if local in local_names and value:
                    output.append(f"{local}={value!r}")

        custom = lower_names.get("docprops/custom.xml")
        if custom:
            try:
                root = ElementTree.fromstring(package.read(custom))
            except ElementTree.ParseError:
                self.fail("OFFICE_XML", f"{custom} is not valid XML")
                return
            personal_name = re.compile(r"author|creator|user|username|manager|company|person|owner", re.I)
            for prop in root.iter():
                name = prop.attrib.get("name", "")
                values = [((child.text or "").strip()) for child in prop if (child.text or "").strip()]
                if name and values and personal_name.search(name):
                    output.append(f"custom:{name}={values[0]!r}")

    def parse_hash_file(self, rel: str) -> dict[str, tuple[str, int]]:
        path = self.root / rel
        if not path.is_file():
            self.fail("MISSING_FILE", f"required file is missing: {rel}")
            return {}
        entries: dict[str, tuple[str, int]] = {}
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            self.fail("READ_ERROR", f"cannot read {rel}: {exc}")
            return {}
        for number, line in enumerate(lines, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = HASH_LINE_RE.match(line)
            if not match:
                self.fail("HASH_MANIFEST_FORMAT", f"{rel}:{number}: malformed hash line")
                continue
            digest, byte_text, target = match.groups()
            if not self.valid_relative_path(target):
                self.fail("HASH_MANIFEST_PATH", f"{rel}:{number}: unsafe path {target!r}")
                continue
            if target in entries:
                self.fail("HASH_MANIFEST_DUPLICATE", f"{rel}: duplicate path {target}")
                continue
            entries[target] = (digest.lower(), int(byte_text))
        if not entries:
            self.fail("HASH_MANIFEST_EMPTY", f"{rel} has no checksum entries")
        return entries

    def verify_hash_entries(self, manifest_rel: str, entries: dict[str, tuple[str, int]]) -> None:
        for target, (expected_hash, expected_bytes) in entries.items():
            path = self.root / target
            if not path.is_file():
                self.fail("HASH_TARGET_MISSING", f"{manifest_rel} lists missing file: {target}")
                continue
            actual_bytes = path.stat().st_size
            if actual_bytes != expected_bytes:
                self.fail(
                    "HASH_BYTES_MISMATCH",
                    f"{manifest_rel}: {target} bytes {actual_bytes}, expected {expected_bytes}",
                )
            actual_hash = self.sha256(path)
            if actual_hash != expected_hash:
                self.fail(
                    "HASH_MISMATCH",
                    f"{manifest_rel}: {target} sha256 {actual_hash}, expected {expected_hash}",
                )

    def check_inventories(self) -> None:
        inventory_rel = "manifests/file_inventory_sha256.txt"
        final_rel = "manifests/checksums_final.txt"
        inventory = self.parse_hash_file(inventory_rel)
        final = self.parse_hash_file(final_rel)
        self.verify_hash_entries(inventory_rel, inventory)
        self.verify_hash_entries(final_rel, final)

        expected_inventory = set(self.files) - {inventory_rel, final_rel}
        listed = set(inventory)
        for rel in sorted(expected_inventory - listed):
            self.fail("INVENTORY_COVERAGE", f"file missing from inventory: {rel}")
        for rel in sorted(listed - expected_inventory):
            self.fail("INVENTORY_SCOPE", f"unexpected inventory entry: {rel}")

    def check_provenance(self) -> None:
        rel = "manifests/provenance_manifest.jsonl"
        rows = self.read_jsonl(rel)
        by_path: dict[str, dict[str, Any]] = {}
        for number, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                self.fail("PROVENANCE_SCHEMA", f"{rel}:{number} must be an object")
                continue
            target = row.get("path")
            if not self.valid_relative_path(target):
                self.fail("PROVENANCE_PATH", f"{rel}:{number} has unsafe/missing path: {target!r}")
                continue
            if target in by_path:
                self.fail("PROVENANCE_DUPLICATE", f"duplicate provenance path: {target}")
                continue
            by_path[target] = row

        for target in sorted(set(self.files) - set(by_path)):
            self.fail("PROVENANCE_COVERAGE", f"file has no provenance entry: {target}")
        for target in sorted(set(by_path) - set(self.files)):
            self.fail("PROVENANCE_STALE_PATH", f"provenance lists missing file: {target}")

        for target, row in by_path.items():
            path = self.root / target
            if not path.is_file():
                continue
            digest = row.get("content_sha256")
            byte_count = row.get("bytes")
            if digest is None:
                if target not in SELF_REFERENTIAL_MANIFESTS:
                    self.fail("PROVENANCE_NULL_HASH", f"non-self-referential file has null hash: {target}")
                if not isinstance(row.get("content_sha256_note"), str) or not row["content_sha256_note"].strip():
                    self.fail("PROVENANCE_NULL_HASH_NOTE", f"null hash lacks explanation: {target}")
                continue
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                self.fail("PROVENANCE_HASH_FORMAT", f"invalid content_sha256 for {target}")
            elif self.sha256(path) != digest:
                self.fail("PROVENANCE_HASH_MISMATCH", f"provenance hash mismatch: {target}")
            if isinstance(byte_count, bool) or not isinstance(byte_count, int):
                self.fail("PROVENANCE_BYTES", f"invalid provenance bytes for {target}")
            elif path.stat().st_size != byte_count:
                self.fail("PROVENANCE_BYTES_MISMATCH", f"provenance byte count mismatch: {target}")
            if target.startswith("deliverable_files/"):
                url = row.get("source_url")
                source_digest = row.get("source_sha256")
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    self.fail("DELIVERABLE_SOURCE_URL", f"missing/invalid source URL: {target}")
                if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
                    self.fail("DELIVERABLE_SOURCE_HASH", f"missing/invalid source SHA-256: {target}")
                elif self.sha256(path) != source_digest:
                    self.fail("DELIVERABLE_SOURCE_HASH_MISMATCH",
                              f"delivered bytes differ from registered source: {target}")

    def required_lookup(self, task_id: str) -> tuple[dict[str, bool], dict[str, bool]]:
        by_id: dict[str, bool] = {}
        by_code: dict[str, bool] = {}
        for number, item in enumerate(self.rubrics.get(task_id, []), 1):
            required = item.get("required") is True
            rubric_id = item.get("rubric_item_id")
            if isinstance(rubric_id, str):
                by_id[rubric_id] = required
            by_code[f"R{number:02d}"] = required
        return by_id, by_code

    def check_required_gates(self) -> None:
        for rel, path in self.files.items():
            if path.name not in {"rubric_execution.json", "gold_human_marking.json"}:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.fail("SCORING_JSON", f"cannot parse {rel}: {exc}")
                continue
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                self.fail("SCORING_SCHEMA", f"{rel} must contain an items array")
                continue
            task_id = data.get("task_id")
            if not isinstance(task_id, str):
                task_id = next(
                    (part for part in PurePosixPath(rel).parts if part in self.rubrics),
                    "",
                )
            by_id, by_code = self.required_lookup(task_id)
            failed_required: list[str] = []
            for item in data["items"]:
                if not isinstance(item, dict):
                    continue
                required = item.get("required")
                if required is None:
                    required = by_id.get(item.get("rubric_item_id"), by_code.get(item.get("code"), False))
                status = str(item.get("status", "")).lower()
                awarded = item.get("awarded")
                failed = status in FAIL_STATUSES or (
                    isinstance(awarded, (int, float))
                    and not isinstance(awarded, bool)
                    and awarded == 0
                )
                if required is True and failed:
                    failed_required.append(str(item.get("code") or item.get("rubric_item_id") or "unknown"))

            if not failed_required:
                continue
            if path.name == "rubric_execution.json":
                if data.get("hard_gates_accepted") is not False:
                    self.fail(
                        "REQUIRED_GATE_OVERRIDE",
                        f"{rel} has failed required items {failed_required} but hard_gates_accepted is not false",
                    )
            else:
                accepted = data.get("counts_toward_acceptance") is True
                score = data.get("combined_score")
                threshold = data.get("threshold")
                threshold_pass = (
                    isinstance(score, (int, float)) and not isinstance(score, bool)
                    and isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
                    and score >= threshold
                )
                if accepted or threshold_pass:
                    self.fail(
                        "REQUIRED_SCORE_OVERRIDE",
                        f"{rel} accepts/passes by total score despite failed required items {failed_required}",
                    )

    def check_validation_truthfulness(self) -> None:
        rows = self.read_jsonl("manifests/validation_status.jsonl")
        pending_re = re.compile(
            r"\bpending\b|\bnot[_ -]?supplied\b|\bunresolved\b|\bawait(?:ing)?\b|"
            r"\bmissing\b|\bunknown\b|待确认|待提供|未提供|未确认|未取得|缺失|悬而未决",
            re.I,
        )
        scope_re = re.compile(
            r"credential|qualification|certif|rights?|licen[cs]e|copyright|permission|"
            r"authori[sz]ation|human.review|expert.review|final.review|资质|资格|权利|版权|"
            r"许可|授权|人工复核|专家复核|终审",
            re.I,
        )
        negated_re = re.compile(r"\bno\s+(?:pending|missing|unresolved)\b|无待确认|不存在待确认", re.I)
        for row_number, row in enumerate(rows, 1):
            if not isinstance(row, dict) or not isinstance(row.get("checks"), list):
                self.fail("VALIDATION_SCHEMA", f"validation_status.jsonl:{row_number} lacks checks array")
                continue
            for check in row["checks"]:
                if not isinstance(check, dict) or str(check.get("status", "")).lower() != "passed":
                    continue
                text = " ".join(str(check.get(k, "")) for k in ("check", "detail", "evidence_path"))
                if scope_re.search(text) and pending_re.search(text) and not negated_re.search(text):
                    self.fail(
                        "VALIDATION_PENDING_MISLABELLED",
                        f"passed check {check.get('check')!r} contains pending human credentials/rights state",
                    )

    @staticmethod
    def readable_text(path: Path) -> str | None:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            return None
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return None

    def check_public_delivery_hygiene(self) -> None:
        """Reject internal workflow residue without banning deterministic QA terms."""
        for rel, path in self.files.items():
            text = self.readable_text(path)
            surfaces = [rel]
            if text is not None:
                surfaces.append(text)
            for pattern in INTERNAL_WORKFLOW_PATTERNS:
                if any(pattern.search(surface) for surface in surfaces):
                    self.fail(
                        "PUBLIC_DELIVERY_INTERNAL_RESIDUE",
                        f"internal remediation/evaluator wording must stay outside delivery: {rel} "
                        f"(matched {pattern.pattern!r})",
                    )
                    break

    def run(self) -> None:
        self.collect_tree()
        self.check_tasks()
        self.check_office()
        self.check_inventories()
        self.check_provenance()
        self.check_required_gates()
        self.check_validation_truthfulness()
        self.check_public_delivery_hygiene()


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only audit of a remediated EPA/ABC delivery root.",
    )
    parser.add_argument("delivery_root", type=Path, help="path containing tasks.jsonl")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.delivery_root.expanduser()
    if not root.is_dir():
        print(f"AUDIT ERROR: delivery root is not a directory: {root}", file=sys.stderr)
        return 2
    root = root.resolve()
    audit = Audit(root)
    try:
        audit.run()
    except PermissionError as exc:
        print(f"AUDIT ERROR: permission denied while reading delivery: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"AUDIT ERROR: cannot audit delivery: {exc}", file=sys.stderr)
        return 2

    if audit.findings:
        print(f"AUDIT FAIL: {root} ({len(audit.findings)} finding(s))")
        for finding in audit.findings:
            print(f"- [{finding.code}] {finding.message}")
        return 1
    print(f"AUDIT PASS: {root} ({len(audit.files)} files, {len(audit.tasks)} task(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())

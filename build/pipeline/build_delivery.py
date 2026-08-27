"""Assemble the GDPval delivery tree from the staging artefacts.

Produces:
    delivery/
    ├── tasks.jsonl                     12-field record, one line
    ├── reference_files/<ref_bundle>/   Agent-visible inputs
    ├── deliverable_files/<dlv_bundle>/ Agent-invisible expert gold
    └── manifests/                      coverage, validation, provenance,
                                        source inventory, sha256 inventory

Bundle directory names are UUID5-derived from task_id exactly as the
specification requires, so the tree is byte-reproducible across rebuilds.
"""
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import date
from uuid import UUID, uuid5

sys.path.insert(0, os.path.dirname(__file__))
import taskdata as TD

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGING = os.environ.get("GDPVAL_STAGING", os.path.join(BASE, "staging"))

# Build into a scratch root, then publish. Some mounted filesystems disallow
# directory removal, which would leave stale files that the SHA-256 inventory
# would then pick up. Building clean elsewhere and syncing avoids that entirely.
BUILD_ROOT = os.environ.get("GDPVAL_BUILD_ROOT", "/tmp/lanyian_delivery_build")
DELIVERY = os.path.join(BUILD_ROOT, "delivery")
PUBLISH_TO = os.environ.get("GDPVAL_DELIVERY", os.path.join(BASE, "delivery"))

class TaskBuild:
    """Everything that is true of one task while the delivery is assembled.

    These were module globals, which is the same as saying the module could
    build exactly one task. The client's format is one delivery root holding
    every task, so the state that varies per task has to be per task.
    """

    def __init__(self, task_id):
        self.task = TD.TaskData(task_id)
        self.task_id = self.task.task_id
        self.meta = self.task.meta
        self.sector = self.meta["sector"]
        self.occupation = self.meta["occupation"]
        self.ref_bundle = uuid5(UUID(self.task_id), "reference_files").hex
        self.dlv_bundle = uuid5(UUID(self.task_id), "deliverable_files").hex
        self.staging = self._staging_dir()
        self.refs = self._staged("reference_files")
        self.dlvs = self._staged("deliverable_files")
        self.deliverable_sources = self._deliverable_sources()

    def _deliverable_sources(self):
        """Bind every staged gold file to its public, byte-identical source.

        A claim that a file is real is not evidence.  The curator supplies the
        immutable source digest and URL; this build verifies the staged bytes
        before publishing them.  Source paths deliberately stay evaluator-only.
        """
        declared = (self.task.gold_provenance or {}).get("real_deliverable_files")
        if not isinstance(declared, list) or not declared:
            raise SystemExit(
                "%s: gold_provenance.real_deliverable_files is required for "
                "untouched deliverables" % self.task_id)
        by_name = {}
        for entry in declared:
            if not isinstance(entry, dict):
                raise SystemExit("%s: real_deliverable_files entries must be objects"
                                 % self.task_id)
            name = entry.get("filename")
            url = entry.get("source_url")
            digest = entry.get("source_sha256")
            if (not isinstance(name, str) or not isinstance(url, str)
                    or not url.startswith(("https://", "http://"))
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise SystemExit("%s: invalid real-deliverable source entry: %r"
                                 % (self.task_id, entry))
            if name in by_name:
                raise SystemExit("%s: duplicate real-deliverable source: %s"
                                 % (self.task_id, name))
            by_name[name] = dict(entry)
        if set(by_name) != set(self.dlvs):
            raise SystemExit("%s: real-deliverable source names %s do not match "
                             "staged deliverables %s" %
                             (self.task_id, sorted(by_name), sorted(self.dlvs)))
        return by_name

    def _staging_dir(self):
        """Per-task staging, with the flat layout still accepted.

        One shared staging directory works until a second task exists, at which
        point the two overwrite each other's inputs silently.
        """
        scoped = os.path.join(STAGING, self.task_id)
        return scoped if os.path.isdir(scoped) else STAGING

    def _staged(self, kind):
        """File order follows the task's declared order, not the filesystem's.

        The record should introduce the files in the order the prompt does.
        Sorting alphabetically reads as arbitrary and, on the accepted package,
        is simply a different list. A mismatch between staging and the declared
        order is an error, not something to paper over with a sort.
        """
        folder = os.path.join(self.staging, kind)
        present = sorted(name for name in os.listdir(folder)
                         if not name.startswith("."))
        declared = (self.meta.get("file_order") or {}).get(kind)
        if not declared:
            return present
        if sorted(declared) != present:
            raise SystemExit(
                "%s / %s: staging holds %s but the task declares %s"
                % (self.task_id, kind, present, sorted(declared)))
        return list(declared)


BUILD_DATE = date.today().isoformat()
# Fixed so an unchanged package hashes the same on every build.
BUILD_DOC_DATE = "2026-08-10"
SELF_REFERENTIAL = ("manifests/provenance_manifest.jsonl",
                    "manifests/file_inventory_sha256.txt",
                    "manifests/validation_status.jsonl",
                    "manifests/checksums_final.txt")


def task_ids():
    """Which tasks this build covers: GDPVAL_TASK_ID, or every assembled task."""
    explicit = os.environ.get("GDPVAL_TASK_ID")
    if explicit:
        return [t.strip() for t in explicit.split(",") if t.strip()]
    root = TD.TASKS_ROOT
    return sorted(name for name in os.listdir(root)
                  if os.path.isdir(os.path.join(root, name)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_owned_delivery(path, label):
    """Refuse to mirror over or recursively clear an unrelated directory."""
    target = os.path.abspath(path)
    forbidden = {os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~")),
                 os.path.abspath(BASE), os.path.abspath(os.path.dirname(BASE))}
    if target in forbidden:
        raise SystemExit("refusing unsafe %s target: %s" % (label, target))
    if os.path.isdir(target) and os.listdir(target):
        owned = (os.path.isfile(os.path.join(target, "tasks.jsonl")) and
                 os.path.isdir(os.path.join(target, "manifests")))
        if not owned:
            raise SystemExit(
                "refusing to replace a non-delivery directory: %s" % target)


def publish():
    """Mirror the freshly built tree onto the delivery location.

    Mirror, not copy. Copying alone leaves behind anything the previous build
    produced under a name this one no longer uses — after the reference files
    were renamed, the published tree held twelve files where tasks.jsonl
    declared six, and the stale six were hashed into the inventory and shipped.
    Files present at the destination but absent from the build are removed.
    """
    if os.path.abspath(PUBLISH_TO) == os.path.abspath(DELIVERY):
        return PUBLISH_TO

    _assert_owned_delivery(PUBLISH_TO, "GDPVAL_DELIVERY")

    built = set()
    for root, _dirs, files in os.walk(DELIVERY):
        rel = os.path.relpath(root, DELIVERY)
        target = os.path.join(PUBLISH_TO, rel) if rel != "." else PUBLISH_TO
        os.makedirs(target, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(target, f))
            built.add(os.path.normpath(os.path.join(rel, f) if rel != "." else f))

    removed = []
    for root, _dirs, files in os.walk(PUBLISH_TO):
        rel = os.path.relpath(root, PUBLISH_TO)
        for f in files:
            key = os.path.normpath(os.path.join(rel, f) if rel != "." else f)
            if key not in built:
                os.remove(os.path.join(root, f))
                removed.append(key)
    for root, dirs, files in os.walk(PUBLISH_TO, topdown=False):
        for d in dirs:
            path = os.path.join(root, d)
            if not os.listdir(path):
                os.rmdir(path)
    if removed:
        print("      removed %d stale file(s) from the published tree" % len(removed))
    return PUBLISH_TO


def clean_tree(builds):
    _assert_owned_delivery(DELIVERY, "GDPVAL_BUILD_ROOT/delivery")
    if os.path.isdir(DELIVERY):
        shutil.rmtree(DELIVERY)
    os.makedirs(os.path.join(DELIVERY, "manifests"), exist_ok=True)
    for build in builds:
        for sub in ("reference_files/" + build.ref_bundle,
                    "deliverable_files/" + build.dlv_bundle,
                    "validation_evidence/" + build.task_id):
            os.makedirs(os.path.join(DELIVERY, sub), exist_ok=True)


def _normalise_office(path, doc_date=None):
    """Strip document properties from a delivered Office file and pin its zip dates.

    Applied to references and deliverables alike, per §2.4 — both go through the
    same strip. Relying on "the generator already stripped it" held only while
    every input came from our own generator; the accepted package ships a store
    profile still carrying `Openpyxl 3.1.5`, a creator of `openpyxl` and a real
    build clock, because that file did not come from one.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import officestrip
    return officestrip.strip(path, doc_date or BUILD_DOC_DATE)


def _make_writable_copy(path):
    """Allow normalisation to rewrite a copied read-only evidence file.

    Role packets intentionally make evidence read-only. ``copy2`` preserves
    that mode, but delivery normalisation operates on the scratch copy and
    needs owner write permission. The registered source is never modified.
    """
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if not mode & stat.S_IWUSR:
        os.chmod(path, mode | stat.S_IWUSR)


def copy_payload(builds):
    """Copy staged files in, refusing to carry OS cruft into the delivery tree."""
    JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}
    pairs = []
    for build in builds:
        for name in build.refs:
            src = os.path.join(build.staging, "reference_files", name)
            dst = os.path.join(DELIVERY, "reference_files", build.ref_bundle, name)
            shutil.copy2(src, dst)
            _make_writable_copy(dst)
            _normalise_office(dst)
            pairs.append((build.task_id, "reference",
                          "reference_files/%s/%s" % (build.ref_bundle, name), dst))
        for name in build.dlvs:
            src = os.path.join(build.staging, "deliverable_files", name)
            dst = os.path.join(DELIVERY, "deliverable_files", build.dlv_bundle, name)
            expected = build.deliverable_sources[name]["source_sha256"]
            if sha256(src) != expected:
                raise SystemExit("%s: staged deliverable %s does not match its "
                                 "registered source SHA-256" % (build.task_id, name))
            shutil.copy2(src, dst)
            if sha256(dst) != expected:
                raise SystemExit("%s: copied deliverable %s changed bytes" %
                                 (build.task_id, name))
            pairs.append((build.task_id, "deliverable",
                          "deliverable_files/%s/%s" % (build.dlv_bundle, name), dst))
    for root, dirs, files in os.walk(DELIVERY):
        dirs[:] = [d for d in dirs if d not in JUNK]
        for f in files:
            if f in JUNK:
                os.remove(os.path.join(root, f))
    return pairs


def _vocabulary_status(sector, occupation):
    """Describe the controlled-vocabulary position from the evidence, not from memory."""
    path = os.path.join(BASE, "vocab", "authoritative_english_strings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            auth = json.load(fh)
    except (OSError, ValueError):
        return ("UNVERIFIED — no authoritative list is present at "
                "vocab/authoritative_english_strings.json.")
    pairs = {(x["sector"], x["occupation"]) for x in auth["pairs"]}
    ok = (sector, occupation) in pairs
    src = auth["_source"]
    return ("VERIFIED — sector and occupation match the client's authoritative list "
            "character for character and are paired there as they are here. That "
            "list holds %d sectors and %d occupations and was taken from %s (%d "
            "rows, sha256 %s), supplied %s."
            % (auth["_counts"]["sectors"], auth["_counts"]["occupations"],
               src["file"], src["rows"], src["sha256"][:16], src["supplied_by"])
            if ok else
            "MISMATCH — '%s' / '%s' is not a pair in the client's authoritative list."
            % (sector, occupation))


def write_tasks_jsonl(builds):
    records = []
    for build in builds:
        items = build.task.rubric
        records.append({
            "task_id": build.task_id,
            "sector": build.sector,
            "occupation": build.occupation,
            "prompt": build.task.prompt,
            "reference_files": ["reference_files/%s/%s" % (build.ref_bundle, n)
                                for n in build.refs],
            "reference_file_urls": [],
            "reference_file_hf_uris": [],
            "deliverable_files": ["deliverable_files/%s/%s" % (build.dlv_bundle, n)
                                  for n in build.dlvs],
            "deliverable_file_urls": [build.deliverable_sources[n]["source_url"]
                                        for n in build.dlvs],
            "deliverable_file_hf_uris": [],
            "rubric_pretty": build.task.rubric_pretty,
            "rubric_json": json.dumps(items, ensure_ascii=False),
        })
    path = os.path.join(DELIVERY, "tasks.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return records


def _modalities(names):
    """Read off the files rather than declared beside them.

    The declared list said pdf/xlsx/docx long after the package had moved to
    Markdown. A field that describes files should be computed from the files.
    """
    return sorted({n.rsplit(".", 1)[-1].lower() for n in names if "." in n})


def write_manifests(pairs, records, builds):
    m = os.path.join(DELIVERY, "manifests")
    coverage = []
    for build, record in zip(builds, records):
        declared = dict(build.task.coverage or {})
        declared.pop("input_modalities", None)
        declared.pop("output_types", None)
        entry = {
            "task_id": build.task_id,
            "sector": build.sector,
            "occupation": build.occupation,
            "input_modalities": _modalities(build.refs),
            "output_types": _modalities(build.dlvs),
            "release_ready": False,
            "release_ready_reason": (
                "Local staging: reference_file_urls, reference_file_hf_uris, "
                "deliverable_file_urls and deliverable_file_hf_uris are empty arrays. "
                "Remote upload and download-hash verification are not yet done."),
            "controlled_vocabulary_status": _vocabulary_status(build.sector,
                                                               build.occupation),
        }
        entry.update(declared)
        entry["input_modalities"] = _modalities(build.refs)
        entry["output_types"] = _modalities(build.dlvs)
        coverage.append(entry)
    with open(os.path.join(m, "coverage_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(coverage, fh, ensure_ascii=False, indent=2)

    write_provenance(pairs, builds)
    return coverage


def _owning_task(rel, builds):
    """Which task an unlisted file belongs to. Evidence lives under the task id;
    the shared index files belong to the delivery, not to any one task."""
    parts = rel.split("/")
    if len(parts) > 1 and parts[0] == "validation_evidence":
        return parts[1]
    if len(builds) == 1:
        return builds[0].task_id
    return None


def write_provenance(pairs, builds):
    """Per-file provenance for the whole tree.

    Called once at build time and again by validate.py after the validation
    evidence exists, because §8 asks for every file and the evidence files do
    not exist yet when the tree is first assembled.

    Every field comes from the task's own provenance declaration. It used to be
    written here as literals, and those literals told the wrong story: they
    declared the reference files `synthetic` and `pipeline-generated` when the
    accepted package declares them supplier work records, de-identified and
    reconstructed. Under a gold-first flow that is not merely stale, it is the
    opposite of the statement the client's core requirement asks for.
    """
    m = os.path.join(DELIVERY, "manifests")
    by_task = {build.task_id: build for build in builds}
    listed = {p[2] for p in pairs}
    extra = []
    for root, _dirs, files in os.walk(DELIVERY):
        for f in sorted(files):
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, DELIVERY).replace(os.sep, "/")
            if rel in listed:
                continue
            role = ("index" if rel.startswith("manifests/") or rel == "tasks.jsonl"
                    else "validation_evidence")
            extra.append((_owning_task(rel, builds), role, rel, ap))

    with open(os.path.join(m, "provenance_manifest.jsonl"), "w", encoding="utf-8") as fh:
        for task_id, role, rel, abs_path in list(pairs) + extra:
            build = by_task.get(task_id) or builds[0]
            declaration = build.task.provenance or {}
            defaults = dict(declaration.get("defaults") or {})
            role_block = dict((declaration.get("roles") or {}).get(role) or {})
            prefix = declaration.get("source_record_prefix", "")
            row = {
                "task_id": task_id,
                "path": rel,
                "role": role,
                "source_record_id": prefix + os.path.basename(rel),
            }
            row.update(defaults)
            row.update(role_block)
            # Per-file override on top of the role template. One task can hold
            # a real agency record and a file we rebuilt ourselves in the same
            # role — registering both as the agency's original is the failure
            # §1.1 calls fatal, and it is what a reviewer found here: three
            # reconstructed inputs declared as official case records, and our
            # own manifests declared as U.S. Government works.
            row.update((declaration.get("files") or {}).get(os.path.basename(rel), {}))
            if role == "deliverable":
                source = build.deliverable_sources[os.path.basename(rel)]
                row["source_url"] = source["source_url"]
                row["source_sha256"] = source["source_sha256"]
            if role_block.get("revision_evidence") == "@gold_revision":
                row["revision_evidence"] = ("validation_evidence/%s/gold_revision/"
                                            % task_id)
            row["acquisition_date"] = defaults.get("acquisition_date", BUILD_DATE)
            row["content_sha256"] = (None if rel in SELF_REFERENTIAL
                                     else sha256(abs_path))
            row["content_sha256_note"] = (
                "Written after this manifest; its hash is recorded in "
                "manifests/checksums_final.txt to avoid asserting a value that "
                "would be stale by construction."
                if rel in SELF_REFERENTIAL else role_block.get("content_sha256_note"))
            row["bytes"] = (None if rel in SELF_REFERENTIAL
                            else os.path.getsize(abs_path))
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(list(pairs)) + len(extra)


def write_source_inventory(builds):
    m = os.path.join(DELIVERY, "manifests")
    with open(os.path.join(m, "source_inventory.jsonl"), "w", encoding="utf-8") as fh:
        for build in builds:
            for entry in (build.task.source_inventory or []):
                row = dict(entry)
                row.setdefault("task_id", build.task_id)
                row.setdefault("acquisition_date", BUILD_DATE)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.write(json.dumps({
            "source_id": "INVENTORY-COMPLETENESS-NOTE",
            "source_type": "note",
            "description": "This inventory covers %d task(s). Acceptance criterion 1 "
                           "requires the candidate source-material inventory for the "
                           "full delivery to list at least 5,000 items or groups. This "
                           "delivery does not meet that scale and must not be presented "
                           "as doing so." % len(builds),
            "adopted": False,
            "task_id": None,
            "rejection_reason": "pilot_scope_only",
            "license": None,
            "acquisition_date": BUILD_DATE,
        }, ensure_ascii=False) + "\n")


def write_sha256_inventory():
    """Covers every file in the delivery tree, written last so it includes manifests."""
    entries = []
    for root, dirs, files in os.walk(DELIVERY):
        dirs.sort()
        for f in sorted(files):
            if f in ("file_inventory_sha256.txt", "checksums_final.txt"):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, DELIVERY).replace(os.sep, "/")
            entries.append((sha256(p), os.path.getsize(p), rel))
    path = os.path.join(DELIVERY, "manifests", "file_inventory_sha256.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# SHA-256 inventory for delivery root. Format: <sha256>  <bytes>  "
                 "<relative POSIX path>\n")
        fh.write("# Covers every file in the delivery tree except this file and "
                 "manifests/checksums_final.txt, which are written after it. Their "
                 "hashes are in manifests/checksums_final.txt.\n")
        for h, n, rel in sorted(entries, key=lambda e: e[2]):
            fh.write("%s  %10d  %s\n" % (h, n, rel))
    return len(entries)


def write_checksums_final():
    """Break the circular dependency with one detached, honestly-scoped file.

    The inventory, the provenance manifest and the validation status each need
    to know the others' hashes, which is impossible in a single pass. This file
    is written after all three, records their true hashes, and states plainly
    that its own hash is not recorded anywhere — that is the one unavoidable
    end of the chain, and pretending otherwise is what produced three wrong
    hashes in the previous build.
    """
    m = os.path.join(DELIVERY, "manifests")
    targets = ["manifests/file_inventory_sha256.txt",
               "manifests/provenance_manifest.jsonl",
               "manifests/validation_status.jsonl"]
    path = os.path.join(m, "checksums_final.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# SHA-256 of the three manifests that reference one another, "
                 "computed after all of them were written.\n")
        fh.write("# This file's own hash is deliberately not recorded anywhere: a "
                 "checksum file cannot contain its own checksum. Verify it against "
                 "the copy held by the supplier if independent confirmation is "
                 "needed.\n")
        for rel in targets:
            f = os.path.join(DELIVERY, rel)
            fh.write("%s  %10d  %s\n" % (sha256(f), os.path.getsize(f), rel))
    return path


def main():
    ids = task_ids()
    if not ids:
        raise SystemExit("no assembled tasks under %s" % TD.TASKS_ROOT)
    builds = [TaskBuild(task_id) for task_id in ids]
    clean_tree(builds)
    pairs = copy_payload(builds)
    records = write_tasks_jsonl(builds)
    write_manifests(pairs, records, builds)
    write_source_inventory(builds)
    n_files = write_sha256_inventory()

    print("tasks              :", len(builds))
    for build, record in zip(builds, records):
        print("  %s  %s / %s" % (build.task_id, build.sector, build.occupation))
        print("    bundles differ :", build.ref_bundle != build.dlv_bundle)
        print("    prompt chars   :", len(record["prompt"]))
        print("    rubric items   :", len(json.loads(record["rubric_json"])))
    print("files hashed       :", n_files)
    print("built at           :", DELIVERY)
    print("published to       :", publish())
    return records


if __name__ == "__main__":
    main()

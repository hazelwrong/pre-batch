"""The reconstruction record: what the delivered files were made from.

Under a gold-first flow this is the honest declaration §1.1 asks for — the
source material, the business context, what was de-identified, and how far the
reconstruction went — together with the hashes of the files it describes.

It replaces a draft-versus-revised diff. That diff belonged to the flow where a
generator drafted the gold and an expert edited it, and it kept reporting edit
counts and a reviser's name taken from a filename, for two files the accepted
package had already replaced. The accepted package asserts no check here at all;
what it ships is this record.

The one thing worth checking automatically is that the record still describes
the files actually delivered, so the hashes are recomputed rather than carried.
"""
import hashlib
import json
import os

REQUIRED = ("source_material", "business_context", "deidentification", "scope")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write(task, outdir, delivery_root, reference_paths):
    record = dict(task.gold_revision or {})
    os.makedirs(outdir, exist_ok=True)
    declared = record.get("revision_record") or {}
    missing = [key for key in REQUIRED if not declared.get(key)]

    payload = {
        "task_id": task.task_id,
        "date": record.get("date"),
        "revision_record": declared,
        "current_reference_files": [
            {"file": os.path.basename(rel),
             "sha256": _sha256(os.path.join(delivery_root, rel)),
             "bytes": os.path.getsize(os.path.join(delivery_root, rel))}
            for rel in sorted(reference_paths, key=os.path.basename)],
    }
    with open(os.path.join(outdir, "gold_revision.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    if not declared:
        return payload, "not_run", ("No reconstruction record for this task. §1.1 "
                                    "requires the production method to be recorded "
                                    "as it actually was.")
    if missing:
        return payload, "failed", ("The reconstruction record omits %s."
                                   % ", ".join(missing))
    return payload, "passed", (
        "Reconstruction recorded as it was performed: source material, business "
        "context, de-identification and the scope of the rebuild, over %d "
        "reference file(s) whose hashes are recomputed here rather than carried."
        % len(payload["current_reference_files"]))

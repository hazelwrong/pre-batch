"""Deterministic packaging: identical inputs must produce an identical archive.

The delivery tree is already byte-reproducible — two consecutive builds write
the same bytes into every file. The archive around it was not: zip records each
member's modification time, and those come from the build clock, so packaging
the same tree twice produced two different SHA-256 values.

At one task that is a curiosity. At five thousand it breaks the things the
client actually relies on: de-duplication sees every re-packaged task as new,
incremental verification cannot tell a re-package from a re-edit, and "the same
inputs give the same archive" stops being checkable.

Everything here is about the container, never the contents. No file is
rewritten; only the metadata the archive records about it is normalised.
"""
import hashlib
import calendar
import os
import stat
import sys
import time
import zipfile

# Reproducible-builds convention: SOURCE_DATE_EPOCH pins every timestamp the
# build would otherwise take from the clock. Falling back to the delivery's own
# build date keeps the archive honest rather than stamping it 1980.
DEFAULT_DATE = "2026-08-14"


def source_date_epoch(default_date=DEFAULT_DATE):
    env = os.environ.get("SOURCE_DATE_EPOCH")
    if env:
        return int(env)
    y, m, d = (int(x) for x in default_date.split("-"))
    # ``mktime`` interprets the tuple in the host's local timezone.  Use UTC
    # explicitly so archives have the same timestamp on every build machine.
    return int(calendar.timegm((y, m, d, 0, 0, 0)))


def _dt(epoch):
    t = time.gmtime(epoch)
    # zip cannot represent anything before 1980.
    return (max(t.tm_year, 1980), t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec)


def normalise_tree_mtimes(root, epoch=None):
    """Give every file in the tree one timestamp, so the tree itself is stable."""
    epoch = source_date_epoch() if epoch is None else epoch
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            try:
                os.utime(p, (epoch, epoch))
                n += 1
            except OSError:
                # Some mounted filesystems refuse utime. The archive writer
                # below does not depend on this succeeding — it sets member
                # timestamps explicitly — so a failure here is not fatal.
                pass
    return n


def write_archive(tree_root, out_path, arc_prefix=None, epoch=None):
    """Zip a tree so that identical contents always yield an identical file.

    Four things are pinned: member order (sorted), member timestamps (the epoch),
    permission bits (0644 for files, 0755 for directories) and the compression
    method. Directory entries are emitted explicitly so the listing does not
    depend on how the walker happened to encounter them.
    """
    epoch = source_date_epoch() if epoch is None else epoch
    dt = _dt(epoch)
    tree_root = os.path.abspath(tree_root)
    prefix = arc_prefix if arc_prefix is not None else os.path.basename(tree_root)

    dirs, files = [], []
    for dirpath, dirnames, filenames in os.walk(tree_root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, tree_root).replace(os.sep, "/")
        if rel_dir != ".":
            dirs.append(rel_dir)
        for name in sorted(filenames):
            rel = os.path.join(rel_dir, name) if rel_dir != "." else name
            files.append(rel.replace(os.sep, "/"))

    if os.path.exists(out_path):
        os.remove(out_path)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(dirs):
            info = zipfile.ZipInfo("%s/%s/" % (prefix, rel), date_time=dt)
            info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, b"")
        for rel in sorted(files):
            with open(os.path.join(tree_root, rel), "rb") as fh:
                data = fh.read()
            info = zipfile.ZipInfo("%s/%s" % (prefix, rel), date_time=dt)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)

    with open(out_path, "rb") as fh:
        h = hashlib.sha256(fh.read()).hexdigest()
    return {"path": out_path, "sha256": h, "bytes": os.path.getsize(out_path),
            "files": len(files), "directories": len(dirs),
            "source_date_epoch": epoch,
            "note": ("Member order, timestamps, permission bits and compression "
                     "method are all pinned, so re-packaging an unchanged tree "
                     "reproduces this archive byte for byte.")}


if __name__ == "__main__":
    tree, out = sys.argv[1], sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else None
    normalise_tree_mtimes(tree)
    r = write_archive(tree, out, prefix)
    print("%s\n  sha256 %s\n  %d files, %d dirs, %d bytes, epoch %d"
          % (r["path"], r["sha256"], r["files"], r["directories"],
             r["bytes"], r["source_date_epoch"]))

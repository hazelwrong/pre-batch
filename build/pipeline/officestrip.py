"""S-STRIP — remove document properties from a delivered file and pin it.

Extracted from common.py, which cannot be imported without reportlab. Stripping
has nothing to do with PDF generation, and tying the two meant the one step that
every delivered Office file must pass could not run unless a PDF toolchain was
installed. The accepted package still carries `docProps/app.xml` naming its
generator and a real build clock in one reference file, which is what that
coupling costs.

Two things happen here and both matter:

  * `docProps/` and `customXml/` are **removed**, not blanked, along with the
    content-type overrides and relationships that declared them. An author name
    or a creation timestamp cannot leak from a part that is not in the file.
  * Every zip member is restamped to one fixed date, because openpyxl and
    python-docx both record the wall clock and an unchanged file would
    otherwise hash differently on every build.
"""
import os
import re
import shutil
import tempfile
import zipfile

DROP_PREFIXES = ("docProps/", "customXml/")
OOXML_SUFFIXES = (".docx", ".xlsx", ".pptx", ".xlsm")


def strip_and_pin(path, doc_date="2026-01-01"):
    """Strip an OOXML package in place. Returns the path."""
    date_time = tuple(int(x) for x in doc_date.split("-")) + (0, 0, 0)
    source = zipfile.ZipFile(path)
    drop = [n for n in source.namelist() if n.startswith(DROP_PREFIXES)]

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=os.path.dirname(path))
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as out:
            for item in source.infolist():
                if item.filename in drop:
                    continue
                data = source.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    text = data.decode("utf-8")
                    for prefix in DROP_PREFIXES:
                        text = re.sub(
                            r'<Override[^>]*PartName="/%s[^"]*"[^>]*/>' % prefix, "", text)
                    data = text.encode("utf-8")
                elif item.filename.endswith(".rels"):
                    text = data.decode("utf-8")
                    for prefix in DROP_PREFIXES:
                        text = re.sub(
                            r'<Relationship[^>]*Target="[./]*%s[^"]*"[^>]*/>' % prefix,
                            "", text)
                    data = text.encode("utf-8")
                info = zipfile.ZipInfo(item.filename, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = item.external_attr
                out.writestr(info, data)
    finally:
        source.close()
    shutil.move(tmp.name, path)
    return path


def residue(path):
    """What a stripped file must not contain. Used by the delivery check so the
    guard and the stripper agree on the definition — for every format either of
    them handles, not just the one that came first."""
    lowered = path.lower()
    if lowered.endswith(".pdf"):
        return pdf_residue(path)
    if not lowered.endswith(OOXML_SUFFIXES):
        return []
    with zipfile.ZipFile(path) as zf:
        return [n for n in zf.namelist() if n.startswith(DROP_PREFIXES)]


def strip_pdf(path):
    """Clear a PDF's information dictionary.

    A real document arrives carrying its authoring history: the scanner model,
    the consultancy that produced it, and a /Title that can state the answer
    outright — "…Final Decision for <facility>" on a file the Agent is being
    asked to write. pypdf keeps writing /Producer even after add_metadata({}),
    so the info object is cleared as well.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({})
    writer._info = None
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def strip(path, doc_date="2026-01-01"):
    """Strip whichever kind of delivered file this is."""
    lowered = path.lower()
    if lowered.endswith(OOXML_SUFFIXES):
        return strip_and_pin(path, doc_date)
    if lowered.endswith(".pdf"):
        return strip_pdf(path)
    return path


def pdf_residue(path):
    from pypdf import PdfReader
    return sorted((PdfReader(path).metadata or {}).keys())

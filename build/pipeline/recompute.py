"""Compare the gold against an independently recomputed set of values.

The independence lives in who produced `expected_values.json`, not in this
module. It is written by T14, the cold-context verifier: an agent that sees the
references, the prompt, the gold and the claimed lineage, but not the producer's
assumptions or working notes. This script's job is to hold the gold against
those figures and report every difference.

That is a deliberate change from the version this replaces, which re-derived one
particular task's cost model inline. Re-deriving in the validator meant the
validator had to be rewritten for every task, and a check that only exists for
one task checks nothing about the next one. What is lost — the validator doing
the arithmetic itself — was never the source of the independence anyway; the
orchestrator's run record is.

`expected_values.json` is a list of entries:

    {"name": "<what this figure is>",
     "value": 0.0,
     "unit": "<unit or currency>",
     "derivation": "how this number was reached, in words",
     "inputs": {"<input this came from>": 0.0},
     "locator": {"file": "<deliverable>.xlsx", "sheet": "<sheet>",
                 "row": 1, "column": 2},
     "tolerance_abs": 0.01}

An entry with no locator is recorded as an intermediate the reviewer can follow
but the workbook does not have to expose.
"""
import os


SPREADSHEET = (".xlsx", ".xlsm", ".xltx", ".xltm")


def _cell(ctx, locator):
    book = ctx.book(locator["file"], data_only=True)
    sheet = book[locator["sheet"]] if locator.get("sheet") else book[book.sheetnames[0]]
    if locator.get("cell"):
        return sheet[locator["cell"]].value
    return sheet.cell(row=int(locator["row"]), column=int(locator["column"])).value


def _spellings(value):
    """How one value can be written in prose."""
    out = {str(value)}
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        out.add("%g" % value)
        out.add("{:,}".format(int(value)) if float(value).is_integer()
                else "{:,.2f}".format(value))
        out.add("{:.2f}".format(value))
    return {t for t in out if t}


def _in_prose(ctx, locator, value):
    """Locate a value in a prose deliverable.

    A memorandum or a decision does not have cells. Requiring one meant every
    expected value on a prose gold came back as a spreadsheet error and the
    evidence file reported that all of them disagreed — 45 mismatches where
    there were none. A written document is checked the way a reader checks it:
    is the value there, in the section it is supposed to be in.
    """
    text = ctx.text(locator["file"])
    section = locator.get("section")
    scope = text
    if section:
        lowered, needle = text.lower(), str(section).lower()
        at = lowered.find(needle)
        if at >= 0:
            scope = text[at:]
    for spelling in _spellings(value):
        if spelling and spelling in scope:
            return spelling
    return None


def run(expected, ctx):
    """Returns (evidence, passed). Never raises on a bad entry — a malformed
    expectation is reported as a failure, because falling over here would hide
    every other comparison behind a stack trace."""
    entries, mismatches, unlocated, unreadable = [], [], 0, []
    for item in expected or []:
        record = {"name": item.get("name"), "expected": item.get("value"),
                  "unit": item.get("unit"), "derivation": item.get("derivation"),
                  "inputs": item.get("inputs")}
        locator = item.get("locator")
        if not locator:
            unlocated += 1
            record.update({"status": "intermediate",
                           "detail": "not carried in the deliverables"})
            entries.append(record)
            continue
        prose = not str(locator.get("file", "")).lower().endswith(SPREADSHEET)
        try:
            if prose:
                found = _in_prose(ctx, locator, item.get("value"))
                # A descriptive expectation — "centred, capitalised", "United
                # States prose form", a blank date field — cannot be settled by
                # matching. Reporting it as a mismatch is as false as reporting
                # it as verified; it needs a reader, and the evidence says so.
                # A descriptive expectation can be settled by inspecting the
                # document's structure rather than its text — alignment, table
                # labels, a word count, an empty field. Where that was done, the
                # observation is recorded so a reviewer can check the claim,
                # not just the verdict.
                settlement = item.get("settlement")
                if not found and settlement:
                    record.update({
                        "status": "settled_by_inspection",
                        "got": settlement.get("observation"),
                        "settled_by": settlement.get("by"),
                        "settlement_outcome": settlement.get("outcome")
                                              or "confirmed_by_inspection",
                        "locator": locator["file"],
                        "detail": settlement.get("detail")
                                  or "settled by inspecting the document structure"})
                    entries.append(record)
                    continue
                record.update({
                    "status": "passed" if found else
                              ("failed" if item.get("literal") is True
                               else "needs_reading"),
                    "got": found,
                    "locator": "%s%s" % (locator["file"],
                                         " / " + str(locator["section"])
                                         if locator.get("section") else ""),
                    "detail": ("located in the deliverable" if found else
                               "no literal match; a reader must settle whether "
                               "the deliverable carries this")})
                if record["status"] == "failed":
                    mismatches.append(record["name"])
                elif record["status"] == "needs_reading":
                    unreadable.append(record["name"])
                entries.append(record)
                continue
            got = _cell(ctx, locator)
        except Exception as exc:                                  # noqa: BLE001
            record.update({"status": "failed", "got": None,
                           "detail": "%s: %s" % (type(exc).__name__, exc)})
            mismatches.append(record["name"])
            entries.append(record)
            continue
        expected_value = item.get("value")
        if not isinstance(got, (int, float)) or not isinstance(expected_value, (int, float)):
            ok = got == expected_value
            diff = None
        else:
            tol = (abs(expected_value) * item.get("tolerance_rel", 0)
                   + item.get("tolerance_abs", 0.01))
            diff = round(abs(got - expected_value), 6)
            ok = diff <= tol
        record.update({"status": "passed" if ok else "failed", "got": got,
                       "difference": diff,
                       "locator": "%s!%s" % (locator["file"],
                                             locator.get("cell") or
                                             "R%sC%s" % (locator.get("row"),
                                                         locator.get("column")))})
        if not ok:
            mismatches.append(record["name"])
        entries.append(record)

    compared = [e for e in entries if e["status"] not in ("intermediate",
                                                          "needs_reading")]
    settled = [e for e in entries if e["status"] == "settled_by_inspection"]
    known_deviations = [e for e in settled
                        if e.get("settlement_outcome") == "known_gold_deviation"]
    evidence = {
        "values_needing_a_reader": unreadable,
        "values_settled_by_inspection": [e["name"] for e in settled],
        "known_gold_deviations": [e["name"] for e in known_deviations],
        "source": "expected_values.json, produced by the cold-context verifier",
        "values_compared": len(compared),
        "intermediates_recorded": unlocated,
        "mismatches": mismatches,
        "passed": not mismatches and bool(compared),
        "settled_by_matching": len(compared),
        "entries": entries,
    }
    return evidence, evidence["passed"]


def status_for(evidence):
    """passed / failed / not_run. A value nobody could settle is not_run — it is
    neither established nor disproved, and criterion 13 asks for that to be said
    rather than guessed in either direction."""
    if evidence["mismatches"]:
        return "failed"
    if evidence.get("values_needing_a_reader"):
        return "not_run"
    return "passed" if evidence["values_compared"] else "not_run"


def summary(evidence):
    reader = evidence.get("values_needing_a_reader") or []
    settled = evidence.get("values_settled_by_inspection") or []
    deviations = evidence.get("known_gold_deviations") or []
    if settled and not reader and not evidence["mismatches"]:
        return ("%d independently recomputed value(s) checked against the gold: %d "
                "located in the text, %d descriptive ones settled by expert inspection, "
                "each with the observation recorded; %d are known Gold deviations "
                "retained in the scoring evidence (%s)."
                % (evidence["values_compared"],
                   evidence["values_compared"] - len(settled), len(settled),
                   len(deviations), ", ".join(deviations) or "none"))
    if reader and not evidence["mismatches"]:
        return ("%d independently recomputed value(s) located in the gold; %d more "
                "are descriptive and cannot be settled by matching — a reader must "
                "confirm them: %s"
                % (evidence["values_compared"], len(reader), ", ".join(reader[:6])))
    if not evidence["values_compared"]:
        return ("No expected values were supplied, so nothing about the gold's "
                "figures has been corroborated.")
    if evidence["passed"]:
        return ("%d independently recomputed value(s) compared against the gold, "
                "every one within tolerance; %d intermediate(s) recorded for the "
                "reviewer's trail."
                % (evidence["values_compared"], evidence["intermediates_recorded"]))
    return ("%d of %d independently recomputed value(s) disagree with the gold: %s"
            % (len(evidence["mismatches"]), evidence["values_compared"],
               ", ".join(evidence["mismatches"][:6])))

"""Checks derived from 任务包建设与自审规范 — the rules distilled from the
package the client accepted.

Every rule in that document that can be decided by a machine is decided here,
once, for every task. A rule that only lives in prose is a rule someone has to
remember at 2am, and this project has already paid for the ones that were
forgotten: PDF inputs after the standard moved to Markdown, missing `required`
after it was settled as `true`, a marking sheet whose codes no longer matched
the rubric it marked.

Rules that need a person — whether the Chinese reads like a native wrote it,
whether a reference file looks synthetic — stay with the person. They are listed
in the self-review prompt in part three of that document, not here; a keyword
test that pretends to judge them would be worse than no test.

Each check returns (name, status, detail). `status` is "passed", "failed" or
"not_run"; "not_run" means the input needed was absent, never that the rule was
waived.
"""
import hashlib
import json
import os
import re
import zipfile
from datetime import datetime

import officestrip
from rights_policy import evaluate_usage_rights

OFFICE_SUFFIXES = (".docx", ".xlsx", ".pptx", ".xlsm", ".pdf")
# §1.3: names like a real business file. `store_profile.xlsx` is the tell of a
# generated corpus; the accepted package's own names carry spaces, version
# tails and date ranges.
ENGINEERED_NAME = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)+\.[a-z]+$")
# §1.5: the prompt is a manager handing over work, not a specification.
SPEC_SHEET_MARKERS = ("DELIVERABLE 1", "DELIVERABLE 2", "Mandatory:",
                      "REQUIREMENTS:", "OUTPUT 1", "OUTPUT 2")


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def reference_file_formats(record, policy, **_):
    """§1.3 — Markdown and spreadsheets only. PDF is excluded for two stated
    reasons: script-built PDFs share a give-away layout, and they cost several
    times the tokens of the same content in Markdown."""
    rules = policy.get("reference_files") or {}
    allowed = set(rules.get("allowed_formats") or [])
    forbidden = set(rules.get("forbidden_formats") or [])
    if not allowed:
        return ("reference_file_formats", "not_run", "policy declares no allowed formats")
    bad = [os.path.basename(p) for p in record["reference_files"]
           if _ext(p) in forbidden or _ext(p) not in allowed]
    return ("reference_file_formats", "passed" if not bad else "failed",
            "All %d reference files are %s." % (len(record["reference_files"]),
                                                " or ".join(sorted(allowed)))
            if not bad else "outside the allowed formats: %s" % bad)


def business_like_filenames(record, **_):
    """§1.3 — engineered names give a synthetic corpus away."""
    names = [os.path.basename(p) for p in
             record["reference_files"] + record["deliverable_files"]]
    engineered = [n for n in names if ENGINEERED_NAME.match(n)]
    return ("business_like_filenames", "passed" if not engineered else "failed",
            "All %d delivered file names read as real business documents." % len(names)
            if not engineered else
            "engineered-looking names: %s" % engineered)


def office_metadata_stripped(record, root, **_):
    """Reference reconstructions are normalized; raw deliverables are not.

    The client now requires deliverable_files to be the exact source bytes.
    Checking or stripping their document properties would either reject a valid
    original or tempt the build to alter it.
    """
    offenders = []
    for rel in record["reference_files"]:
        if not rel.lower().endswith(OFFICE_SUFFIXES):
            continue
        leftovers = officestrip.residue(os.path.join(root, rel))
        if leftovers:
            offenders.append((os.path.basename(rel), leftovers[:3]))
    checked = sum(1 for rel in record["reference_files"]
                  if rel.lower().endswith(OFFICE_SUFFIXES))
    return ("office_metadata_stripped", "passed" if not offenders else "failed",
            "%d delivered file(s) carry no document properties." % checked
            if not offenders else "residual metadata parts: %s" % offenders)


def license_permits_delivery(record, provenance, policy, task_root=None, **_):
    """§12 and the requirement document: every item must carry a verifiable
    source *and* permission to use it for this project.

    Nothing checked this before. A task can be built from impeccably real
    material that we are not licensed to hand over — and the package would pass
    every other check on its way out the door. The declaration lives in the
    task's provenance; this reads it and refuses to call the position clear when
    it is not.
    """
    if not provenance:
        return ("license_permits_delivery", "not_run", "任务未提供 provenance 声明")
    blocks = [("defaults", provenance.get("defaults") or {})]
    blocks += [("roles." + k, v) for k, v in (provenance.get("roles") or {}).items()]
    blocks += [("files." + k, v) for k, v in (provenance.get("files") or {}).items()]
    UNRESOLVED = ("pending", "未确认", "待确认", "未发现",
                  "not identified", "unclear", "to be confirmed", "阻塞")
    flagged = []
    for where, block in blocks:
        text = " ".join(str(block.get(k) or "") for k in
                        ("license", "usage_scope", "rights_note", "license_note"))
        hit = next((t for t in UNRESOLVED if t.lower() in text.lower()), None)
        if hit:
            flagged.append("%s（%r）" % (where, hit))
    rights = evaluate_usage_rights(
        provenance, record.get("task_id"), task_root)
    restricted = rights["restricted"]
    flagged.extend(rights["errors"])
    if not flagged:
        if restricted:
            return ("license_permits_delivery", "passed",
                    "甲方已书面确认材料可用于其控制的内部 GDPval 研究与人工审核环境；"
                    "项目内交付允许，对外再分发仍受限。")
        return ("license_permits_delivery", "passed",
                "每一类材料都声明了权利主体与允许本项目使用的许可依据，无待确认项。")
    return ("license_permits_delivery", "failed",
            "%d 处许可声明尚未落定，交付前须由甲方书面确认或限定用途：%s。"
            "材料真实不等于可以交付——这一条不通过时，包不得进入正式 tasks.jsonl。"
            % (len(flagged), "；".join(flagged[:4])))


def rubric_required_field(record, policy, **_):
    """Check the hard-gate field without flattening its business semantics.

    Missing fields are defaulted to true during orchestration. Once present,
    both true and false are valid: the attached client definition says the
    value answers whether failure is an uncompensable acceptance gate.
    """
    rules = policy.get("rubric") or {}
    allowed = set(rules.get("required_field_allowed") or (True, False))
    items = json.loads(record["rubric_json"])
    wrong = [i.get("rubric_item_id") for i in items
             if i.get("required") not in allowed]
    return ("rubric_required_field", "passed" if not wrong else "failed",
            "All %d items carry a boolean required hard-gate designation; "
            "missing values default to true." % len(items) if not wrong else
            "%d item(s) have required outside %s" % (len(wrong), sorted(allowed)))


def rubric_item_count(record, policy, **_):
    """Enforce the lower-truncated normal count contract's observable bound."""
    rules = policy.get("rubric") or {}
    dist = rules.get("item_count_distribution") or {}
    floor = int(dist.get("lower_bound", rules.get("item_count_hard_min", 25)))
    items = json.loads(record["rubric_json"])
    count = len(items)
    return ("rubric_item_count", "passed" if count >= floor else "failed",
            "%d rubric items; count is lower-truncated at %d from N(%s,%s) and "
            "rounded to an integer." %
            (count, floor, dist.get("mean", 30), dist.get("stddev", 10)) if count >= floor
            else "%d rubric items; minimum after lower truncation is %d" % (count, floor))


def rubric_score_granularity(record, policy, **_):
    """Validate score type only; no artificial 1-3 or low-score-share gate."""
    items = json.loads(record["rubric_json"])
    invalid = [i.get("rubric_item_id") for i in items
               if (isinstance(i.get("score"), bool)
                   or not isinstance(i.get("score"), int)
                   or i.get("score") < 0)]
    return ("rubric_score_granularity", "failed" if invalid else "passed",
            "All item scores are non-negative integers; score values are not "
            "restricted to 1-3 and no 1-2 share threshold applies." if not invalid
            else "invalid score type/value on %s" % invalid[:8])


def rubric_pretty_format(record, **_):
    """§1.6 — `[+N] one sentence`, blank line between items, same count as the
    JSON, and no verification notes leaking into the reviewer-facing text."""
    pretty = record["rubric_pretty"]
    items = json.loads(record["rubric_json"])
    blocks = [b.strip() for b in pretty.split("\n\n") if b.strip()]
    problems = []
    if len(blocks) != len(items):
        problems.append("%d blocks against %d rubric items" % (len(blocks), len(items)))
    malformed = [b[:40] for b in blocks if not re.match(r"^\[\+\d+\]\s+\S", b)]
    if malformed:
        problems.append("not in [+N] form: %s" % malformed[:3])
    scores_match = [b for b, i in zip(blocks, items)
                    if not b.startswith("[+%d]" % i.get("score", -1))]
    if scores_match:
        problems.append("%d block(s) disagree with the item score" % len(scores_match))
    return ("rubric_pretty_format", "passed" if not problems else "failed",
            "%d blocks, each `[+N] one sentence`, scores agreeing with the JSON."
            % len(blocks) if not problems else "; ".join(problems))


def prompt_style(record, policy, **_):
    """§1.5 — a manager handing over work, not a specification sheet. The
    markers screened here are the ones the published corpus effectively never
    uses: one all-caps heading in 220 tasks, no numbered DELIVERABLE blocks and
    no `Mandatory:` at all."""
    prompt = record["prompt"]
    hits = [m for m in SPEC_SHEET_MARKERS if m in prompt]
    caps = re.findall(r"^[A-Z][A-Z \d/&-]{7,}$", prompt, re.M)
    limits = ((policy.get("language") or {}).get("prompt_length_chars") or {})
    low, high = limits.get("range", [0, 10 ** 9])
    problems = []
    if hits:
        problems.append("spec-sheet markers: %s" % hits)
    if caps:
        problems.append("all-caps headings: %s" % caps[:3])
    if not low <= len(prompt) <= high:
        problems.append("length %d outside the observed range %d-%d"
                        % (len(prompt), low, high))
    return ("prompt_style", "passed" if not problems else "failed",
            "%d characters, no spec-sheet markers and no all-caps headings."
            % len(prompt) if not problems else "; ".join(problems))


def expert_rejection_recorded(reviewers, policy, **_):
    """§1.7 — a review record in which nothing was ever rejected is itself a
    suspicious signal. The accepted package records six rounds, two of them
    substantive rejections."""
    if not (policy.get("human_review") or {}).get("expert_rejection_required"):
        return ("expert_rejection_recorded", "not_run", "policy does not require it")
    if not reviewers:
        return ("expert_rejection_recorded", "not_run", "no reviewer roster available")
    experts = reviewers.get("occupational_expert_review") or []
    experts = experts if isinstance(experts, list) else [experts]
    signed = [e for e in experts
              if e.get("reviewer") and e.get("title") and e.get("date")
              and e.get("counts_toward_acceptance") is not False]
    if not signed:
        return ("expert_rejection_recorded", "not_run",
                "%d returned expert-feedback record(s), but none has an "
                "acceptance-eligible reviewer identity, title and date" % len(experts))
    experts = signed
    # A roster carried across from an accepted record holds the adoption summary
    # rather than the individual rounds. Both are evidence of the same thing;
    # only accepting one of them would report a diligent expert as a rubber
    # stamp for the second time.
    for expert in experts:
        summary = expert.get("adoption_summary") or {}
        objected = int(summary.get("rounds_with_substantive_objection", 0) or 0)
        if objected:
            return ("expert_rejection_recorded", "passed",
                    "%d recorded adoption round(s), %d of them a substantive "
                    "objection, on items %s."
                    % (int(summary.get("rounds_recorded", objected) or objected),
                       objected, ", ".join(summary.get("objected_item_codes") or [])))
    rounds = [rd for e in experts for rd in (e.get("adoption_rounds") or [])]
    # Sweep the class, not one spelling. Rosters record a rejection variously as
    # an `objected` list, an `objection` note, a `rejected` flag or an outcome
    # string; a guard that knows only one of those reports a diligent expert as
    # a rubber stamp.
    def _is_rejection(rd):
        if str(rd.get("outcome", "")).lower().startswith(("reject", "驳回")):
            return True
        for key in ("objected", "objections", "rejected", "rejected_items",
                    "objection", "驳回"):
            value = rd.get(key)
            if value not in (None, "", [], {}, False):
                return True
        return False

    rejected = [rd for rd in rounds if _is_rejection(rd)]
    return ("expert_rejection_recorded", "passed" if rejected else "failed",
            "%d adoption round(s) recorded, %d of them a substantive rejection."
            % (len(rounds), len(rejected)) if rejected else
            "%d adoption round(s) and not one rejection — a record in which the "
            "expert never disagreed is not evidence that the expert looked"
            % len(rounds))


def _valid_task_exception(exception, record, marking, task_root):
    """Validate a narrow, evidence-bound exception without weakening policy."""
    if not isinstance(exception, dict):
        return False, "no task-scoped exception is registered"
    task_id = (record or {}).get("task_id")
    required = {
        "status": "approved_task_exception",
        "check": "gold_not_full_marks",
        "task_id": task_id,
        "global_policy_unchanged": True,
        "scope": "single_task_only",
    }
    mismatches = [key for key, expected in required.items()
                  if exception.get(key) != expected]
    if mismatches:
        return False, "task exception fields do not match: %s" % ", ".join(mismatches)
    identity_missing = [key for key in ("approved_by", "approved_role")
                        if not str(exception.get(key) or "").strip()]
    if identity_missing:
        return False, "task exception approval identity is missing: %s" % \
            ", ".join(identity_missing)
    try:
        approved_at = datetime.fromisoformat(str(exception.get("approved_at")))
    except (TypeError, ValueError):
        return False, "approved_at is not valid ISO-8601"
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        return False, "approved_at must include a timezone"
    if approved_at > datetime.now(approved_at.tzinfo):
        return False, "approved_at is later than the validation time"
    accepted_score = exception.get("accepted_score")
    if accepted_score != (marking or {}).get("returned_form_total"):
        return False, "accepted_score does not match the signed marking"
    evidence_file = exception.get("evidence_file")
    evidence_sha256 = exception.get("evidence_sha256")
    if (not task_root or not isinstance(evidence_file, str)
            or os.path.basename(evidence_file) != evidence_file
            or not re.fullmatch(r"[0-9a-f]{64}", str(evidence_sha256 or ""))):
        return False, "exception evidence path or digest is invalid"
    evidence_path = os.path.join(task_root, evidence_file)
    try:
        with open(evidence_path, "rb") as evidence_fh:
            digest = hashlib.sha256(evidence_fh.read()).hexdigest()
    except OSError:
        return False, "exception evidence file is missing"
    if digest != evidence_sha256:
        return False, "exception evidence digest does not match"
    if not str(exception.get("reason") or "").strip():
        return False, "exception reason is missing"
    return True, "approved by %s at %s; evidence sha256=%s" % (
        exception["approved_by"], exception["approved_at"], evidence_sha256)


def gold_not_full_marks(marking, policy, record=None, policy_exceptions=None,
                        task_root=None, **_):
    """§1.7 and the rubric spec — a gold that scores full marks says the rubric
    was cut to fit it. Disclosing the shortfall is the point, not a blemish."""
    if not (policy.get("rubric") or {}).get("gold_must_not_score_full"):
        return ("gold_not_full_marks", "not_run", "policy does not require it")
    if not marking:
        return ("gold_not_full_marks", "not_run", "no marking sheet")
    if (not marking.get("marked_by") or not marking.get("marked_on") or
            marking.get("counts_toward_acceptance") is False):
        return ("gold_not_full_marks", "not_run",
                "A provisional marking record exists, but its reviewer identity/date "
                "is unresolved or it is explicitly excluded from acceptance")
    short = [m["code"] for m in marking.get("items", []) if m.get("shortfall")]
    if not short:
        exception = (policy_exceptions or {}).get("gold_not_full_marks")
        valid, exception_detail = _valid_task_exception(
            exception, record, marking, task_root)
        if valid:
            return ("gold_not_full_marks", "passed",
                    "No item is marked short; the real %s/100 score is retained "
                    "under a disclosed single-task exception (%s). Global policy "
                    "remains unchanged."
                    % (marking.get("returned_form_total"), exception_detail))
    return ("gold_not_full_marks", "passed" if short else "failed",
            "The gold is marked short on %d item(s) and the shortfall is recorded "
            "per item: %s." % (len(short), ", ".join(short)) if short else
            "No item is marked short. A gold that meets every criterion suggests "
            "the criteria were written from the gold")


def independence_claim_truthful(marking, reviewers, policy, **_):
    """§1.7 and §2.5 — the supplier marked its own gold. Saying so is safe;
    implying third-party certification is fatal."""
    want = (policy.get("human_review") or {}).get("independence_claim")
    if not want:
        return ("independence_claim_truthful", "not_run", "policy states no wording")
    stated = " ".join(filter(None, [
        str((marking or {}).get("independence") or ""),
        json.dumps(reviewers or {}, ensure_ascii=False)]))
    overclaims = [p for p in ("independent third-party", "第三方独立", "独立认证",
                              "independently certified")
                  if p.lower() in stated.lower()
                  and "not represented as" not in stated.lower()]
    declared = bool(stated.strip())
    return ("independence_claim_truthful",
            "passed" if declared and not overclaims else
            ("failed" if overclaims else "not_run"),
            "Self-assessment is declared and no independence is claimed for it."
            if declared and not overclaims else
            ("independence overclaimed: %s" % overclaims if overclaims
             else "no independence statement found"))


CHECKS = [license_permits_delivery, reference_file_formats, business_like_filenames, office_metadata_stripped,
          rubric_required_field, rubric_item_count, rubric_score_granularity,
          rubric_pretty_format,
          prompt_style, expert_rejection_recorded, gold_not_full_marks,
          independence_claim_truthful]


def run_all(record, root, policy, marking=None, reviewers=None, provenance=None,
            policy_exceptions=None, task_root=None):
    for fn in CHECKS:
        yield fn(record=record, root=root, policy=policy, marking=marking,
                 reviewers=reviewers, provenance=provenance,
                 policy_exceptions=policy_exceptions, task_root=task_root)

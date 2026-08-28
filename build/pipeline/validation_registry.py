"""Versioned registry for the fixed GDPval validator's emitted checks.

The registry is shared by the validator and the orchestrator so neither side
can silently accept a partial list.  Task metadata controls only checks whose
implementation is genuinely conditional.
"""
import hashlib
import json


VALIDATION_REGISTRY_VERSION = "gdpval-validation-registry-v1"

BASE_VALIDATION_CHECKS = frozenset({
    "tasks_jsonl_parses", "schema_12_fields", "task_id_uuid",
    "bundle_uuid5_derivation", "path_convention", "files_exist_nonempty",
    "release_state_and_list_parity", "rubric_json_parsable",
    "rubric_total_100", "rubric_item_ids_unique_uuid",
    "rubric_schema_fields", "rubric_author_type_truthful",
    "rubric_adoption_complete", "sha256_matches_inventory",
    "payload_files_all_declared", "delivery_tree_no_stray_files",
    "manifest_present_coverage_manifest",
    "manifest_present_provenance_manifest",
    "manifest_present_source_inventory",
    "manifest_present_file_inventory_sha256", "answer_leakage_scan",
    "controlled_vocabulary_mapping_verified",
    "controlled_vocabulary_english_strings",
    "privacy_pii_scan", "copyright_scan", "malicious_content_scan",
    "path_traversal_scan", "secret_key_scan",
    "license_permits_delivery", "reference_file_formats",
    "business_like_filenames", "office_metadata_stripped",
    "rubric_required_field", "rubric_item_count",
    "rubric_score_granularity", "rubric_pretty_format", "prompt_style",
    "expert_rejection_recorded", "gold_not_full_marks",
    "independence_claim_truthful", "template_guards_applicability",
    "gold_matches_independent_recompute", "rubric_item_judgeability",
    "gold_deliverable_eval", "gold_scored_against_threshold",
    "gold_reconstruction_record", "gold_source_eligible",
    "source_to_gold_lineage", "visual_render",
    "review_narratives_not_stale", "post_adoption_corrections_confirmed",
    "human_review_general_review",
    "human_review_occupational_expert_review",
    "human_review_final_review", "provenance_covers_every_file",
    "self_referential_manifest_hashes",
})

TEMPLATE_GUARD_ROLES = (
    "policy", "issue_log", "profile", "quotations", "narrative_deliverable",
)
TEMPLATE_GUARD_CHECKS = frozenset({
    "template_guard_a_checklist_source_anchor",
    "template_guard_b_no_invented_facts",
    "template_guard_c_tests_decidable",
})


def expected_validation_checks(task_meta=None):
    """Return the exact check names the validator must emit for one task."""
    meta = task_meta if isinstance(task_meta, dict) else {}
    expected = set(BASE_VALIDATION_CHECKS)
    roles = meta.get("file_roles") or {}
    if all(roles.get(role) for role in TEMPLATE_GUARD_ROLES):
        expected.update(TEMPLATE_GUARD_CHECKS)
    if meta.get("render_expectations"):
        expected.add("visual_output_contract")
    return frozenset(expected)


def validation_registry_digest(names):
    """Bind both the registry schema and its exact sorted check-name set."""
    payload = {
        "version": VALIDATION_REGISTRY_VERSION,
        "checks": sorted(names),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

"""Shared rights and restricted-use evaluation for review and release gates."""
import hashlib
from datetime import datetime
from pathlib import Path


BOUNDARY_FIELDS = (
    "public_release", "internal_use", "third_party_redistribution", "sublicensing",
)
AUTHORIZED_VALUES = {"authorized", "allowed", "true"}
ACCEPTED_AUTHORIZATION_STATUSES = {
    "client_confirmed_internal_use",
    "rights_holder_authorized",
    "licensed_project_use",
}


def _is_authorized(value):
    return str(value or "").strip().lower() in AUTHORIZED_VALUES


def evaluate_usage_rights(provenance, task_id, task_root=None):
    """Return one normalized rights decision used by every pipeline gate."""
    provenance = provenance if isinstance(provenance, dict) else {}
    defaults = provenance.get("defaults") or {}
    authorization = (provenance.get("project_use_authorization") or
                     defaults.get("project_use_authorization") or {})
    boundaries = (authorization.get("usage_boundaries") or
                  provenance.get("usage_boundaries") or
                  defaults.get("usage_boundaries") or {})
    errors = []
    missing_boundaries = [name for name in BOUNDARY_FIELDS
                          if name not in boundaries]
    if missing_boundaries:
        errors.append("usage boundaries missing %s" %
                      ", ".join(missing_boundaries))
    if "internal_use" in boundaries and not _is_authorized(
            boundaries.get("internal_use")):
        errors.append("internal_use is not authorized")

    restricted = bool(missing_boundaries) or any(
        not _is_authorized(boundaries.get(name))
        for name in ("public_release", "third_party_redistribution", "sublicensing"))
    if restricted:
        if authorization.get("status") not in ACCEPTED_AUTHORIZATION_STATUSES:
            errors.append("restricted use needs an accepted authorization status")
        required = ("confirmed_by", "role", "confirmed_at", "task_id", "scope",
                    "evidence_file", "evidence_sha256")
        missing = [name for name in required
                   if not str(authorization.get(name) or "").strip()]
        if missing:
            errors.append("restricted-use authorization missing %s" %
                          ", ".join(missing))
        if authorization.get("task_id") != task_id:
            errors.append("restricted-use authorization is not bound to this task")
        try:
            confirmed = datetime.fromisoformat(
                str(authorization.get("confirmed_at") or "").replace("Z", "+00:00"))
            if confirmed.utcoffset() is None:
                raise ValueError
        except ValueError:
            errors.append("restricted-use authorization time is invalid or lacks timezone")

        evidence_raw = str(authorization.get("evidence_file") or "")
        evidence_rel = Path(evidence_raw)
        evidence = Path(task_root or "") / evidence_rel
        if (not evidence_raw or evidence_rel.is_absolute() or
                ".." in evidence_rel.parts or not evidence.is_file()):
            errors.append("restricted-use authorization evidence is missing or unsafe")
        else:
            expected = str(authorization.get("evidence_sha256") or "").lower()
            digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
            if len(expected) != 64 or any(char not in "0123456789abcdef"
                                           for char in expected) or digest != expected:
                errors.append("restricted-use authorization evidence SHA-256 does not match")
    return {
        "authorization": authorization,
        "boundaries": boundaries,
        "restricted": restricted,
        "errors": errors,
    }

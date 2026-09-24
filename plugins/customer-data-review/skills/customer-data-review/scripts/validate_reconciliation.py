"""Validate reconciliation output against schema and classification sources."""

from skill.schemas.constants import UNKNOWN_SEGMENT
from skill.schemas.validate import validate_json


def validate_reconciliation(
    reconciliation: dict, classification_file_ids: list[str]
) -> list[str]:
    """Validate reconciliation output.

    Checks schema validity, then cross-references assignments against
    classification files and defined segments.

    Args:
        reconciliation: Reconciliation output from the LLM.
        classification_file_ids: File IDs from the classification step.

    Returns:
        List of error strings. Empty means valid.
    """
    errors = []

    # Schema validation
    schema_errors = validate_json(reconciliation, "reconciliation")
    errors.extend(schema_errors)

    segments = reconciliation.get("segments", [])
    assignments = reconciliation.get("assignments", [])

    # "unknown" is always a valid segment assignment
    segment_ids = {
        s["id"] for s in segments if isinstance(s, dict) and "id" in s
    } | {UNKNOWN_SEGMENT}
    classification_set = set(classification_file_ids)

    # Check each assignment
    assigned_files = []
    for a in assignments:
        if not isinstance(a, dict):
            continue
        fid = a.get("file_id", "")
        seg_id = a.get("segment_id", "")
        assigned_files.append(fid)

        if fid and fid not in classification_set:
            errors.append(f"invented file in assignments: {fid}")

        if seg_id and seg_id not in segment_ids:
            errors.append(f"assignment references undefined segment: {seg_id}")

    # Duplicate assignments
    assigned_set = set()
    for fid in assigned_files:
        if fid in assigned_set:
            errors.append(f"duplicate assignment: {fid}")
        assigned_set.add(fid)

    # Dropped files (in classification but not assigned)
    for fid in sorted(classification_set - assigned_set):
        errors.append(f"file not assigned: {fid}")

    return errors

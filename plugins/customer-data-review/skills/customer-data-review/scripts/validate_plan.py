"""Validate assembled analysis plan."""

from skill.schemas.constants import UNKNOWN_SEGMENT
from skill.schemas.validate import validate_with_conditionals


def validate_plan(plan: dict) -> list[str]:
    """Validate a plan dict against schema and cross-referential rules.

    Checks:
        - Schema validity (including excluded/exclusion_reason conditional)
        - File segments reference defined segment IDs (or "unknown")
        - Unique question, file, and segment IDs

    Returns:
        List of error strings. Empty means valid.
    """
    errors = validate_with_conditionals(plan, "plan")

    segments = plan.get("segments", [])
    files = plan.get("files", [])
    questions = plan.get("questions", [])

    # Valid segment IDs — "unknown" is always allowed
    valid_segment_ids = {
        s["id"] for s in segments if isinstance(s, dict) and "id" in s
    } | {UNKNOWN_SEGMENT}

    # File segments must reference defined segments
    for i, source in enumerate(files):
        if not isinstance(source, dict):
            continue
        seg = source.get("segment", "")
        if seg and seg not in valid_segment_ids:
            errors.append(f"files.{i}.segment: '{seg}' not in defined segments")

    # Unique question IDs
    seen_q: set[str] = set()
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = q.get("id", "")
        if qid in seen_q:
            errors.append(f"duplicate question id: {qid}")
        seen_q.add(qid)

    # Unique file IDs
    seen_f: set[str] = set()
    for s in files:
        if not isinstance(s, dict):
            continue
        fid = s.get("file_id", "")
        if fid in seen_f:
            errors.append(f"duplicate file id: {fid}")
        seen_f.add(fid)

    # Unique segment IDs
    seen_seg: set[str] = set()
    for s in segments:
        if not isinstance(s, dict):
            continue
        segid = s.get("id", "")
        if segid in seen_seg:
            errors.append(f"duplicate segment id: {segid}")
        seen_seg.add(segid)

    return errors

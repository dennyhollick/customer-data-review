"""Validate classification output rows against schema and expected sources."""

import re

from skill.schemas.validate import validate_json


_MULTI_ENTITY_SOURCE_TYPES = frozenset({
    "nps_survey", "csat_survey", "general_survey", "review", "support_ticket",
})

# Filename substring → acceptable source_types.  Advisory cross-check.
_FILENAME_TYPE_HINTS: dict[str, set[str]] = {
    "win_loss": {"competitive_intel"},
    "objection": {"competitive_intel", "sales_call"},
    "nps": {"nps_survey"},
    "csat": {"csat_survey"},
    "survey": {"nps_survey", "csat_survey", "general_survey"},
    "support_ticket": {"support_ticket"},
    "feature_request": {"feature_request"},
    "review": {"review"},
    "churn": {"churn_interview"},
}

# Matches "18 survey responses", "140 calls", "9 reviews", etc.
# Minimum value of 5 avoids false positives on low narrative counts.
_COUNT_PATTERN = re.compile(
    r"\b(\d+)\s+"
    r"(responses?|reviews?|tickets?|calls?|deals?|records?|entries|surveys?)",
    re.IGNORECASE,
)
_COUNT_MINIMUM = 5


def auto_fix_classification(rows: list[dict], max_summary: int = 150) -> list[str]:
    """Auto-fix common classification issues. Returns list of warnings."""
    warnings = []
    for i, row in enumerate(rows):
        summary = row.get("summary") or ""
        if len(summary) > max_summary:
            parts = summary[: max_summary - 3].rsplit(" ", 1)
            truncated = parts[0] + "..." if len(parts) > 1 else summary[: max_summary - 3] + "..."
            row["summary"] = truncated
            warnings.append(
                f"row {i}: summary truncated from {len(summary)} to {len(row['summary'])} chars"
            )
        # Warn if a likely multi-entity source type is missing interaction_count
        source_type = row.get("source_type", "")
        if source_type in _MULTI_ENTITY_SOURCE_TYPES and "interaction_count" not in row:
            warnings.append(
                f"row {i}: source_type '{source_type}' may contain multiple interactions"
                " but interaction_count is missing — verify with source content"
            )
    return warnings


def cross_check_classification(rows: list[dict]) -> list[str]:
    """Heuristic cross-check: filename substrings vs assigned source_type.

    Returns list of advisory warnings (not blocking errors).
    """
    warnings = []
    for i, row in enumerate(rows):
        file_id = (row.get("file_id") or "").lower()
        source_type = row.get("source_type", "")
        for hint, expected_types in _FILENAME_TYPE_HINTS.items():
            if hint in file_id and source_type not in expected_types:
                warnings.append(
                    f"row {i}: file_id '{row.get('file_id')}' contains "
                    f"'{hint}' but source_type is '{source_type}' "
                    f"(expected one of: {', '.join(sorted(expected_types))})"
                )
    return warnings


def check_interaction_count_hints(rows: list[dict]) -> list[str]:
    """Warn when a classification summary mentions a count but interaction_count is absent.

    Catches the common failure where a classification agent describes
    '18 survey responses' or '140 call records' without setting interaction_count.
    """
    warnings = []
    for i, row in enumerate(rows):
        if row.get("interaction_count"):
            continue
        summary = row.get("summary") or ""
        match = _COUNT_PATTERN.search(summary)
        if match and int(match.group(1)) >= _COUNT_MINIMUM:
            warnings.append(
                f"row {i}: summary mentions '{match.group()}' but "
                f"interaction_count is not set"
            )
    return warnings


def validate_classification(rows: list[dict], expected_file_ids: list[str]) -> list[str]:
    """Validate classification rows against schema and expected file IDs.

    Args:
        rows: Classification output rows from the LLM.
        expected_file_ids: File IDs the caller expects (derived from filenames).

    Returns:
        List of error strings. Empty means valid.
    """
    errors = []

    # Schema-validate each row
    for i, row in enumerate(rows):
        row_errors = validate_json(row, "classification")
        for e in row_errors:
            errors.append(f"row {i}: {e}")

    # Set comparison: returned vs expected
    returned_ids = [row.get("file_id", "") for row in rows]
    returned_set = set()

    # Check for empty/missing file_ids
    for i, fid in enumerate(returned_ids):
        if not fid:
            errors.append(f"row {i} missing file_id")

    # Check duplicates
    for fid in returned_ids:
        if not fid:
            continue
        if fid in returned_set:
            errors.append(f"duplicate file_id: {fid}")
        returned_set.add(fid)

    expected_set = set(expected_file_ids)

    # Missing files (expected but not returned)
    for fid in sorted(expected_set - returned_set):
        errors.append(f"missing file: {fid}")

    # Invented files (returned but not expected)
    for fid in sorted(returned_set - expected_set):
        errors.append(f"invented file: {fid}")

    return errors

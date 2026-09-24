"""Phase 3b — Per-agent assignment validator for a single question batch."""

import argparse
import json
import sys

from skill.schemas.validate import validate_json


def validate_assignments(
    assignment_obj: dict,
    expected_mention_ids: set[str],
    valid_theme_ids: set[str],
) -> list[str]:
    """Validate a single agent's assignment output for one question batch.

    Args:
        assignment_obj: Parsed assignments JSON (has question_id + assignments list).
        expected_mention_ids: All mention_ids that must appear.
        valid_theme_ids: All theme_ids that exist for this question.

    Returns:
        List of error strings. Empty list means valid.
    """
    errors = validate_json(assignment_obj, "assignments")

    assignments = assignment_obj.get("assignments", [])

    # Collect assigned mention_ids
    seen_mention_ids: set[str] = set()
    for i, a in enumerate(assignments):
        rid = a.get("mention_id", "")
        tid = a.get("theme_id", "")

        # Duplicate assignment
        if rid in seen_mention_ids:
            errors.append(f"assignments.{i}: duplicate mention_id '{rid}'")
        seen_mention_ids.add(rid)

        # Invented theme
        if tid and tid not in valid_theme_ids:
            errors.append(f"assignments.{i}: theme_id '{tid}' not in valid themes")

        # Invented mention
        if rid and rid not in expected_mention_ids:
            errors.append(f"assignments.{i}: mention_id '{rid}' not in expected batch")

    # Dropped mentions
    missing = expected_mention_ids - seen_mention_ids
    for rid in sorted(missing):
        errors.append(f"missing mention_id '{rid}' — not assigned")

    return errors


def check_distribution(
    assignment_obj: dict,
    theme_types: dict[str, str] | None = None,
) -> list[str]:
    """Check assignment distribution for anomalies.

    Returns advisory WARNING strings for interpretation. Distribution alone
    does not establish an assignment defect or authorize another theme pass.
    Missing, duplicate, and invalid assignments remain hard validation errors.

    Args:
        assignment_obj: Parsed assignments JSON.
        theme_types: Optional mapping of theme_id -> type (e.g., "other", "complaint").

    Returns:
        List of severity-prefixed strings. Empty means no concerns.
    """
    warnings = []
    assignments = assignment_obj.get("assignments", [])
    if not assignments:
        return warnings

    theme_counts: dict[str, int] = {}
    for a in assignments:
        tid = a.get("theme_id", "")
        theme_counts[tid] = theme_counts.get(tid, 0) + 1

    total = len(assignments)
    for tid, count in theme_counts.items():
        pct = count / total * 100
        is_other = (theme_types or {}).get(tid) == "other"

        if is_other and pct > 25:
            warnings.append(
                f"WARNING: theme '{tid}' (type=other) has {pct:.0f}% of mentions "
                f"({count}/{total}) - disclose limited theme coverage; "
                f"retain heterogeneous evidence without restarting theme identification"
            )
        elif not is_other and pct > 40:
            warnings.append(
                f"WARNING: theme '{tid}' has {pct:.0f}% of mentions ({count}/{total}) "
                f"- may be too broad; disclose concentration without starting another pass"
            )

    return warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate assignment batch")
    parser.add_argument("assignments_json", help="Path to assignments JSON file")
    parser.add_argument("--mention-ids-json", required=True, help="JSON list of expected mention IDs")
    parser.add_argument("--theme-ids-json", required=True, help="JSON list of valid theme IDs")
    args = parser.parse_args()

    with open(args.assignments_json, encoding="utf-8") as f:
        data = json.load(f)
    with open(args.mention_ids_json, encoding="utf-8") as f:
        expected = set(json.load(f))
    with open(args.theme_ids_json, encoding="utf-8") as f:
        valid_themes = set(json.load(f))

    errors = validate_assignments(data, expected, valid_themes)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("OK")


if __name__ == "__main__":
    main()

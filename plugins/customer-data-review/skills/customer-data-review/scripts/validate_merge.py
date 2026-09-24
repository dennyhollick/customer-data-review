"""Phase 3b — Cross-batch merge validator for combined assignments."""

import argparse
import json
import sys


def validate_merge(
    merged_assignments: list[dict],
    expected_mention_ids: set[str],
    valid_theme_ids: set[str],
) -> list[str]:
    """Validate merged assignments across all batches for a question.

    Args:
        merged_assignments: List of {"mention_id": ..., "theme_id": ...} dicts.
        expected_mention_ids: All mention_ids that should appear exactly once.
        valid_theme_ids: All valid theme_ids for this question.

    Returns:
        List of error strings. Empty list means valid.
    """
    errors: list[str] = []

    seen: set[str] = set()
    for i, a in enumerate(merged_assignments):
        rid = a.get("mention_id", "")
        tid = a.get("theme_id", "")

        # Empty/missing IDs
        if not rid:
            errors.append(f"merged.{i}: empty or missing mention_id")
        if not tid:
            errors.append(f"merged.{i}: empty or missing theme_id")

        # Cross-batch duplicate
        if rid in seen:
            errors.append(f"merged.{i}: duplicate mention_id '{rid}'")
        seen.add(rid)

        # Invalid theme
        if tid and tid not in valid_theme_ids:
            errors.append(f"merged.{i}: theme_id '{tid}' not in valid themes")

        # Extra mention (not in expected set)
        if rid and rid not in expected_mention_ids:
            errors.append(f"merged.{i}: mention_id '{rid}' not in expected set")

    # Missing mentions
    missing = expected_mention_ids - seen
    for rid in sorted(missing):
        errors.append(f"missing mention_id '{rid}' — not in merged assignments")

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate merged assignments")
    parser.add_argument("merged_json", help="JSON file with merged assignments list")
    parser.add_argument("--mention-ids-json", required=True, help="JSON list of expected mention IDs")
    parser.add_argument("--theme-ids-json", required=True, help="JSON list of valid theme IDs")
    args = parser.parse_args()

    with open(args.merged_json, encoding="utf-8") as f:
        merged = json.load(f)
    if isinstance(merged, dict):
        if "assignments" not in merged:
            print("ERROR: JSON is a dict but has no 'assignments' key", file=sys.stderr)
            sys.exit(1)
        merged = merged["assignments"]
    with open(args.mention_ids_json, encoding="utf-8") as f:
        expected = set(json.load(f))
    with open(args.theme_ids_json, encoding="utf-8") as f:
        valid_themes = set(json.load(f))

    errors = validate_merge(merged, expected, valid_themes)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("OK")


if __name__ == "__main__":
    main()

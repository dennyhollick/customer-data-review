"""Phase 3a — Theme identification validator (business rules beyond schema).

Also provides normalize_themes() for auto-correcting common LLM output
errors (array→string, malformed theme_ids) before validation.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from skill.schemas.constants import THEME_TYPES, THEME_ID_RE
from skill.schemas.validate import validate_json


def normalize_themes(themes_obj: dict) -> tuple[dict, list[str]]:
    """Auto-correct common LLM output errors in theme data.

    Fixes applied:
    - include/exclude fields: arrays joined to comma-separated strings
    - theme_id: non-matching IDs (e.g. Q1_OTHER) renumbered to Q1_T{n}

    Args:
        themes_obj: Parsed themes_{qid}.json content (may be malformed).

    Returns:
        (normalized_copy, fixes_applied) where fixes_applied is a list
        of human-readable strings describing each fix.
    """
    result = json.loads(json.dumps(themes_obj))  # deep copy
    fixes = []

    qid = result.get("question_id", "")
    themes = result.get("themes", [])

    # Pass 1: fix include/exclude arrays → strings
    for i, theme in enumerate(themes):
        for field in ("include", "exclude"):
            value = theme.get(field)
            if isinstance(value, list):
                joined = ", ".join(str(v) for v in value if str(v).strip())
                theme[field] = joined if joined else "None specified"
                fixes.append(
                    f"themes.{i}: converted {field} from array to string"
                )

    # Pass 2: fix malformed theme_ids
    # Collect existing valid T-numbers to avoid collisions
    existing_nums: set[int] = set()
    for theme in themes:
        tid = theme.get("theme_id", "")
        m = re.match(r"^Q\d+_T(\d+)$", tid)
        if m:
            existing_nums.add(int(m.group(1)))

    next_num = max(existing_nums, default=0) + 1
    for i, theme in enumerate(themes):
        tid = theme.get("theme_id", "")
        if tid and not THEME_ID_RE.match(tid):
            new_tid = f"{qid}_T{next_num}"
            fixes.append(
                f"themes.{i}: renamed theme_id '{tid}' → '{new_tid}'"
            )
            theme["theme_id"] = new_tid
            next_num += 1

    return result, fixes


_NUMBER_PATTERNS = [
    re.compile(r"\d+\s*%"),  # "25% of users", "50 %"
    re.compile(
        r"\(\d+[\s+\-].*?(?:votes?|mentions?)\)", re.IGNORECASE
    ),  # "(287+ votes)", "(234-118 votes)", "(45 mentions)"
]


def check_number_in_descriptions(themes_obj: dict) -> list[str]:
    """Reject themes where description or label embeds data numbers."""
    errors = []
    for i, theme in enumerate(themes_obj.get("themes", [])):
        for field in ("description", "label"):
            value = theme.get(field, "")
            for pattern in _NUMBER_PATTERNS:
                if pattern.search(value):
                    errors.append(
                        f"themes.{i}: {field} contains embedded data "
                        f"(matches {pattern.pattern})"
                    )
                    break  # one error per field is enough
    return errors


def validate_themes(themes_obj: dict, question_id: str | None = None) -> list[str]:
    """Validate a themes object beyond schema — checks business rules.

    Args:
        themes_obj: Parsed themes_{qid}.json content.
        question_id: If provided, verify theme_ids belong to this question.

    Returns:
        List of error strings. Empty list means valid.
    """
    errors = validate_json(themes_obj, "themes")

    qid = question_id or themes_obj.get("question_id")
    themes = themes_obj.get("themes", [])

    # Check each theme
    seen_ids: set[str] = set()
    for i, theme in enumerate(themes):
        tid = theme.get("theme_id", "")

        # Duplicate theme_id
        if tid in seen_ids:
            errors.append(f"themes.{i}: duplicate theme_id '{tid}'")
        seen_ids.add(tid)

        # Theme type belt-and-suspenders (schema covers this, but be explicit)
        ttype = theme.get("type", "")
        if ttype and ttype not in THEME_TYPES:
            errors.append(f"themes.{i}: invalid theme type '{ttype}'")

        # theme_id must belong to the question
        if qid and tid and THEME_ID_RE.match(tid):
            theme_qid = tid.split("_T")[0]
            if theme_qid != qid:
                errors.append(
                    f"themes.{i}: theme_id '{tid}' does not belong to question '{qid}'"
                )

        # Required fields check (belt-and-suspenders with schema)
        for field in ("label", "description", "type", "include", "exclude", "example_mention"):
            if field not in theme:
                errors.append(f"themes.{i}: missing required field '{field}'")

    errors.extend(check_number_in_descriptions(themes_obj))

    return errors


def check_theme_budget(themes_obj: dict, mention_count: int) -> list[str]:
    """Check whether the theme count is reasonable for the data density.

    Args:
        themes_obj: Parsed themes_{qid}.json content.
        mention_count: Total mention count for this question.

    Returns:
        List of warning strings. Empty means no concerns.
    """
    warnings = []
    themes = themes_obj.get("themes", [])
    if not themes or mention_count <= 0:
        return warnings

    # Too many themes for the data — each theme would average <4 mentions
    if len(themes) > mention_count / 4:
        warnings.append(
            f"{len(themes)} themes for {mention_count} mentions is likely too many "
            f"— aim for 1 theme per 5-8 mentions to avoid flooding the Other bucket"
        )

    # Multiple Other themes
    other_count = sum(1 for t in themes if t.get("type") == "other")
    if other_count > 1:
        warnings.append(
            f"{other_count} themes with type='other' — there should be exactly 1 catch-all"
        )

    return warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate themes file")
    parser.add_argument("themes_json", help="Path to themes_{qid}.json")
    parser.add_argument("--question-id", help="Expected question ID")
    args = parser.parse_args()

    with open(args.themes_json, encoding="utf-8") as f:
        data = json.load(f)

    errors = validate_themes(data, question_id=args.question_id)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("OK")


if __name__ == "__main__":
    main()

"""Validate a single mentions.jsonl line against schema and business rules."""

import argparse
import json
import re
import sys
from collections import defaultdict

from skill.schemas.constants import SENTIMENTS
from skill.schemas.validate import validate_json
from skill.scripts._quote_quality import has_markdown_formatting, is_low_signal_quote, looks_like_csv_row


# Per-question mention caps
_SINGLE_INTERACTION_WARN = 6
_SINGLE_INTERACTION_ERROR = 12
_MULTI_INTERACTION_WARN = 25
_MULTI_INTERACTION_ERROR = 35

# Transcript marker pattern (timestamps reliably indicate transcript dumps;
# same pattern used in compute_findings._disqualifying_penalty)
_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}")


def _normalize_quote(text: str) -> str:
    """Collapse whitespace and lowercase for duplicate comparison."""
    return " ".join(text.lower().split())


def validate_mentions(mention_obj: dict, plan: dict, expected_file_id: str | None = None) -> tuple[list[str], list[str]]:
    """Validate a single mentions.jsonl object against schema + business rules.

    Args:
        mention_obj: Parsed JSON object (one line of mentions.jsonl).
        plan: Parsed plan.json dict.
        expected_file_id: If provided, verify file_id matches this value.

    Returns:
        Tuple of (errors, warnings). Errors are hard failures; warnings are
        informational (e.g., empty mentions array). Both are list[str].
    """
    errors = []

    # 1. Schema validation
    schema_errors = validate_json(mention_obj, "mentions")
    errors.extend(schema_errors)

    # If schema validation fails badly (missing required fields), some checks
    # below will still run where possible but may skip gracefully.
    file_id = mention_obj.get("file_id", "")
    mentions = mention_obj.get("mentions", [])

    # 2. File ID matches expected
    if expected_file_id is not None and file_id != expected_file_id:
        errors.append(
            f"file_id mismatch: expected '{expected_file_id}', got '{file_id}'"
        )

    # 3. Build valid question IDs from plan
    valid_question_ids = {q["id"] for q in plan.get("questions", [])}

    # Zero-mention flag (warning, not hard error)
    warnings = []
    if not mentions:
        warnings.append("mentions array is empty — source may have no relevant content")

    # 4. Track mention_ids and mention texts for uniqueness checks
    seen_mention_ids = set()
    seen_mention_texts = set()
    quote_usage_counts: dict[str, int] = defaultdict(int)

    for i, mention in enumerate(mentions):
        rid = mention.get("mention_id", "")
        qid = mention.get("question_id", "")

        # mention_id format: {file_id}_{question_id}_{index}
        if rid and file_id:
            expected_prefix = f"{file_id}_{qid}_"
            if not rid.startswith(expected_prefix):
                errors.append(
                    f"mentions[{i}].mention_id '{rid}' does not match "
                    f"expected pattern '{file_id}_{{question_id}}_{{index}}'"
                )

        # question_id exists in plan
        if qid and qid not in valid_question_ids:
            errors.append(
                f"mentions[{i}].question_id '{qid}' not found in plan questions"
            )

        # sentiment is valid enum (schema catches this too, but belt-and-suspenders)
        sentiment = mention.get("sentiment", "")
        if sentiment and sentiment not in SENTIMENTS:
            errors.append(
                f"mentions[{i}].sentiment '{sentiment}' is not a valid sentiment"
            )

        # quote must not equal mention text
        quote = mention.get("quote", "")
        mention_text = mention.get("mention", "")
        if quote and mention_text and quote == mention_text:
            errors.append(
                f"mentions[{i}].quote is identical to mention text"
            )

        # mention_id uniqueness within source
        if rid:
            if rid in seen_mention_ids:
                errors.append(
                    f"mentions[{i}].mention_id '{rid}' is a duplicate"
                )
            seen_mention_ids.add(rid)

        # duplicate mention text check — identical analytical summary (whitespace-normalized)
        if mention_text:
            normalized_mention = _normalize_quote(mention_text)
            if normalized_mention in seen_mention_texts:
                errors.append(
                    f"mentions[{i}].mention text is a duplicate of another mention's text"
                )
            seen_mention_texts.add(normalized_mention)

        # quote reuse cap — warn if more than 5 mentions share the same quote
        if quote:
            normalized_quote = _normalize_quote(quote)
            quote_usage_counts[normalized_quote] += 1
            if quote_usage_counts[normalized_quote] > 5:
                warnings.append(
                    f"mentions[{i}].quote has been reused by {quote_usage_counts[normalized_quote]} mentions (cap is 5)"
                )

        # CSV-pattern quote warning
        if quote and looks_like_csv_row(quote):
            warnings.append(
                f"mentions[{i}].quote looks like a CSV row — may not be customer voice"
            )

        # Markdown formatting warning
        if quote and has_markdown_formatting(quote):
            warnings.append(
                f"mentions[{i}].quote contains markdown formatting — may be an extraction artifact"
            )

        # Low-signal quote warning (self-introductions, meta-commentary)
        if quote and is_low_signal_quote(quote):
            warnings.append(
                f"mentions[{i}].quote appears to be a self-introduction or meta-commentary — "
                f"may not evidence the analytical claim in the mention"
            )

        # Empty attribution warning
        attribution = mention.get("quote_attribution", "")
        if not attribution or not attribution.strip():
            warnings.append(
                f"mentions[{i}].quote_attribution is empty — every quote should have a source the reader can evaluate"
            )

        # Quote length checks (A3)
        if quote:
            word_count = len(quote.split())
            if word_count > 100:
                errors.append(
                    f"mentions[{i}].quote is {word_count} words — transcript dump; Rule 18 requires 6-100 words"
                )
            elif word_count < 6:
                warnings.append(
                    f"mentions[{i}].quote is only {word_count} words — Rule 18 requires 6+ words"
                )

            # Transcript marker check
            if _TIMESTAMP_RE.search(quote):
                warnings.append(
                    f"mentions[{i}].quote contains transcript markers — extract the customer's words only, not the full transcript"
                )

            # Notable flag validation
            notable = mention.get("notable_quote", False)
            if notable and word_count < 8:
                warnings.append(
                    f"mentions[{i}].notable_quote is true but quote is only {word_count} words — notable quotes require specificity, intensity, or decision language per Rule 9"
                )

    # Per-question mention cap (A2)
    mentions_by_qid: dict[str, int] = defaultdict(int)
    for mention in mentions:
        qid = mention.get("question_id", "")
        if qid:
            mentions_by_qid[qid] += 1

    # Look up interaction_count for this source
    interaction_count = 1
    for src in plan.get("files", []):
        if src.get("file_id") == file_id:
            interaction_count = src.get("interaction_count", 1)
            break

    is_multi = interaction_count > 1
    warn_cap = _MULTI_INTERACTION_WARN if is_multi else _SINGLE_INTERACTION_WARN
    error_cap = _MULTI_INTERACTION_ERROR if is_multi else _SINGLE_INTERACTION_ERROR

    for qid, count in sorted(mentions_by_qid.items()):
        if count > error_cap:
            errors.append(
                f"Question {qid}: {count} mentions from this source exceeds cap of {error_cap}. "
                f"Rule 2 targets 2-4 mentions per question for organic narrative. "
                f"Reduce to high-signal mentions only — keep specific claims, comparisons, "
                f"and described experiences; drop conversational acknowledgments and small talk."
            )
        elif count > warn_cap:
            warnings.append(
                f"Question {qid}: {count} mentions from this source exceeds advisory threshold of {warn_cap}"
            )

    return errors, warnings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Validate a mentions.jsonl line."
    )
    parser.add_argument("mentions_json", help="Path to a JSON file with one mentions object")
    parser.add_argument("plan", help="Path to plan.json")
    parser.add_argument(
        "--expected-file-id",
        help="Expected file_id to verify against",
        default=None,
    )
    args = parser.parse_args(argv)

    with open(args.mentions_json, encoding="utf-8") as f:
        mention_obj = json.load(f)
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    errors, warnings = validate_mentions(mention_obj, plan, args.expected_file_id)

    for warn in warnings:
        print(f"WARNING: {warn}", file=sys.stderr)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    else:
        print("Valid.")


if __name__ == "__main__":
    main()

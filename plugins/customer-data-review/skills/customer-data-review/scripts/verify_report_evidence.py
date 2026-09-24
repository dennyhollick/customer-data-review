"""Check report quotations against retained, source-bound evidence.

This is lexical grounding, not a semantic judgment of the surrounding prose.
Quotation spans have no length cap. Apostrophes within words are not delimiters.
"""

import argparse
import json
import re
from pathlib import Path

from skill.scripts.bounded_audit import read_json, read_records, verify_gate
from skill.scripts.verify_attributions import _normalize_attribution, extract_quote_attribution_pairs
from skill.scripts.verify_quotes import normalize_text


def quoted_spans(text):
    """Return outer quotation spans and unclosed-quotation offsets.

    Supports straight/smart double and single quotation marks. While inside a
    double quotation, single quotes are ordinary content. This avoids treating
    possessives and contractions as nested quotations. A trailing inch/foot
    mark after a numeral outside a quotation is not a new opening delimiter.
    """
    pairs = {'"': '"', "“": "”", "'": "'", "‘": "’", "«": "»", "‹": "›"}
    spans = []
    unmatched = []
    opening = None
    start = None
    for pos, char in enumerate(text):
        before = text[pos - 1] if pos else ""
        after = text[pos + 1] if pos + 1 < len(text) else ""
        if opening is not None:
            if char != pairs[opening]:
                continue
            if char in {"'", "’"} and before.isalnum() and after.isalnum():
                continue  # customer's / we're
            value = text[start + 1:pos]
            if value.strip():
                spans.append({"quote": value, "start": start, "end": pos + 1})
            opening = None
            start = None
            continue

        if char in {"”", "»", "›"}:
            unmatched.append(pos)
            continue
        if char not in pairs:
            continue
        if char in {"'", "‘"} and (before.isalnum() or not after or after.isspace()):
            continue
        if char in {'"', "'"} and before.isdigit():
            continue
        opening, start = char, pos
    if opening is not None:
        unmatched.append(start)
    return spans, unmatched


def evidence_index(records):
    """Flatten retained records without losing source/question ownership."""
    entries = []
    seen = set()
    for record in records:
        for mention in record["mentions"]:
            mid = mention["mention_id"]
            if mid in seen:
                raise ValueError(f"Duplicate retained mention: {mid}")
            seen.add(mid)
            entries.append({**mention, "file_id": record["file_id"],
                            "normalized_quote": normalize_text(mention["quote"])})
    return entries


def matching_evidence(quote, entries, question_id=None):
    """Accept a contiguous excerpt, never an invented extension or paraphrase."""
    normalized = normalize_text(quote).rstrip(".,;:!?").strip()
    if not normalized:
        return []
    matches = []
    pattern = re.compile(
        (r"(?<!\w)" if normalized[0].isalnum() else "")
        + re.escape(normalized)
        + (r"(?!\w)" if normalized[-1].isalnum() else "")
    )
    for entry in entries:
        if question_id is not None and entry["question_id"] != question_id:
            continue
        if pattern.search(entry["normalized_quote"]):
            matches.append(entry)
    return matches


def _narrative_fields(report):
    """Yield analytical prose, excluding research-question/metadata labels."""
    groups = [("executive_summary", report.get("executive_summary", {}), None)]
    groups += [(f"questions[{i}]", question, question.get("question_id"))
               for i, question in enumerate(report.get("questions", []))]
    for path, group, qid in groups:
        for field in ("headline", "body", "synthesis"):
            if field in group:
                yield f"{path}.{field}", group[field], qid
        for field in ("key_findings", "key_questions", "takeaways"):
            for i, text in enumerate(group.get(field, [])):
                item_path = f"{path}.{field}[{i}]"
                if field == "key_findings" and isinstance(text, dict):
                    yield f"{item_path}.text", text.get("text"), qid
                    for j, annotation in enumerate(text.get("evidence_annotations", [])):
                        for name in ("claim_text", "scope"):
                            yield (f"{item_path}.evidence_annotations[{j}].{name}",
                                   annotation.get(name), annotation.get("question_id"))
                else:
                    yield item_path, text, qid
        for i, insight in enumerate(group.get("additional_insights", [])):
            for name in ("text", "scope"):
                yield f"{path}.additional_insights[{i}].{name}", insight.get(name), qid
        for i, outcome in enumerate(group.get("notable_outcomes", [])):
            yield f"{path}.notable_outcomes[{i}].outcome", outcome.get("outcome", ""), qid


def _structured_quotes(report):
    groups = [("executive_summary", report.get("executive_summary", {}), None)]
    groups += [(f"questions[{i}]", question, question.get("question_id"))
               for i, question in enumerate(report.get("questions", []))]
    for path, group, qid in groups:
        for i, outcome in enumerate(group.get("notable_outcomes", [])):
            yield f"{path}.notable_outcomes[{i}]", outcome, qid


def _blockquote_texts(text):
    """Collect contiguous Markdown blockquote lines without a quote regex."""
    current = []
    for line in [*text.splitlines(), ""]:
        stripped = line.lstrip()
        if stripped.startswith(">"):
            current.append(stripped[1:].lstrip())
        elif current:
            yield " ".join(current)
            current = []


def verify_report_evidence(report, records):
    """Return hard issues for ungrounded, unclosed, or mismatched quotations."""
    entries = evidence_index(records)
    issues = []

    def issue(path, check, message):
        issues.append({"severity": "error", "check": check,
                       "message": message, "context": path})

    for i, question in enumerate(report.get("questions", [])):
        lead_id = question.get("lead_quote_id")
        if lead_id is not None and not any(
            entry["mention_id"] == lead_id and entry["question_id"] == question.get("question_id")
            for entry in entries
        ):
            issue(f"questions[{i}].lead_quote_id", "lead_quote_reference",
                  "Leading quote must reference retained evidence for this question.")

    for path, text, qid in _narrative_fields(report):
        if text is None and path.rsplit(".", 1)[-1] in {"headline", "body", "synthesis"}:
            continue  # An explicitly omitted optional component is not prose.
        if not isinstance(text, str):
            issue(path, "invalid_prose", "Narrative field must be a string.")
            continue
        spans, unmatched = quoted_spans(text)
        if unmatched:
            issue(path, "unclosed_quote", "Unbalanced quotation; use balanced double quotes for customer evidence.")
        quotations = [span["quote"] for span in spans]
        # A blockquote is itself a claim of verbatim source text. Strip a
        # complete enclosing quote pair, if present, rather than its contents.
        for block in _blockquote_texts(text):
            block_spans, _ = quoted_spans(block)
            if len(block_spans) == 1 and block_spans[0]["start"] == 0 and block_spans[0]["end"] == len(block):
                block = block_spans[0]["quote"]
            quotations.append(block)
        for quote in quotations:
            if not matching_evidence(quote, entries, qid):
                issue(path, "ungrounded_quote", f"Quotation is not a contiguous excerpt of retained evidence: {quote[:100]!r}")
        # These new fields are not visited by the legacy attribution walker.
        if any(part in path for part in (".additional_insights[", ".evidence_annotations[", ".key_findings[")):
            for pair in extract_quote_attribution_pairs(text):
                if not pair["attribution"]:
                    continue
                matches = matching_evidence(pair["quote"], entries, qid)
                if matches and not any(_normalize_attribution(pair["attribution"]) ==
                                       _normalize_attribution(m["quote_attribution"]) for m in matches):
                    issue(path, "attribution_mismatch", "Narrative attribution must match the same retained quote and question.")

    for path, outcome, qid in _structured_quotes(report):
        quote = outcome.get("quote")
        attribution = outcome.get("attribution")
        if not isinstance(quote, str) or not isinstance(attribution, str) or not attribution.strip():
            issue(path, "invalid_structured_quote", "A structured quote requires text and its retained attribution.")
            continue
        matches = matching_evidence(quote, entries, qid)
        if not matches:
            issue(path, "ungrounded_quote", "Structured quote is absent from retained evidence for this question.")
        elif not any(_normalize_attribution(attribution) == _normalize_attribution(m["quote_attribution"]) for m in matches):
            issue(path, "attribution_mismatch", "Copy the attribution from the same retained mention as the quote.")
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Run root")
    parser.add_argument("--report", default="synthesis/output/report.json", help="Report path relative to root")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        gate = verify_gate(root)
        if not gate["complete"]:
            raise ValueError(gate["reason"])
        report = read_json(root / args.report)
        records = read_records(root / "synthesis/audit/validated_mentions.jsonl")
        issues = verify_report_evidence(report, records)
        print(json.dumps({"complete": not issues, "issues": issues}, indent=2))
        if issues:
            parser.exit(1)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Evidence check stopped: {exc}\n")


if __name__ == "__main__":
    main()

"""Verify executive summary claims trace to question syntheses or findings.

Usage: python -m v2.scripts.verify_crossref report.json findings.json
"""

import argparse
import json
import re
import sys


# Sentences with numbers are considered claims worth tracing
_NUMBER_RE = re.compile(r"\d+%|\d+\s+(?:of|/)\s+\d+|\b\d{2,}\b")


def extract_claims(report):
    """Extract sentences with numbers from executive summary.

    Returns list of dicts with keys: text, field_path.
    """
    claims = []
    es = report.get("executive_summary", {})

    for field in ("headline", "body"):
        text = es.get(field, "")
        if not text:
            continue
        # Split on sentence boundaries
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for sentence in sentences:
            sentence = sentence.strip()
            if _NUMBER_RE.search(sentence):
                claims.append({
                    "text": sentence,
                    "field_path": f"executive_summary.{field}",
                })

    for i, no in enumerate(es.get("notable_outcomes", [])):
        outcome_text = no.get("outcome", "")
        if outcome_text and _NUMBER_RE.search(outcome_text):
            claims.append({
                "text": outcome_text,
                "field_path": f"executive_summary.notable_outcomes[{i}].outcome",
            })

    return claims


def _extract_key_terms(text):
    """Extract significant terms from a claim for matching."""
    # Extract numbers
    numbers = set()
    for m in re.finditer(r"(\d+)%", text):
        numbers.add(m.group(1))
    for m in re.finditer(r"(\d+)\s*(?:of|/)\s*(\d+)", text):
        numbers.add(m.group(1))
        numbers.add(m.group(2))

    # Extract meaningful words (lowercase, 4+ chars, not stopwords)
    stopwords = {"this", "that", "with", "from", "have", "were", "been", "their",
                 "also", "more", "most", "than", "them", "they", "will", "about",
                 "across", "other"}
    words = set()
    for word in re.findall(r"[a-zA-Z]{4,}", text.lower()):
        if word not in stopwords:
            words.add(word)

    return numbers, words


def find_claim_source(claim, report, findings):
    """Find matching question synthesis or finding for a claim.

    Returns list of source dicts with keys: source_type, source_id, match_detail.
    """
    sources = []
    claim_numbers, claim_words = _extract_key_terms(claim["text"])

    if not claim_numbers:
        return sources

    # Check question syntheses
    for question in report.get("questions", []):
        syn_numbers, syn_words = _extract_key_terms(question.get("synthesis") or "")
        # Match if any claim number appears in synthesis
        number_overlap = claim_numbers & syn_numbers
        word_overlap = claim_words & syn_words
        if number_overlap and len(word_overlap) >= 2:
            sources.append({
                "source_type": "synthesis",
                "source_id": question["question_id"],
                "match_detail": f"numbers={number_overlap}, words={len(word_overlap)} overlap",
            })

    # Check findings themes
    for question in findings.get("questions", []):
        for theme in question.get("themes", []):
            theme_numbers = {
                str(theme.get("mention_pct", "")),
                str(theme.get("source_type_pct", "")),
                str(theme.get("mention_count", "")),
                str(theme.get("source_type_count", "")),
            }
            number_overlap = claim_numbers & theme_numbers
            if number_overlap:
                # Require word overlap to avoid false matches on common numbers
                theme_description_lower = theme.get("description", "").lower()
                _, theme_words = _extract_key_terms(theme_description_lower)
                word_overlap = claim_words & theme_words
                if len(word_overlap) >= 1:
                    sources.append({
                        "source_type": "finding",
                        "source_id": theme["theme_id"],
                        "match_detail": f"numbers={number_overlap}, words={word_overlap}",
                    })

    return sources


def verify_crossrefs(report, findings, claim_bindings=None):
    """Verify all executive summary claims trace to sources.

    Returns list of Issue dicts.
    """
    issues = []
    claims = extract_claims(report)

    question_ids = {q["question_id"] for q in report.get("questions", [])}

    for claim in claims:
        binding = (claim_bindings or {}).get(claim["field_path"])
        if binding is not None:
            from skill.scripts.claim_quantities import validate_quantities
            field = claim["field_path"].split(".", 1)[1]
            valid = (isinstance(binding, dict) and binding.get("text") == report["executive_summary"].get(field)
                     and isinstance(binding.get("claim_ids"), list) and bool(binding["claim_ids"])
                     and all(isinstance(cid, str) for cid in binding["claim_ids"])
                     and len(set(binding["claim_ids"])) == len(binding["claim_ids"])
                     and isinstance(binding.get("question_ids"), list) and bool(binding["question_ids"])
                     and set(binding["question_ids"]) <= question_ids
                     and isinstance(binding.get("statistics"), list)
                     and all(stat.get("scope") in binding["question_ids"] for stat in binding["statistics"])
                     and not validate_quantities(binding["text"], binding["statistics"]))
            if not valid:
                issues.append({"severity": "error", "check": "invalid_claim_binding",
                               "message": "Executive claim binding is stale or has unsupported scoped quantities.",
                               "context": f"field={claim['field_path']}"})
                continue
            sources = [{"source_type": "accepted_claim", "source_id": qid,
                        "match_detail": "Exact admitted component with derived support statistics."}
                       for qid in binding["question_ids"]]
        else:
            sources = find_claim_source(claim, report, findings)
        if not sources:
            issues.append({
                "severity": "error",
                "check": "untraced_claim",
                "message": f"Exec summary claim has no traceable source: \"{claim['text'][:80]}\"",
                "context": f"field={claim['field_path']}",
            })
        else:
            # Check for cross-question references
            source_qids = set()
            for src in sources:
                if src["source_type"] in {"synthesis", "accepted_claim"}:
                    source_qids.add(src["source_id"])
                elif src["source_type"] == "finding":
                    # Extract question ID from theme ID (Q1_T1 -> Q1)
                    tid = src["source_id"]
                    qid = tid.split("_T")[0] if "_T" in tid else tid
                    source_qids.add(qid)

            if len(source_qids) > 1:
                issues.append({
                    "severity": "warning",
                    "check": "cross_question_unverified",
                    "message": (
                        f"Claim spans multiple questions ({', '.join(sorted(source_qids))}): "
                        f"\"{claim['text'][:60]}\""
                    ),
                    "context": f"field={claim['field_path']}",
                })

    return issues


def main():
    parser = argparse.ArgumentParser(description="Verify executive summary cross-references")
    parser.add_argument("report", help="Path to report.json")
    parser.add_argument("findings", help="Path to findings.json")
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    with open(args.findings, encoding="utf-8") as f:
        findings = json.load(f)

    issues = verify_crossrefs(report, findings)
    for issue in issues:
        print(f"[{issue['severity'].upper()}] {issue['message']}")
    sys.exit(1 if any(i["severity"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()

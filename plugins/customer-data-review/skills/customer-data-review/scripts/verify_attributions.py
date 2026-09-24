"""Verify that quote attributions in synthesis text match findings source data.

Extracts quoted text + attribution pairs from report synthesis and executive
summary, matches each quote against sample_quotes in findings.json, and
verifies that the attribution matches.

Usage: python -m skill.scripts.verify_attributions report.json findings.json
"""

import argparse
import json
import re
import sys


# Smart quote normalization (same table as verify_quotes.py)
_SMART_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'",   # single curly quotes
    "\u201c": '"', "\u201d": '"',   # double curly quotes
    "\u2013": "-", "\u2014": "-",   # en-dash, em-dash
    "\u2026": "...",                 # ellipsis
})


def _normalize(text):
    """Normalize text for fuzzy matching — collapse whitespace, lower, strip quotes."""
    result = text.translate(_SMART_QUOTES)
    result = re.sub(r"\s+", " ", result).strip().lower()
    return result


# --- Quote + attribution extraction from report text ---

# Patterns for quoted text (smart and straight double quotes)
_QUOTED_TEXT_RE = re.compile(
    r'(?:'
    r'[\u201c"]'           # opening quote (smart or straight)
    r'(.{10,300}?)'        # quote body (10-300 chars, non-greedy)
    r'[\u201d"]'           # closing quote (smart or straight)
    r')'
)

# Attribution patterns AFTER a closing quote:
#   "quote," says Name, Title.
#   "quote" — Name, Title
#   "quote," notes Name, Title.
#   "quote," according to Name, Title.
_ATTR_AFTER_RE = re.compile(
    r'[\u201d"]\s*'                                # closing quote
    r'[,.]?\s*'                                    # optional comma/period
    r'(?:[-\u2013\u2014]+\s*'                      # dash attribution: — Name
    r'|(?:says?|notes?|explains?|describes?|'
    r'observes?|recalls?|adds?|mentions?|'
    r'according\s+to|reported\s+by)\s+)'           # verb attribution: says Name
    r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*'      # Name (possibly multi-word)
    r'(?:\s*,\s*[^."\u201c\u201d]{2,60})?)',       # , Title (optional)
    re.UNICODE
)

# Attribution patterns BEFORE an opening quote:
#   Name, Title, says: "quote"
#   As Name, Title, puts it: "quote"
#   Name, Title: "quote"
_ATTR_BEFORE_RE = re.compile(
    r'(?:^|(?<=\.\s)|(?<=\n))'                     # start of text/sentence
    r'(?:As\s+)?'                                   # optional "As"
    r'([A-Z][A-Za-z]+\s*,\s*[^."\u201c\u201d]{2,60}?)'  # Name, Title
    r'\s*,?\s*'
    r'(?:says?|notes?|explains?|describes?|'
    r'observes?|recalls?|adds?|mentions?|'
    r'puts?\s+it|said)\s*'
    r'[:]\s*'                                       # colon before quote
    r'[\u201c"]',                                   # opening quote
    re.UNICODE
)


def extract_quote_attribution_pairs(text):
    """Extract (quote_text, attribution) pairs from report text.

    Returns list of dicts with keys: quote, attribution, field_path (set later).
    Attribution may be None if a quote is found without a detectable attribution.
    """
    pairs = []

    # Find all quoted spans
    for m in _QUOTED_TEXT_RE.finditer(text):
        quote_text = m.group(1).strip()
        attribution = None

        # Try attribution after the quote
        after_start = m.start()
        # Build a window including the closing quote and text after it
        after_window = text[after_start:min(len(text), m.end() + 150)]
        after_match = _ATTR_AFTER_RE.search(after_window)
        if after_match:
            attribution = after_match.group(1).strip()

        # Try attribution before the quote if not found after
        if attribution is None:
            before_start = max(0, m.start() - 150)
            before_window = text[before_start:m.end()]
            before_match = _ATTR_BEFORE_RE.search(before_window)
            if before_match:
                attribution = before_match.group(1).strip()

        pairs.append({
            "quote": quote_text,
            "attribution": attribution,
        })

    return pairs


def extract_all_quote_attributions(report):
    """Walk report text fields and extract quote-attribution pairs.

    Returns list of dicts with keys: quote, attribution, field_path.
    """
    all_pairs = []

    es = report.get("executive_summary", {})
    for field in ("headline", "body"):
        text = es.get(field, "")
        if not text:
            continue
        for pair in extract_quote_attribution_pairs(text):
            pair["field_path"] = f"executive_summary.{field}"
            all_pairs.append(pair)

    for i, question in enumerate(report.get("questions", [])):
        if question.get("synthesis"):
            for pair in extract_quote_attribution_pairs(question["synthesis"]):
                pair["field_path"] = f"questions[{i}].synthesis"
                all_pairs.append(pair)
        for j, takeaway in enumerate(question.get("takeaways", [])):
            for pair in extract_quote_attribution_pairs(takeaway):
                pair["field_path"] = f"questions[{i}].takeaways[{j}]"
                all_pairs.append(pair)
        for j, no in enumerate(question.get("notable_outcomes", [])):
            if no.get("quote") and no.get("attribution"):
                all_pairs.append({
                    "quote": no["quote"],
                    "attribution": no["attribution"],
                    "field_path": f"questions[{i}].notable_outcomes[{j}]",
                })

    for j, no in enumerate(es.get("notable_outcomes", [])):
        if no.get("quote") and no.get("attribution"):
            all_pairs.append({
                "quote": no["quote"],
                "attribution": no["attribution"],
                "field_path": f"executive_summary.notable_outcomes[{j}]",
            })

    return all_pairs


def _build_quote_registry(findings):
    """Build a registry of known quotes and their attributions from findings.

    Returns list of dicts with keys: quote_normalized, attribution, theme_id.
    """
    registry = []
    for question in findings.get("questions", []):
        for theme in question.get("themes", []):
            theme_id = theme.get("theme_id", "unknown")
            for sq in theme.get("sample_quotes", []):
                quote_text = sq.get("quote", "")
                if not quote_text:
                    continue
                registry.append({
                    "quote_normalized": _normalize(quote_text),
                    "attribution": sq.get("attribution", ""),
                    "theme_id": theme_id,
                    "quote_raw": quote_text,
                })
                # Also index display_quote so cleaned quotes in synthesis
                # prose still match back to findings
                display_text = sq.get("display_quote", "")
                if display_text and display_text != quote_text:
                    registry.append({
                        "quote_normalized": _normalize(display_text),
                        "attribution": sq.get("attribution", ""),
                        "theme_id": theme_id,
                        "quote_raw": display_text,
                    })
    return registry


def _match_quote(quote_normalized, registry, threshold=0.6):
    """Find the best matching quote in the registry.

    Uses substring matching: if the normalized report quote is a substring
    of a registry quote (or vice versa), it's a match. For partial quotes
    (synthesis may truncate), checks if enough of the shorter string appears
    in the longer one.

    Returns the matching registry entry or None.
    """
    best_match = None
    best_overlap = 0

    for entry in registry:
        reg_quote = entry["quote_normalized"]

        # Exact match
        if quote_normalized == reg_quote:
            return entry

        # Substring match (synthesis may use a truncated or slightly modified quote)
        if quote_normalized in reg_quote or reg_quote in quote_normalized:
            overlap = min(len(quote_normalized), len(reg_quote))
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = entry
            continue

        # Word overlap ratio for fuzzy matching
        quote_words = set(quote_normalized.split())
        reg_words = set(reg_quote.split())
        if not quote_words or not reg_words:
            continue
        overlap_count = len(quote_words & reg_words)
        smaller = min(len(quote_words), len(reg_words))
        ratio = overlap_count / smaller if smaller > 0 else 0
        if ratio >= threshold and overlap_count > best_overlap:
            best_overlap = overlap_count
            best_match = entry

    return best_match


def _normalize_attribution(attr):
    """Normalize an attribution string for comparison.

    Strips common prefixes/suffixes, normalizes whitespace, lowercases.
    """
    if not attr:
        return ""
    result = attr.strip().rstrip(".,;:")
    # Remove only a complete outer wrapper. strip("()") also removes the
    # closing delimiter from "Cheryl (sales call)", leaving a broken name.
    if re.fullmatch(r"\([^()]*\)", result):
        result = result[1:-1].strip()
    result = re.sub(r"\s+", " ", result).lower()
    return result


def _attributions_match(report_attr, findings_attr):
    """Check if a report attribution matches a findings attribution.

    Flexible: the report attribution's name portion must appear in the
    findings attribution (or vice versa). Titles may be abbreviated.
    """
    r = _normalize_attribution(report_attr)
    f = _normalize_attribution(findings_attr)

    if not r or not f:
        return False

    # Exact match
    if r == f:
        return True

    # Extract just the name (first word or first comma-delimited part)
    r_name = re.sub(r"\s*\([^()]*\)\s*$", "", r.split(",")[0]).strip()
    f_name = re.sub(r"\s*\([^()]*\)\s*$", "", f.split(",")[0]).strip()

    # Name must match (the person's name is the critical part)
    return r_name == f_name


def verify_attributions(report, findings):
    """Verify all quote attributions in report against findings.

    Returns list of Issue dicts with severity, check, message, context.
    Also returns summary stats for methodology.
    """
    issues = []
    registry = _build_quote_registry(findings)
    pairs = extract_all_quote_attributions(report)

    matched_count = 0
    correct_count = 0

    for pair in pairs:
        if pair["attribution"] is None:
            # Quote without detectable attribution — not an attribution error
            continue

        quote_norm = _normalize(pair["quote"])
        match = _match_quote(quote_norm, registry)

        if match is None:
            # Quote not found in findings — could be paraphrased or from
            # high-intensity outliers. Not an attribution error per se.
            issues.append({
                "severity": "warning",
                "check": "unmatched_quote",
                "message": (
                    f"Quote in {pair['field_path']} not found in findings "
                    f"sample_quotes: \"{pair['quote'][:60]}...\""
                ),
                "context": f"attribution={pair['attribution']}",
            })
            continue

        matched_count += 1

        if not _attributions_match(pair["attribution"], match["attribution"]):
            issues.append({
                "severity": "error",
                "check": "attribution_mismatch",
                "message": (
                    f"Attribution mismatch in {pair['field_path']}: "
                    f"report says \"{pair['attribution']}\" but findings "
                    f"says \"{match['attribution']}\""
                ),
                "context": (
                    f"quote=\"{pair['quote'][:50]}...\", "
                    f"theme={match['theme_id']}"
                ),
            })
        else:
            correct_count += 1

    return issues, matched_count, correct_count


def main():
    parser = argparse.ArgumentParser(
        description="Verify quote attributions in report against findings"
    )
    parser.add_argument("report", help="Path to report.json")
    parser.add_argument("findings", help="Path to findings.json")
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    with open(args.findings, encoding="utf-8") as f:
        findings = json.load(f)

    issues, matched, correct = verify_attributions(report, findings)
    for issue in issues:
        print(f"[{issue['severity'].upper()}] {issue['message']}")

    if matched > 0:
        accuracy = round(correct / matched, 2)
        print(f"\nAttribution accuracy: {correct}/{matched} ({accuracy:.0%})")

    sys.exit(1 if any(i["severity"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()

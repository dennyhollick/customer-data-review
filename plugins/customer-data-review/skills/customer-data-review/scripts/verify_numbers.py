"""Verify numbers in report.json against findings.json.

Extracts percentages, counts, and fractions from report text fields,
verifies each against a registry built from findings, and checks
adjective-percentage alignment.

Usage: python -m v2.scripts.verify_numbers report.json findings.json
"""

import argparse
import json
import re
import sys

from skill.schemas.constants import ADJECTIVE_THRESHOLDS
from skill.scripts.verify_report_evidence import _narrative_fields

# Regex to match Q-references like Q1, Q2 (not treated as numbers)
_Q_REF_RE = re.compile(r"^Q\d+$")

# Number extraction patterns
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_FRACTION_RE = re.compile(r"(\d+)\s*(?:of|/)\s*(\d+)")
_COUNT_RE = re.compile(r"\b(\d+)\b")

# Contextual patterns — standalone counts matching these are NOT findings references
_DOLLAR_PREFIX_RE = re.compile(r"\$\s*\d")
_RANGE_RE = re.compile(r"\d+\s*[-–—]\s*\d+")
_APPROX_RE = re.compile(r"[~≈]\s*\d")
_UNIT_WORDS = frozenset({
    "employees", "employee", "users", "user", "companies", "company",
    "people", "person", "hours", "hour", "minutes", "minute",
    "days", "day", "weeks", "week", "months", "month", "years", "year",
    "words", "word", "pages", "page", "dollars", "dollar",
    "accounts", "account", "customers", "customer",
    "teams", "team", "members", "member", "respondents", "respondent",
})


# Quote characters for span detection (opening → closing pairs)
_QUOTE_PAIRS = [
    ("\u201c", "\u201d"),  # smart double quotes: \u201c \u201d
    ("\u2018", "\u2019"),  # smart single quotes: \u2018 \u2019
    ('"', '"'),            # straight double quotes
]
_MAX_QUOTE_SPAN = 300  # max chars between open and close quote


def _find_quoted_spans(text: str) -> list[tuple[int, int]]:
    """Find regions enclosed in quotation marks.

    Handles smart quotes (curly), straight double quotes.
    Does NOT treat straight single quotes as quote delimiters
    (ambiguous with apostrophes like "customer's").

    Max span of 300 chars prevents unclosed quotes from
    exempting everything after the opening mark.

    Returns list of (start, end) tuples (inclusive of quote chars).
    """
    spans = []
    for open_char, close_char in _QUOTE_PAIRS:
        i = 0
        while i < len(text):
            start = text.find(open_char, i)
            if start == -1:
                break
            # For straight double quotes, the same char opens and closes
            search_from = start + 1
            end = text.find(close_char, search_from)
            if end == -1 or (end - start) > _MAX_QUOTE_SPAN:
                # Unclosed or too-long span — skip this opening quote
                i = start + 1
                continue
            spans.append((start, end + len(close_char)))
            i = end + len(close_char)
    return spans


def _position_in_quoted_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    """Check if a character position falls inside any quoted span."""
    return any(start <= pos < end for start, end in spans)


def build_number_registry(findings, methodology=None, statistics=None):
    """Build per-question and global registries of numeric values.

    Returns dict with:
      - "global": {value_str: [contexts]} — all numbers pooled
      - "Q1", "Q2", ...: {value_str: [contexts]} — scoped per question

    Numbers in question syntheses/takeaways are checked against the
    per-question registry. Executive summary uses the global registry.
    Methodology numbers are registered globally so exec summary can cite them.
    """
    registries = {"global": {}}

    def _register(value, context, qid=None):
        key = str(value)
        registries["global"].setdefault(key, []).append(context)
        if qid:
            registries.setdefault(qid, {}).setdefault(key, []).append(context)

    for question in findings.get("questions", []):
        qid = question["question_id"]

        _register(question["total_mentions"], f"{qid} total_mentions", qid)
        _register(question["total_source_types"], f"{qid} total_source_types", qid)

        for theme in question.get("themes", []):
            tid = theme["theme_id"]
            _register(theme["mention_count"], f"{tid} mention_count", qid)
            _register(theme["mention_pct"], f"{tid} mention_pct", qid)
            _register(theme["source_type_count"], f"{tid} source_type_count", qid)
            _register(theme["source_type_pct"], f"{tid} source_type_pct", qid)

            for category, count in theme.get("source_type_breakdown", {}).items():
                _register(count, f"{tid} source_type_breakdown.{category}", qid)

    # Register numbers found inside sample quotes — these are customer-stated
    # figures (headcounts, dollar amounts, etc.) that may appear in narrative
    # text without quote-wrapping. Domain-independent: covers any vocabulary.
    for question in findings.get("questions", []):
        qid = question["question_id"]
        for theme in question.get("themes", []):
            for sq in theme.get("sample_quotes", []):
                quote_text = sq.get("quote", "")
                for m in _COUNT_RE.finditer(quote_text):
                    val = m.group(1)
                    if len(val) >= 2:  # Skip single digits
                        tid = theme["theme_id"]
                        _register(val, f"{tid} sample_quote", qid)

    # Register methodology numbers so exec summary can cite them
    if methodology:
        for field in ("source_count", "excluded_count", "total_interactions",
                      "question_count"):
            value = methodology.get(field)
            if value is not None:
                _register(value, f"methodology.{field}")

    # Packet-mode statistics include distinct source files, unlike the legacy
    # source_type counts. Validate their exact sets before registering values.
    for qid, rows in (statistics or {}).items():
        if qid not in {q["question_id"] for q in findings.get("questions", [])}:
            raise ValueError("Statistics belong to an unknown question.")
        for stat in rows:
            numerator, denominator = stat["numerator_ids"], stat["denominator_ids"]
            if (stat["scope"] != qid or stat["unit"] not in ("mentions", "source_files")
                    or len(set(numerator)) != len(numerator) or len(set(denominator)) != len(denominator)
                    or not set(numerator) <= set(denominator)
                    or stat["numerator"] != len(numerator) or stat["denominator"] != len(denominator)
                    or stat["percentage"] != (round(len(numerator) / len(denominator) * 100) if denominator else 0)):
                raise ValueError("Statistics must match exact scoped numerator/denominator sets.")
            for field in ("numerator", "denominator", "percentage"):
                _register(stat[field], f"{stat['stat_ref']} {field}", qid)
    return registries


def extract_numbers_from_text(text, field_path):
    """Extract numbers from a text field.

    Returns list of dicts with keys: value, type, field_path, match.
    Skips Q-references (Q1, Q2, etc.) and numbers inside quotation marks
    (customer quotes may contain numbers that are not findings data).
    """
    numbers = []
    quoted_spans = _find_quoted_spans(text)

    # Extract percentages
    for m in _PCT_RE.finditer(text):
        if _position_in_quoted_span(m.start(), quoted_spans):
            continue
        value = m.group(1)
        # Normalize: strip trailing .0
        if "." in value:
            value = str(round(float(value)))
        numbers.append({
            "value": value,
            "type": "percentage",
            "field_path": field_path,
            "match": m.group(0),
        })

    # Extract fractions (e.g., "14 of 50", "14/50")
    for m in _FRACTION_RE.finditer(text):
        if _position_in_quoted_span(m.start(), quoted_spans):
            continue
        numerator = m.group(1)
        denominator = m.group(2)
        numbers.append({
            "value": numerator,
            "type": "fraction_numerator",
            "field_path": field_path,
            "match": m.group(0),
        })
        numbers.append({
            "value": denominator,
            "type": "fraction_denominator",
            "field_path": field_path,
            "match": m.group(0),
        })

    # Extract standalone counts (numbers not already captured by % or fraction)
    already_captured = set()
    for m in _PCT_RE.finditer(text):
        already_captured.add(m.start())
        # Also mark the number part
        num_match = re.search(r"\d+", m.group(0))
        if num_match:
            already_captured.add(m.start() + num_match.start())
    for m in _FRACTION_RE.finditer(text):
        already_captured.add(m.start())

    for m in _COUNT_RE.finditer(text):
        if m.start() in already_captured:
            continue
        if _position_in_quoted_span(m.start(), quoted_spans):
            continue
        # Check if this number is part of a percentage or fraction already extracted
        # by checking if it's within a known match span
        skip = False
        for pm in _PCT_RE.finditer(text):
            if pm.start() <= m.start() < pm.end():
                skip = True
                break
        if skip:
            continue
        for fm in _FRACTION_RE.finditer(text):
            if fm.start() <= m.start() < fm.end():
                skip = True
                break
        if skip:
            continue

        value = m.group(1)

        # Skip Q-references (e.g., Q1, Q2)
        if m.start() > 0 and text[m.start() - 1] == "Q":
            continue

        # Skip single-digit numbers — too common to be meaningful data refs
        if len(value) == 1:
            continue

        # Skip dollar amounts ($50, $180K)
        prefix = text[max(0, m.start() - 3):m.start()].rstrip()
        if prefix.endswith("$"):
            continue

        # Skip numbers in ranges (50-500, 180–300)
        if _RANGE_RE.search(text[max(0, m.start() - 6):m.end() + 6]):
            continue

        # Skip approximate numbers (~100, ≈200)
        if m.start() > 0 and _APPROX_RE.match(text[max(0, m.start() - 2):m.end()]):
            continue

        # Skip numbers followed by unit words (200 employees, 40 hours)
        after = text[m.end():m.end() + 20].lstrip()
        first_word_after = after.split()[0].lower().rstrip(".,;:!?") if after.split() else ""
        if first_word_after in _UNIT_WORDS:
            continue

        # Skip 4-digit year references (2019, 2024, etc.)
        if len(value) == 4 and value[:2] in ("19", "20"):
            continue

        numbers.append({
            "value": value,
            "type": "count",
            "field_path": field_path,
            "match": m.group(0),
        })

    return numbers


def extract_all_numbers(report):
    """Walk all report text fields and extract numbers.

    Returns list of number dicts from extract_numbers_from_text.
    """
    numbers = []
    for path, text, qid in _narrative_fields(report):
        if isinstance(text, str) and ".key_questions[" not in path:
            for number in extract_numbers_from_text(text, path):
                number["question_id"] = qid
                numbers.append(number)
    return numbers


def check_adjective_thresholds(report, findings):
    """Check that adjectives near percentages match threshold bands.

    Scans a window of ~10 words around each percentage for threshold adjectives.
    Returns list of Issue dicts for mismatches.
    """
    issues = []

    def _check_text(text, field_path):
        for m in _PCT_RE.finditer(text):
            pct_str = m.group(1)
            pct = round(float(pct_str))

            # Get surrounding window (~10 words each direction)
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 80)
            window = text[start:end].lower()

            for adjective, (lo, hi) in ADJECTIVE_THRESHOLDS.items():
                if re.search(r"(?<!\w)" + re.escape(adjective) + r"(?!\w)", window):
                    if not (lo <= pct < hi):
                        # Find the correct band for this percentage
                        correct_bands = [
                            adj for adj, (l, h) in ADJECTIVE_THRESHOLDS.items()
                            if l <= pct < h
                        ]
                        hi_display = hi - 1 if hi > 100 else hi
                        issues.append({
                            "severity": "warning",
                            "check": "adjective_threshold",
                            "message": (
                                f'"{adjective}" used near {pct}% in {field_path}, '
                                f"but threshold is {lo}-{hi_display}%"
                            ),
                            "context": f"Suggested: {', '.join(correct_bands[:3]) if correct_bands else 'none'}",
                        })

    for path, text, _qid in _narrative_fields(report):
        if isinstance(text, str) and ".key_questions[" not in path:
            _check_text(text, path)

    return issues


_ORDINAL_RE = re.compile(
    r'(?:most\s+(?:frequent|common|cited|mentioned|prevalent|popular|reported))'
    r'|(?:top\s+(?:theme|issue|concern|driver|factor|priority|complaint))'
    r'|(?:#1\s+(?:theme|issue|concern|complaint))'
    r'|(?:single\s+largest)',
    re.IGNORECASE,
)


def check_ordinal_claims(report, findings):
    """Check that ordinal claims (#1, most frequent, top theme) match rankings."""
    issues = []

    # Build rankings per question
    rankings = {}
    for fq in findings.get("questions", []):
        qid = fq["question_id"]
        active = [t for t in fq.get("themes", []) if not t.get("demoted")]
        rankings[qid] = sorted(active, key=lambda t: -t.get("mention_count", 0))

    def _check_text(text, field_path, qid):
        ranked = rankings.get(qid, [])
        if len(ranked) < 2:
            return
        top = ranked[0]
        top_label = top.get("label", "").lower()

        for m in _ORDINAL_RE.finditer(text):
            window_start = max(0, m.start() - 120)
            window_end = min(len(text), m.end() + 120)
            window = text[window_start:window_end].lower()

            for theme in ranked[1:]:
                label = theme.get("label", "").lower()
                if not label:
                    continue
                label_words = [w for w in label.split() if len(w) >= 4]
                for word in label_words:
                    if word in window and word not in top_label:
                        issues.append({
                            "severity": "error",
                            "check": "ordinal_claim",
                            "message": (
                                f"Ordinal claim '{m.group()}' in {field_path} "
                                f"may reference '{theme['label']}' (rank #{ranked.index(theme)+1}, "
                                f"{theme['mention_count']} mentions) but "
                                f"actual #1 is '{top['label']}' ({top['mention_count']} mentions)"
                            ),
                        })
                        return

    for path, text, qid in _narrative_fields(report):
        if qid and isinstance(text, str):
            _check_text(text, path, qid)

    return issues


_Q_IDX_RE = re.compile(r"questions\[(\d+)\]")


def verify_numbers(report, findings, statistics=None):
    """Full number verification pipeline.

    Numbers in question syntheses/takeaways are checked against the
    per-question registry. Executive summary uses the global registry.

    Returns list of Issue dicts with severity, check, message, context.
    """
    issues = []
    registries = build_number_registry(findings, report.get("methodology"), statistics)

    # Map report question index → question_id
    report_qid_by_index = {}
    for i, question in enumerate(report.get("questions", [])):
        report_qid_by_index[i] = question.get("question_id", "")

    numbers = extract_all_numbers(report)

    for num in numbers:
        field_path = num["field_path"]

        # Select registry: exec summary → global, questions → per-question
        if num.get("question_id"):
            registry = registries.get(num["question_id"], registries["global"])
        elif field_path.startswith("executive_summary"):
            registry = registries["global"]
        else:
            idx_match = _Q_IDX_RE.match(field_path)
            if idx_match:
                idx = int(idx_match.group(1))
                qid = report_qid_by_index.get(idx, "")
                registry = registries.get(qid, registries["global"])
            else:
                registry = registries["global"]

        if num["value"] not in registry:
            issues.append({
                "severity": "error",
                "check": "unverified_number",
                "message": (
                    f"Number {num['match']} in {num['field_path']} "
                    f"not found in findings"
                ),
                "context": f"type={num['type']}, value={num['value']}",
            })

    # Add adjective threshold checks (unscoped — checks value bands, not registry)
    issues.extend(check_adjective_thresholds(report, findings))

    issues.extend(check_ordinal_claims(report, findings))

    return issues


def main():
    parser = argparse.ArgumentParser(description="Verify numbers in report against findings")
    parser.add_argument("report", help="Path to report.json")
    parser.add_argument("findings", help="Path to findings.json")
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    with open(args.findings, encoding="utf-8") as f:
        findings = json.load(f)

    issues = verify_numbers(report, findings)
    for issue in issues:
        print(f"[{issue['severity'].upper()}] {issue['message']}")
    sys.exit(1 if any(i["severity"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()

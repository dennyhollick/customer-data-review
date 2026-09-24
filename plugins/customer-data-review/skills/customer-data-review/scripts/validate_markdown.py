"""Structural validation of generated Markdown reports.

Uses stdlib only (regex). No external dependencies.

Usage: python -m v2.scripts.validate_markdown report.md
"""

import argparse
import re
import sys


def _check_title_present(md):
    """Check report starts with expected title."""
    issues = []
    if not md.startswith("# Customer Feedback Analysis"):
        issues.append({
            "severity": "error",
            "check": "title_present",
            "message": "Missing or incorrect title — expected '# Customer Feedback Analysis'",
            "context": "document structure",
        })
    return issues


def _check_metadata_present(md):
    """Check required metadata lines exist."""
    issues = []
    required = [
        ("**Date:**", "Date"),
        ("**Sources:**", "Sources"),
        ("**Questions:**", "Questions"),
        ("**Mentions retained:**" if "**Mentions retained:**" in md else "**Mentions extracted:**", "Mentions retained (or legacy extracted)"),
    ]
    for pattern, label in required:
        if pattern not in md:
            issues.append({
                "severity": "error",
                "check": "metadata_present",
                "message": f"Missing metadata line: {label}",
                "context": "document header",
            })
    return issues


def _check_exec_summary(md):
    """Check executive summary sections exist.

    At least one of Key Findings or Context must be present.
    Key Findings is optional (key_findings is optional in schema).
    """
    issues = []
    has_findings = "## Key Findings" in md
    has_context = "## Context" in md

    if not has_findings and not has_context:
        issues.append({
            "severity": "error",
            "check": "exec_summary",
            "message": "Missing executive summary — need at least '## Key Findings' or '## Context'",
            "context": "executive summary",
        })
    return issues


def _check_question_sections(md):
    """Check for question sections."""
    issues = []
    question_headings = re.findall(r"^## Q\d+:", md, re.MULTILINE)
    if not question_headings:
        issues.append({
            "severity": "warning",
            "check": "question_sections",
            "message": "No question sections found (expected ## Q1:, ## Q2:, ...)",
            "context": "document structure",
        })
    return issues


def _check_tables_present(md):
    """Check each question section has a pipe table with correct headers."""
    issues = []
    # Find all question sections
    question_starts = [m.start() for m in re.finditer(r"^## Q\d+:", md, re.MULTILINE)]
    if not question_starts:
        return issues

    # Sources and Segments are omitted for single-category datasets. Keep
    # accepting legacy Key Quotes tables when validating older reports.
    expected_header = re.compile(
        r"^\| Theme \| Description \| Mentions \| %"
        r"(?: \| Sources)?(?: \| Segments)?(?: \| Key Quotes)? \|$",
        re.MULTILINE,
    )

    for i, start in enumerate(question_starts):
        # Section ends at next question or end of document
        end = question_starts[i + 1] if i + 1 < len(question_starts) else len(md)
        section = md[start:end]

        # Extract question ID for error messages
        qid_match = re.match(r"## (Q\d+):", section)
        qid = qid_match.group(1) if qid_match else f"Q{i + 1}"

        if not expected_header.search(section):
            issues.append({
                "severity": "warning",
                "check": "tables_present",
                "message": f"Question {qid} missing theme table with expected headers",
                "context": f"section {qid}",
            })

    return issues


def _check_no_html_artifacts(md):
    """Check for leaked HTML tags."""
    issues = []
    html_tags = [
        (r"<div[\s>]", "div"),
        (r"<section[\s>]", "section"),
        (r"<svg[\s>]", "svg"),
        (r"<table[\s>]", "table"),
        (r"<td[\s>]", "td"),
        (r"<tr[\s>]", "tr"),
    ]
    for pattern, tag in html_tags:
        if re.search(pattern, md, re.IGNORECASE):
            issues.append({
                "severity": "error",
                "check": "html_artifacts",
                "message": f"Found HTML <{tag}> tag in markdown output",
                "context": "content cleanup",
            })
    return issues


def _check_no_empty_sections(md):
    """Check for empty sections (heading followed immediately by another heading or rule)."""
    issues = []
    # Match ## heading followed by blank lines then another ## heading or ---
    empty_pattern = re.compile(
        r"^(## .+)\n(?:\s*\n)*(## |---)",
        re.MULTILINE,
    )
    for match in empty_pattern.finditer(md):
        heading = match.group(1)
        issues.append({
            "severity": "warning",
            "check": "empty_sections",
            "message": f"Empty section: '{heading}' has no content",
            "context": "document structure",
        })
    return issues


def validate_markdown(md_str, *, allow_empty_executive=False):
    """Run all Markdown validation checks.

    Args:
        md_str: Complete Markdown string to validate.

    Returns:
        list of Issue dicts.
    """
    issues = []
    issues.extend(_check_title_present(md_str))
    issues.extend(_check_metadata_present(md_str))
    if not allow_empty_executive:
        issues.extend(_check_exec_summary(md_str))
    issues.extend(_check_question_sections(md_str))
    issues.extend(_check_tables_present(md_str))
    issues.extend(_check_no_html_artifacts(md_str))
    issues.extend(_check_no_empty_sections(md_str))
    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate Markdown report")
    parser.add_argument("markdown_file", help="Path to Markdown file")
    args = parser.parse_args()

    with open(args.markdown_file, encoding="utf-8") as f:
        md_str = f.read()

    issues = validate_markdown(md_str)
    for issue in issues:
        print(f"[{issue['severity'].upper()}] {issue['message']}")
    sys.exit(1 if any(i["severity"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()

"""Generate Markdown reports from report.json + findings.json.

Produces:
  - report.md — full human-readable report (via generate_markdown)
  - report-context.md — compressed LLM context document (via format_context_md,
    which formats the output of the compress_context agent)

Builds markdown entirely in code — no template file needed.
Pure Python, no new dependencies.

Usage:
  Full report:   python -m skill.scripts.generate_markdown report.json findings.json [-o output.md]
  Context doc:   python -m skill.scripts.generate_markdown report.json findings.json --context compressed.json [-o output.md]
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

from skill.schemas.validate import validate_json


def audit_summary_lines(meth):
    """Describe recorded audit evidence without inferring missing verification."""
    mode = meth.get("audit_mode")
    lines = []
    if mode == "skip":
        lines.append("Audit skipped: semantic attribution and context were not re-checked.")
    elif mode == "sampled":
        sample = meth.get("audit_sample_size")
        population = meth.get("audit_population")
        coverage = f"{sample} selected mentions" if sample is not None else "sample size unavailable"
        if population is not None:
            coverage += f" from {population} extracted mentions"
        lines.append(f"Sampled audit: {coverage}. This is sample evidence, not a population-accuracy guarantee.")
    else:
        lines.append("Audit mode: not recorded (legacy report).")

    total = meth.get("audit_total")
    passed = meth.get("audit_passed")
    if mode != "skip":
        if total is not None and passed is not None and total > 0:
            lines.append(f"Assessed sample: {passed}/{total} passed ({passed / total:.1%}).")
        elif total == 0:
            lines.append("No assessed audit evidence; pass rate unavailable.")
        elif meth.get("audit_pass_rate") is not None:
            lines.append(f"Recorded audit pass rate: {meth['audit_pass_rate']:.1%}; assessed counts unavailable.")
        else:
            lines.append("Audit pass rate: unavailable.")
        for key, alias, label in (
            ("audit_raw_pass_rate", "audit_pass_rate_raw", "Initial sample agreement"),
            ("audit_corrected_pass_rate", "audit_pass_rate_corrected", "Final sample agreement"),
        ):
            rate = meth.get(key, meth.get(alias))
            if rate is not None:
                lines.append(f"{label}: {rate:.1%}.")

    if meth.get("audit_excluded") is not None:
        lines.append(f"Excluded mentions: {meth['audit_excluded']}.")
    if meth.get("audit_unassessed") is not None:
        lines.append(f"Unassessed mentions: {meth['audit_unassessed']} (not labelled audited).")
    if meth.get("audit_status"):
        lines.append(f"Audit status: {meth['audit_status']}.")
    return lines


def _effective_methodology(report, audit_skipped=False):
    """Explicit metadata wins over the legacy audit_skipped.flag adapter."""
    meth = dict(report.get("methodology", {}))
    if audit_skipped and "audit_mode" not in meth:
        meth["audit_mode"] = "skip"
    return meth


def _normalize_newlines(text):
    """Normalize double-escaped newlines from LLM JSON output."""
    if not text:
        return ""
    text = text.replace("\\n\\n", "\n\n")
    text = text.replace("\\n", "\n")
    return text


def _escape_cell(text):
    """Escape markdown metacharacters in pipe table cell content.

    Handles: pipe, bold/italic markers, backticks, links, headings, blockquotes.
    Also strips newlines that would break table rows.
    """
    s = str(text)
    s = s.replace("\n", " ").replace("\r", " ")
    s = s.replace("\\", "\\\\")
    s = s.replace("|", "\\|")
    s = s.replace("*", "\\*")
    s = s.replace("_", "\\_")
    s = s.replace("`", "\\`")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    s = s.replace("#", "\\#")
    s = s.replace(">", "\\>")
    return s


def _format_quotes(sample_quotes, max_quotes=3):
    """Format sample quotes for display in a table cell.

    Prefers notable quotes first, caps at max_quotes.
    Format: "quote" — attribution (file_id) • "quote2" — attr2 (file_id2)
    """
    if not sample_quotes:
        return ""

    # Sort: notable first, then original order
    sorted_quotes = sorted(sample_quotes, key=lambda q: not q.get("notable", False))
    selected = sorted_quotes[:max_quotes]

    parts = []
    for q in selected:
        quote_text = q.get("display_quote") or q.get("quote", "")
        if not quote_text:
            continue
        attribution = q.get("attribution", "")
        file_id = q.get("file_id", "")

        escaped_quote = _escape_cell(quote_text)
        piece = f'"{escaped_quote}"'
        if attribution:
            piece += f" — {_escape_cell(attribution)}"
        if file_id:
            piece += f" ({_escape_cell(file_id)})"

        parts.append(piece)

    return " • ".join(parts)


def _inject_other_for_outliers(themes, findings_question):
    """Add synthetic Other row for outlier mentions so percentages add to 100%."""
    outliers = findings_question.get("high_intensity_outliers", [])
    if not outliers:
        return
    total_mentions = findings_question.get("total_mentions", 0)
    other_count = len(outliers)
    other_pct = round(other_count / total_mentions * 100) if total_mentions else 0
    total_source_types = findings_question.get("total_source_types", 0)
    outlier_stc = findings_question.get("outlier_source_type_count", 0)
    outlier_stb = findings_question.get("outlier_source_type_breakdown", {})
    outlier_stp = round(outlier_stc / total_source_types * 100) if total_source_types else 0
    themes.append({
        "theme_id": "_other",
        "label": "Other",
        "description": "",
        "type": "other",
        "mention_count": other_count,
        "mention_pct": other_pct,
        "source_type_count": outlier_stc,
        "source_type_pct": outlier_stp,
        "source_type_breakdown": outlier_stb,
        "sample_quotes": [],
        "mention_ids": [o.get("mention_id", "") for o in outliers],
    })


def _build_theme_table(findings_question):
    """Build a pipe table for one question's themes.

    Columns: Theme | Description | Mentions | % | optional Sources / Segments
    Filters demoted themes. Sorts by mention_count desc, 'other' last.
    """
    if not findings_question:
        return ""

    themes = findings_question.get("themes", [])
    themes = [t for t in themes if not t.get("demoted")]
    _inject_other_for_outliers(themes, findings_question)
    if not themes:
        return ""

    # Sort: 'other' type last, then by mention_count descending
    themes = sorted(
        themes,
        key=lambda t: (t.get("type") == "other", -t.get("mention_count", 0)),
    )

    total_mentions = findings_question.get("total_mentions", 0)
    total_source_types = findings_question.get("total_source_types", 0)
    total_segments = findings_question.get("total_segments", 0)
    show_sources = total_source_types > 1
    show_segments = total_segments > 1

    lines = []
    header = "| Theme | Description | Mentions | %"
    sep = "| --- | --- | --- | ---"
    if show_sources:
        header += " | Sources"
        sep += " | ---"
    if show_segments:
        header += " | Segments"
        sep += " | ---"
    header += " | Key Quotes |"
    sep += " | --- |"
    lines.append(header)
    lines.append(sep)

    for theme in themes:
        label = theme.get("label", "")
        description = theme.get("description", "")
        m_count = theme.get("mention_count", 0)
        m_pct = theme.get("mention_pct", 0)
        s_count = theme.get("source_type_count", 0)
        seg_count = theme.get("segment_count", 0)
        seg_breakdown = theme.get("segment_breakdown", {})

        sources_str = f"{s_count}/{total_source_types}"
        segments_str = ", ".join(sorted(seg_breakdown.keys())) if seg_breakdown else ""

        # Show more quotes for large Other buckets
        is_other_type = theme.get("type") == "other"
        max_q = len(theme.get("sample_quotes", [])) if (is_other_type and m_pct > 15) else 3
        quotes_str = _format_quotes(theme.get("sample_quotes", []), max_quotes=max_q)

        # Build row dynamically based on visible columns
        row = (
            f"| {_escape_cell(label)} "
            f"| {_escape_cell(description)} "
            f"| {m_count} "
            f"| {m_pct}%"
        )
        if show_sources:
            row += f" | {sources_str}"
        if show_segments:
            row += f" | {_escape_cell(segments_str)}"
        row += f" | {quotes_str or ' '} |"
        lines.append(row)

    return "\n".join(lines)


def _build_header(report, findings):
    """Build the report header: title, metadata, headline."""
    meth = report.get("methodology", {})
    es = report.get("executive_summary", {})

    # Total mentions from findings
    total_mentions = sum(
        q.get("total_mentions", 0) for q in findings.get("questions", [])
    )

    # Source types breakdown: sorted by count descending, lowercased
    source_types = meth.get("source_types", {})
    sorted_types = sorted(source_types.items(), key=lambda x: -x[1])
    type_parts = [f"{count} {name.lower()}" for name, count in sorted_types]
    source_count = meth.get("source_count", 0)

    lines = []
    lines.append("# Customer Feedback Analysis")
    lines.append("")
    lines.append(f"**Date:** {datetime.date.today().isoformat()}")

    if type_parts:
        lines.append(f"**Sources:** {source_count} files ({', '.join(type_parts)})")
    else:
        lines.append(f"**Sources:** {source_count} files")

    lines.append(f"**Questions:** {meth.get('question_count', 0)}")
    lines.append(f"**Mentions retained:** {total_mentions}")
    lines.append("")

    # Headline as bare text
    headline = es.get("headline", "")
    if headline:
        lines.append(headline)
        lines.append("")

    return "\n".join(lines)


def finding_text(finding):
    """Support legacy prose and findings with code-owned evidence annotations."""
    return finding.get("text", "") if isinstance(finding, dict) else finding


def evidence_annotation_lines(finding):
    """Keep each claim's supporting-source denominator separate and explicit."""
    if not isinstance(finding, dict):
        return []
    return [
        f"Evidence for {a['claim_id']}: {a['supporting_source_files']} of "
        f"{a['question_source_files']} {a['question_id']} source files support "
        f"this claim. {a['claim_text']} Scope: {a['scope']}"
        for a in finding.get("evidence_annotations", [])
    ]


def _build_executive_summary(report):
    """Lead with the conclusion and context, then findings and questions."""
    es = report.get("executive_summary", {})
    lines = []

    # Context (from 'body' field)
    body = es.get("body", "")
    if body:
        lines.append("## Context")
        lines.append("")
        for paragraph in _normalize_newlines(body).split("\n\n"):
            paragraph = paragraph.strip()
            if paragraph:
                lines.append(paragraph)
                lines.append("")

    key_findings = es.get("key_findings")
    if key_findings:
        lines.append("## Key Findings")
        lines.append("")
        for kf in key_findings:
            lines.append(f"- {finding_text(kf)}")
            for annotation in evidence_annotation_lines(kf):
                lines.extend(["", f"  {annotation}"])
        lines.append("")

    # Notable Outcomes (optional callout)
    notable_outcomes = es.get("notable_outcomes")
    if notable_outcomes:
        lines.append("## Notable Outcomes")
        lines.append("")
        for no in notable_outcomes:
            outcome = no.get("outcome", "")
            quote = no.get("quote", "")
            attribution = no.get("attribution", "")
            lines.append(f"- **{outcome}**")
            if quote:
                lines.append(f'  > "{quote}"')
            if attribution:
                lines.append(f"  > — {attribution}")
            lines.append("")

    # Open Questions (from 'key_questions' field)
    key_questions = es.get("key_questions")
    if key_questions:
        lines.append("## Open Questions")
        lines.append("")
        for kq in key_questions:
            lines.append(f"- {kq}")
        lines.append("")

    return "\n".join(lines)


def selected_lead_quote(question, findings_question):
    """Resolve an explicit accepted quote ID, without inventing a fallback.

    Only display quotes for this question are eligible. Ambiguous IDs and
    demoted themes fail closed; validate_report reports the invalid reference.
    Legacy selection, where needed, remains the caller's responsibility.
    """
    mention_id = question.get("lead_quote_id")
    if not mention_id or not findings_question:
        return None
    matches = [quote for theme in findings_question.get("themes", [])
               if not theme.get("demoted") and mention_id in theme.get("mention_ids", [])
               for quote in theme.get("sample_quotes", [])
               if quote.get("mention_id") == mention_id]
    return matches[0] if len(matches) == 1 else None


def _build_question_section(question, findings_question):
    """Build one question section: heading, n= line, table, synthesis, takeaways."""
    qid = question.get("question_id", "")
    text = question.get("question_text", "")

    lines = []
    lines.append(f"## {qid}: {text}")
    lines.append("")

    # n= line (from findings data)
    if findings_question:
        total_m = findings_question.get("total_mentions", 0)
        total_s = findings_question.get("total_source_types", 0)
        if total_s > 1:
            lines.append(f"n = {total_m} mentions from {total_s} sources")
        else:
            lines.append(f"n = {total_m} mentions")
        lines.append("")

        # Sparse data disclaimer
        if findings_question.get("sparse"):
            lines.append(f"> **Note:** Based on {total_m} mentions. Treat as directional signals, not patterns.")
            lines.append("")

        # An explicit lead is editorially accepted upstream, never inferred
        # here from frequency, length, or a notable flag.
        lead = selected_lead_quote(question, findings_question)
        if lead:
            quote = lead.get("display_quote") or lead.get("quote", "")
            lines.append(f'> "{quote}"')
            if lead.get("attribution"):
                lines.append(f'> — {lead["attribution"]}')
            lines.append("")

        # Theme table
        table = _build_theme_table(findings_question)
        if table:
            lines.append(table)
            lines.append("")

    # Synthesis
    synthesis = question.get("synthesis", "")
    if synthesis:
        lines.append("### Synthesis")
        lines.append("")
        lines.append(synthesis)
        lines.append("")

    # Takeaways
    takeaways = question.get("takeaways", [])
    if takeaways:
        lines.append("### Takeaways")
        lines.append("")
        for ta in takeaways:
            lines.append(f"- {ta}")
        lines.append("")

    insights = question.get("additional_insights", [])
    if insights:
        lines.extend(["### Additional Insights", ""])
        for insight in insights:
            lines.extend([f"- {insight['text']}", "", f"  Scope: {insight['scope']}", ""])

    return "\n".join(lines)


def generate_markdown(report, findings, audit_skipped=False):
    """Generate complete Markdown report.

    Args:
        report: Parsed report.json dict.
        findings: Parsed findings.json dict.
        audit_skipped: True if the user chose Skip audit at Phase 2.5; prepends a callout.

    Returns:
        Markdown string.
    """
    # Validate inputs against schemas (warn, don't crash — renderer is robust)
    for label, data, schema in [("report.json", report, "report"), ("findings.json", findings, "findings")]:
        errors = validate_json(data, schema)
        for e in errors[:3]:
            print(f"WARNING: {label} schema issue: {e}", file=sys.stderr)

    # Build findings lookup by question_id
    findings_by_qid = {}
    for q in findings.get("questions", []):
        qid = q.get("question_id")
        if qid:
            findings_by_qid[qid] = q

    parts = []

    # Header
    parts.append(_build_header(report, findings))

    meth = _effective_methodology(report, audit_skipped)
    if meth.get("audit_mode") in {"skip", "sampled"} or meth.get("audit_status") == "incomplete":
        parts.append("> " + " ".join(audit_summary_lines(meth)) + "\n")
    if meth.get("data_quality_notes"):
        parts.append("**Data quality notes:** " + meth["data_quality_notes"] + "\n")

    # Executive summary
    parts.append(_build_executive_summary(report))

    # Separator after exec summary
    parts.append("---")
    parts.append("")

    # Question sections
    questions = report.get("questions", [])
    for i, question in enumerate(questions):
        fq = findings_by_qid.get(question.get("question_id"))
        parts.append(_build_question_section(question, fq))

        # Separator between questions (not after the last one)
        if i < len(questions) - 1:
            parts.append("---")
            parts.append("")

    parts.extend(["---", "", _build_context_methodology({"methodology": meth})])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Context markdown (report-context.md) — condensed LLM context document
# ---------------------------------------------------------------------------


def _build_yaml_front_matter(report, findings):
    """Build YAML front matter block with report metadata."""
    meth = report.get("methodology", {})
    total_mentions = sum(
        q.get("total_mentions", 0) for q in findings.get("questions", [])
    )
    audit_pass_rate = meth.get("audit_pass_rate")
    if meth.get("audit_mode") == "skip" or meth.get("audit_total") == 0:
        audit_pass_rate = None

    lines = ["---"]

    company = meth.get("company_name", "")
    if company:
        # Escape double quotes for valid YAML
        company_escaped = company.replace('"', '\\"')
        lines.append(f"company: \"{company_escaped}\"")

    lines.append(f"date: {datetime.date.today().isoformat()}")
    lines.append(f"source_count: {meth.get('source_count', 0)}")
    lines.append(f"mention_count: {total_mentions}")
    lines.append(f"question_count: {meth.get('question_count', 0)}")
    lines.append(f"audit_pass_rate: {audit_pass_rate if audit_pass_rate is not None else 'null'}")
    if meth.get("audit_mode"):
        lines.append(f"audit_mode: {meth['audit_mode']}")
    if meth.get("audit_status"):
        lines.append(f"audit_status: {meth['audit_status']}")
    lines.append("---")
    return "\n".join(lines)


def _build_context_theme_table(findings_question):
    """Build a compact theme table for the context document.

    Columns: Theme | Mentions | % | Sources
    No description or quotes — those are rendered separately.
    Filters demoted themes. Sorts by mention_count desc, 'other' last.
    """
    if not findings_question:
        return ""

    themes = findings_question.get("themes", [])
    themes = [t for t in themes if not t.get("demoted")]
    _inject_other_for_outliers(themes, findings_question)
    if not themes:
        return ""

    themes = sorted(
        themes,
        key=lambda t: (t.get("type") == "other", -t.get("mention_count", 0)),
    )

    total_source_types = findings_question.get("total_source_types", 0)

    lines = []
    lines.append("| Theme | Mentions | % | Sources |")
    lines.append("| --- | --- | --- | --- |")

    for theme in themes:
        label = theme.get("label", "")
        m_count = theme.get("mention_count", 0)
        m_pct = theme.get("mention_pct", 0)
        s_count = theme.get("source_type_count", 0)

        row = (
            f"| {_escape_cell(label)} "
            f"| {m_count} "
            f"| {m_pct}% "
            f"| {s_count}/{total_source_types} |"
        )
        lines.append(row)

    return "\n".join(lines)


def _build_context_methodology(report):
    """Build methodology as structured data for LLM consumption."""
    meth = report.get("methodology", {})
    lines = []
    lines.append("## Methodology")
    lines.append("")

    lines.append(f"- **Sources analyzed:** {meth.get('source_count', 0)}")
    lines.append(f"- **Sources excluded:** {meth.get('excluded_count', 0)}")

    source_types = meth.get("source_types", {})
    if source_types:
        sorted_types = sorted(source_types.items(), key=lambda x: -x[1])
        type_str = ", ".join(f"{count} {name.lower()}" for name, count in sorted_types)
        lines.append(f"- **Source types:** {type_str}")

    lines.append(f"- **Questions:** {meth.get('question_count', 0)}")
    lines.extend(f"- {line}" for line in audit_summary_lines(meth))

    confidence = meth.get("confidence_model", "")
    if confidence:
        lines.append(f"- **Confidence model:** {confidence}")

    dq_notes = meth.get("data_quality_notes", "")
    if dq_notes:
        lines.append(f"- **Data quality notes:** {dq_notes}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compressed context validation
# ---------------------------------------------------------------------------


def _normalize_quote(text):
    """Normalize quote text for comparison (handle smart quotes, whitespace)."""
    if not text:
        return ""
    # Smart quotes → straight quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def validate_compressed_context(compressed, report, findings):
    """Validate compressed context against source report and findings.

    Returns list of issues, each with 'severity' ('error' or 'warning'),
    'check', and 'message' keys.
    """
    issues = []

    # --- Schema validation ---
    schema_errors = validate_json(compressed, "compressed_context")
    for e in schema_errors:
        issues.append({
            "severity": "error",
            "check": "schema",
            "message": f"Schema violation: {e}",
        })
    if schema_errors:
        return issues  # Can't validate further if schema is broken

    # --- Passthrough integrity ---
    report_es = report.get("executive_summary", {})
    compressed_es = compressed.get("executive_summary", {})

    if compressed_es.get("headline", "") != report_es.get("headline", ""):
        issues.append({
            "severity": "error",
            "check": "passthrough",
            "message": "headline does not match report.json verbatim",
        })

    report_kf = report_es.get("key_findings", [])
    compressed_kf = compressed_es.get("key_findings", [])
    if report_kf != compressed_kf:
        issues.append({
            "severity": "error",
            "check": "passthrough",
            "message": "key_findings do not match report.json verbatim",
        })

    report_kq = report_es.get("key_questions", [])
    compressed_kq = compressed_es.get("key_questions", [])
    if report_kq != compressed_kq:
        issues.append({
            "severity": "error",
            "check": "passthrough",
            "message": "key_questions do not match report.json verbatim",
        })

    report_no = report_es.get("notable_outcomes", [])
    compressed_no = compressed_es.get("notable_outcomes", [])
    if report_no != compressed_no:
        issues.append({
            "severity": "error",
            "check": "passthrough",
            "message": "notable_outcomes do not match report.json verbatim",
        })

    # --- Question coverage ---
    report_qids = {q.get("question_id") for q in report.get("questions", [])}
    compressed_qids = {q.get("question_id") for q in compressed.get("questions", [])}
    missing = report_qids - compressed_qids
    if missing:
        issues.append({
            "severity": "error",
            "check": "question_coverage",
            "message": f"Missing questions: {sorted(missing)}",
        })

    # --- Build findings quote index for traceability ---
    all_findings_quotes = set()
    for fq in findings.get("questions", []):
        for theme in fq.get("themes", []):
            for sq in theme.get("sample_quotes", []):
                all_findings_quotes.add(_normalize_quote(sq.get("quote", "")))

    # --- Per-question checks ---
    for cq in compressed.get("questions", []):
        qid = cq.get("question_id", "?")

        # Synthesis word count
        synthesis = cq.get("synthesis_compressed", "")
        word_count = len(synthesis.split()) if synthesis.strip() else 0
        if word_count < 60:
            issues.append({
                "severity": "warning",
                "check": "word_count",
                "message": f"{qid} synthesis_compressed is {word_count} words (target 60-100)",
            })
        elif word_count > 100:
            issues.append({
                "severity": "warning",
                "check": "word_count",
                "message": f"{qid} synthesis_compressed is {word_count} words (target 60-100)",
            })

        # Quote count
        quotes = cq.get("selected_quotes", [])
        if len(quotes) < 2:
            issues.append({
                "severity": "warning",
                "check": "quote_count",
                "message": f"{qid} has {len(quotes)} selected_quotes (target 2-3)",
            })
        elif len(quotes) > 3:
            issues.append({
                "severity": "warning",
                "check": "quote_count",
                "message": f"{qid} has {len(quotes)} selected_quotes (target 2-3)",
            })

        # Quote traceability
        for sq in quotes:
            normalized = _normalize_quote(sq.get("quote", ""))
            if normalized and normalized not in all_findings_quotes:
                issues.append({
                    "severity": "error",
                    "check": "quote_traceability",
                    "message": (
                        f"{qid} quote not found in findings: "
                        f"\"{sq.get('quote', '')[:80]}...\""
                    ),
                })

    # --- Exec summary body word count ---
    body = compressed_es.get("body_compressed", "")
    body_wc = len(body.split()) if body.strip() else 0
    if body_wc < 200:
        issues.append({
            "severity": "warning",
            "check": "word_count",
            "message": f"body_compressed is {body_wc} words (target 200-350)",
        })
    elif body_wc > 350:
        issues.append({
            "severity": "warning",
            "check": "word_count",
            "message": f"body_compressed is {body_wc} words (target 200-350)",
        })

    return issues


# ---------------------------------------------------------------------------
# Compressed context markdown formatter
# ---------------------------------------------------------------------------


def format_context_md(compressed, report, findings):
    """Format compressed agent output into context Markdown.

    Takes the structured JSON from the compress_context agent and renders
    it as a markdown document with YAML front matter, compressed executive
    summary, per-question compressed synthesis with theme tables and
    selected quotes, and methodology.

    Args:
        compressed: Parsed compressed_context.json from the agent.
        report: Parsed report.json (for methodology and front matter).
        findings: Parsed findings.json (for theme tables).

    Returns:
        Markdown string.
    """
    findings_by_qid = {}
    for q in findings.get("questions", []):
        qid = q.get("question_id")
        if qid:
            findings_by_qid[qid] = q

    parts = []

    # YAML front matter
    parts.append(_build_yaml_front_matter(report, findings))
    parts.append("")

    # Title
    parts.append("# Customer Feedback Analysis")
    parts.append("")

    # Executive summary
    es = compressed.get("executive_summary", {})

    headline = es.get("headline", "")
    if headline:
        parts.append(headline)
        parts.append("")

    key_findings = es.get("key_findings", [])
    if key_findings:
        parts.append("## Key Findings")
        parts.append("")
        for kf in key_findings:
            parts.append(f"- {finding_text(kf)}")
            for annotation in evidence_annotation_lines(kf):
                parts.extend(["", f"  {annotation}"])
        parts.append("")

    notable_outcomes = es.get("notable_outcomes", [])
    if notable_outcomes:
        parts.append("## Notable Outcomes")
        parts.append("")
        for no in notable_outcomes:
            outcome = no.get("outcome", "")
            quote = no.get("quote", "")
            attribution = no.get("attribution", "")
            parts.append(f"- **{outcome}**")
            if quote:
                parts.append(f'  > "{quote}"')
            if attribution:
                parts.append(f"  > — {attribution}")
            parts.append("")

    body = es.get("body_compressed", "")
    if body:
        parts.append("## Summary")
        parts.append("")
        for para in _normalize_newlines(body).split("\n\n"):
            para = para.strip()
            if para:
                parts.append(para)
                parts.append("")

    key_questions = es.get("key_questions", [])
    if key_questions:
        parts.append("## Open Questions")
        parts.append("")
        for kq in key_questions:
            parts.append(f"- {kq}")
        parts.append("")

    # Question sections
    for cq in compressed.get("questions", []):
        qid = cq.get("question_id", "")
        text = cq.get("question_text", "")

        parts.append(f"## {qid}: {text}")
        parts.append("")

        synthesis = cq.get("synthesis_compressed", "")
        if synthesis:
            parts.append(synthesis)
            parts.append("")

        # Theme table from findings (compact, same as before)
        fq = findings_by_qid.get(qid)
        if fq:
            table = _build_context_theme_table(fq)
            if table:
                parts.append(table)
                parts.append("")

        # Selected quotes (agent-curated, not full dump)
        selected = cq.get("selected_quotes", [])
        if selected:
            parts.append("### Quotes")
            parts.append("")
            for sq in selected:
                quote_text = sq.get("quote", "")
                attribution = sq.get("attribution", "")
                file_id = sq.get("file_id", "")
                theme = sq.get("theme_label", "")

                attr_parts = []
                if attribution:
                    attr_parts.append(attribution)
                if file_id:
                    attr_parts.append(file_id)
                attr_str = f" — {', '.join(attr_parts)}" if attr_parts else ""
                theme_str = f" [{theme}]" if theme else ""

                parts.append(f"> \"{quote_text}\"{attr_str}{theme_str}")
                parts.append("")

    # Methodology
    parts.append(_build_context_methodology(report))

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Generate Markdown report")
    parser.add_argument("report", help="Path to report.json")
    parser.add_argument("findings", help="Path to findings.json")
    parser.add_argument("-o", "--output", help="Output Markdown file (default: stdout)")
    parser.add_argument(
        "--context", metavar="COMPRESSED_JSON",
        help="Format compressed context doc from agent output JSON file",
    )
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    with open(args.findings, encoding="utf-8") as f:
        findings = json.load(f)

    # Detect audit-skipped flag relative to report.json's location.
    report_path = Path(args.report).resolve()
    audit_flag = report_path.parent.parent / "audit" / "audit_skipped.flag"
    audit_skipped = audit_flag.exists()

    if args.context:
        with open(args.context, encoding="utf-8") as f:
            compressed = json.load(f)
        context_report = {**report, "methodology": _effective_methodology(report, audit_skipped)}
        result = format_context_md(compressed, context_report, findings)
    else:
        result = generate_markdown(report, findings, audit_skipped=audit_skipped)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()

"""Generate HTML report from report.json + findings.json.

Loads the HTML template, builds sections, substitutes markers.
Pure Python — no Jinja2, no new dependencies.

Usage: python -m v2.scripts.generate_html report.json findings.json [-o output.html]
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

from skill.schemas.validate import validate_json
from skill.scripts.generate_markdown import (
    audit_summary_lines, _effective_methodology, selected_lead_quote,
    finding_text, evidence_annotation_lines,
)


TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "report.html"

# Map theme type → CSS class for chart bar colors
TYPE_TO_CSS_CLASS = {
    "complaint": "negative",
    "risk": "negative",
    "praise": "positive",
    "feature_request": "neutral",
    "competitive": "neutral",
    "behavioral": "neutral",
    "other": "none",
}

# Dropdown arrow SVG icon (reused in outliers + methodology)
_DROPDOWN_ARROW = (
    '<svg class="dropdown-arrow" aria-hidden="true" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>'
)


def _normalize_newlines(text):
    """Normalize double-escaped newlines from LLM JSON output."""
    if not text:
        return ""
    text = text.replace("\\n\\n", "\n\n")
    text = text.replace("\\n", "\n")
    return text


def _escape(text):
    """HTML-escape user data."""
    return html.escape(str(text))


def _humanize_file_id(file_id: str) -> str:
    """Convert raw file_id like '01___SALES_CALL___CLARITASK' to 'Sales Call Claritask'.

    Strips leading numeric prefixes, replaces underscores with spaces,
    collapses multiple separators, and title-cases the result.
    """
    import re as _re
    # Strip leading digits + separators (e.g., "01___" or "01_")
    result = _re.sub(r"^\d+[_]+", "", file_id)
    # Replace underscores with spaces, collapse multiples
    result = _re.sub(r"_+", " ", result).strip()
    # Title case
    return result.title() if result else file_id


def _display_quote(sq):
    """Return display_quote if available, else fall back to quote."""
    return sq.get("display_quote") or sq.get("quote", "")


def _render_quote(text):
    """Convert markdown bold/italic in quote text to HTML for display.

    Scope is strictly limited to bold and italic — no other markdown.
    HTML-escapes first, then converts markdown patterns so that generated
    tags are not escaped.
    """
    result = _escape(text)
    # Bold+italic: ***text*** → <strong><em>text</em></strong>
    result = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: f"<strong><em>{m.group(1)}</em></strong>", result)
    # Bold: **text** → <strong>text</strong>
    result = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<strong>{m.group(1)}</strong>", result)
    # Italic: *text* → <em>text</em>
    result = re.sub(r"\*(.+?)\*", lambda m: f"<em>{m.group(1)}</em>", result)
    return result


def _select_pull_quote(findings_question):
    """Select the best pull-quote from a question's findings.

    Prefer non-"other" themes — "other" themes collect noise and produce
    poor featured quotes. Within the preferred set, pick the highest-mention
    theme and its first notable=True sample_quote (fallback: first quote).

    Falls through to "other" themes only if no non-"other" theme has quotes.

    Returns (quote, attribution, file_id) or None.
    """
    themes = findings_question.get("themes", [])
    active = [t for t in themes if not t.get("demoted")]
    if not active:
        return None

    # Prefer non-"other" themes; fall through to "other" only if no
    # non-"other" theme produces a quote
    non_other = [t for t in active if t.get("type") != "other"]
    other_only = [t for t in active if t.get("type") == "other"]

    for candidate_set in (non_other, other_only):
        candidate_set.sort(key=lambda t: -t.get("mention_count", 0))
        for theme in candidate_set:
            quotes = theme.get("sample_quotes", [])
            if not quotes:
                continue
            for q in quotes:
                if q.get("notable"):
                    attr = q.get("attribution", "") or _humanize_file_id(q.get("file_id", ""))
                    return (_display_quote(q), attr, q.get("file_id", ""))
            attr = quotes[0].get("attribution", "") or _humanize_file_id(quotes[0].get("file_id", ""))
            return (_display_quote(quotes[0]), attr, quotes[0].get("file_id", ""))

    return None


def _format_audit_pass_rate(meth):
    """Format audit pass rate for display.

    Uses audit_total/audit_passed counts when available, falls back to
    displaying the decimal rate as a percentage.
    """
    audit_total = meth.get("audit_total")
    audit_passed = meth.get("audit_passed")
    rate = meth.get("audit_pass_rate")

    if meth.get("audit_mode") == "skip" or audit_total == 0:
        return ""

    if audit_total is not None and audit_passed is not None and audit_total > 0:
        pct = int(round(audit_passed / audit_total * 100))
        return f"{pct}% ({audit_passed}/{audit_total} passed)"

    if rate is not None:
        return f"{int(round(rate * 100))}%"

    return ""


# ---------------------------------------------------------------------------
# New builder functions for redesigned report
# ---------------------------------------------------------------------------


def _build_hero(report):
    """Build typographic hero section with company badge."""
    meth = report.get("methodology", {})
    company = _escape(meth.get("company_name", ""))
    badge = f'<div class="badge">{company}</div>' if company else ""

    return (
        '<section class="hero">\n'
        '  <div class="report-section">\n'
        f"    {badge}\n"
        "    <h1>Customer <br> Feedback <br> Analysis</h1>\n"
        "  </div>\n"
        "</section>"
    )


def _build_sidebar_nav(report):
    """Build sidebar navigation with section links and question sub-links."""
    parts = [
        '<div class="sidebar-nav-header">',
        '  <h3>Navigation</h3>',
        '  <p>Report Sections</p>',
        '</div>',
        '<nav id="sidebar-nav" aria-label="Report sections">',
        '  <a href="#executive-summary" class="nav-link active">Executive Summary</a>',
        '  <a href="#questions" class="nav-link">Questions</a>',
    ]

    for question in report.get("questions", []):
        qid = question["question_id"]
        text = _escape(question.get("question_text", qid))
        parts.append(f'  <a href="#{qid}" class="nav-sub-link">{text}</a>')

    parts.append('  <a href="#methodology" class="nav-link">Methodology</a>')
    parts.append("</nav>")
    return "\n".join(parts)


def _build_executive_summary(report):
    """Build executive summary with two-column findings grid."""
    es = report.get("executive_summary", {})
    parts = [
        '<section id="executive-summary" class="exec-summary">',
        '  <div class="report-section" style="padding-top: 24px;">',
        '    <h2 class="section-title" style="border-bottom: none; '
        'margin-bottom: 32px; padding-bottom: 0;">EXECUTIVE SUMMARY</h2>',
    ]

    # Headline
    headline = es.get("headline", "")
    if headline:
        parts.append(f'    <div class="headline-text">{_escape(headline)}</div>')

    # Body paragraphs
    body_text = es.get("body", "")
    if body_text:
        parts.append('    <div style="margin-top: 64px;">')
        for paragraph in _normalize_newlines(body_text).split("\n\n"):
            paragraph = paragraph.strip()
            if paragraph:
                parts.append(
                    f'      <p style="font-size: 16px; color: var(--slate-600); '
                    f'max-width: 1000px; line-height: 1.7; font-weight: 400; '
                    f'margin-bottom: 1rem;">{_escape(paragraph)}</p>'
                )
        parts.append("    </div>")

    key_findings = es.get("key_findings", [])
    key_questions = es.get("key_questions", [])

    if key_findings or key_questions:
        parts.append('    <div class="findings-grid">')

        if key_findings:
            parts.append('      <div class="finding-list key">')
            parts.append('        <h3>')
            parts.append(
                '          <span style="width: 10px; height: 10px; '
                "background: var(--primary); border-radius: 50%; "
                'display: inline-block;"></span>'
            )
            parts.append("          Key Findings")
            parts.append("        </h3>")
            for kf in key_findings:
                parts.append(f'        <div class="finding-item"><p>{_escape(finding_text(kf))}</p>')
                for annotation in evidence_annotation_lines(kf):
                    parts.append(f'          <p class="evidence-annotation">{_escape(annotation)}</p>')
                parts.append('        </div>')
            parts.append("      </div>")

        if key_questions:
            parts.append('      <div class="finding-list critical">')
            parts.append('        <h3>')
            parts.append(
                '          <span style="font-size: 14px; font-weight: 900; '
                'color: var(--primary);">?</span>'
            )
            parts.append("          Critical Questions")
            parts.append("        </h3>")
            for kq in key_questions:
                parts.append(f'        <div class="finding-item"><p>{_escape(kq)}</p></div>')
            parts.append("      </div>")

        parts.append("    </div>")

    # Notable outcomes callout box
    notable_outcomes = es.get("notable_outcomes", [])
    if notable_outcomes:
        parts.append(
            '    <div class="notable-outcomes" style="margin-top: 48px; '
            "padding: 32px; border-left: 4px solid var(--primary); "
            'background: rgba(59, 130, 246, 0.04); border-radius: 0 8px 8px 0;">'
        )
        parts.append(
            '      <h3 style="font-size: 11px; font-weight: 700; '
            "text-transform: uppercase; letter-spacing: 0.25em; "
            "margin-bottom: 24px; display: flex; align-items: center; "
            'gap: 10px; color: var(--primary);">'
        )
        parts.append(
            '        <span style="font-size: 14px;">&#9733;</span>'
        )
        parts.append("        Notable Outcomes")
        parts.append("      </h3>")
        for no in notable_outcomes:
            outcome = _escape(no.get("outcome", ""))
            quote = _escape(no.get("quote", ""))
            attribution = _escape(no.get("attribution", ""))
            parts.append(
                '      <div style="margin-bottom: 24px; '
                'padding-left: 24px; border-left: 2px solid var(--slate-900);">'
            )
            parts.append(
                f'        <p style="font-size: 15px; font-weight: 500; '
                f'color: #1e293b; line-height: 1.6; '
                f'margin-bottom: 8px;">{outcome}</p>'
            )
            if quote:
                parts.append(
                    f'        <p style="font-size: 14px; font-style: italic; '
                    f'color: var(--slate-600); line-height: 1.6; '
                    f'margin-bottom: 4px;">&ldquo;{quote}&rdquo;</p>'
                )
            if attribution:
                parts.append(
                    f'        <p style="font-size: 13px; color: var(--slate-500); '
                    f'font-weight: 400;">&mdash; {attribution}</p>'
                )
            parts.append("      </div>")
        parts.append("    </div>")

    parts.append("  </div>")
    parts.append("</section>")
    return "\n".join(parts)


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


def _build_bar_chart_css(findings_question):
    """Build CSS grid-based horizontal bar chart for one question."""
    themes = findings_question.get("themes", [])
    if not themes:
        return ""

    # Sort: descending by mention_count, "other" type always last
    themes = sorted(
        themes,
        key=lambda t: (t.get("type") == "other", -t.get("mention_count", 0)),
    )

    # Filter demoted
    themes = [t for t in themes if not t.get("demoted")]
    _inject_other_for_outliers(themes, findings_question)
    if not themes:
        return ""

    total_mentions = findings_question.get("total_mentions", 0)
    max_pct = max(t.get("mention_pct", 0) for t in themes) if themes else 1

    parts = ['<div class="chart-container">']
    parts.append(
        f'<h4 class="chart-title">Theme Frequency (Normalized %) '
        f'<span style="font-size: 10px; color: var(--slate-400); font-weight: 400; '
        f'margin-left: 8px; text-transform: none; letter-spacing: normal;">'
        f"n={total_mentions}</span></h4>"
    )

    # Collect which sentiment classes are used for legend
    sentiments_present = set()

    for theme in themes:
        label = _escape(theme.get("label", ""))
        pct = theme.get("mention_pct", 0)
        css_class = TYPE_TO_CSS_CLASS.get(theme.get("type", ""), "none")
        sentiments_present.add(css_class)

        # Scale bar width relative to max percentage
        bar_width = round(pct / max_pct * 100) if max_pct > 0 else 0

        parts.append('  <div class="chart-row">')
        parts.append(f'    <div class="chart-label">{label}</div>')
        parts.append(
            f'    <div class="chart-bar-bg">'
            f'<div class="chart-bar-fill {css_class}" style="width: {bar_width}%;"></div></div>'
        )
        parts.append(f'    <div class="chart-value">{pct}%</div>')
        parts.append("  </div>")

    # Legend
    legend_order = ["positive", "negative", "neutral", "none"]
    legend_labels = {
        "positive": "Positive",
        "negative": "Negative",
        "neutral": "Neutral",
        "none": "Other",
    }
    parts.append('  <div class="chart-legend">')
    for cls in legend_order:
        if cls in sentiments_present:
            parts.append(
                f'    <div class="legend-item">'
                f'<span class="legend-dot {cls}"></span>{legend_labels[cls]}</div>'
            )
    parts.append("  </div>")
    parts.append("</div>")
    return "\n".join(parts)


def _build_theme_detail_table(findings_question):
    """Build HTML detail table for themes in a question.

    Columns: Theme | Description | Mentions | % | Sources | Key Quotes.
    Demoted themes are filtered out.
    """
    themes = findings_question.get("themes", [])
    themes = [t for t in themes if not t.get("demoted")]
    _inject_other_for_outliers(themes, findings_question)
    if not themes:
        return ""

    # Sort by mention_pct descending, "other" type always last
    themes = sorted(
        themes,
        key=lambda t: (t.get("type") == "other", -t.get("mention_pct", 0)),
    )

    total_mentions = findings_question.get("total_mentions", 0)
    total_source_types = findings_question.get("total_source_types", 0)
    total_segments = findings_question.get("total_segments", 0)
    show_sources = total_source_types > 1
    show_segments = total_segments > 1

    parts = ['<div class="table-container">', "<table>"]
    parts.append("<thead><tr>")
    parts.append("<th>Theme</th><th>Description</th><th>Mentions</th>")
    header_extras = "<th>%</th>"
    if show_sources:
        header_extras += "<th>Sources</th>"
    if show_segments:
        header_extras += "<th>Segments</th>"
    header_extras += "<th>Key Quotes</th>"
    parts.append(header_extras)
    parts.append("</tr></thead>")
    parts.append("<tbody>")

    for theme in themes:
        label = _escape(theme.get("label", ""))
        description = _escape(theme.get("description", ""))
        mention_count = theme.get("mention_count", 0)
        mention_pct = theme.get("mention_pct", 0)
        source_type_count = theme.get("source_type_count", 0)
        breakdown = theme.get("source_type_breakdown", {})

        # Source types: "count/total" with type names below
        source_names = ", ".join(sorted(breakdown.keys())) if breakdown else ""
        sources_html = (
            f'<div style="font-size: 10px; color: var(--slate-500); '
            f'margin-bottom: 4px;">{source_type_count}/{total_source_types}</div>'
            f"{_escape(source_names)}"
        )

        # Key quotes with attribution (up to 2, notable first)
        quote_html = ""
        sample_quotes = theme.get("sample_quotes", [])
        if sample_quotes:
            sorted_quotes = sorted(
                sample_quotes,
                key=lambda q: not q.get("notable", False),
            )
            # Show more quotes for large Other buckets (up to all 5 sample_quotes)
            is_other_type = theme.get("type") == "other"
            max_display = len(sorted_quotes) if (is_other_type and mention_pct > 15) else 2
            quote_parts = []
            for idx, sq in enumerate(sorted_quotes[:max_display]):
                raw_quote = _display_quote(sq)
                if not raw_quote:
                    continue
                attribution = sq.get("attribution", "") or _humanize_file_id(
                    sq.get("file_id", "")
                )
                rendered_quote = _render_quote(raw_quote)
                if len(raw_quote) > 250:
                    summary = _render_quote(raw_quote[:250].rstrip() + "...")
                    block = (
                        f"<details><summary>{summary}</summary>"
                        f"{rendered_quote}</details>"
                    )
                else:
                    block = rendered_quote
                if attribution:
                    block += (
                        f'<div style="font-size: 10px; color: var(--slate-400); '
                        f'margin-top: 4px;">&mdash; {_escape(attribution)}</div>'
                    )
                margin = ' style="margin-top: 8px;"' if quote_parts else ""
                quote_parts.append(f"<div{margin}>{block}</div>")
            quote_html = "\n".join(quote_parts)

        # Segment breakdown cell
        seg_breakdown = theme.get("segment_breakdown", {})
        segment_count = theme.get("segment_count", 0)
        seg_names = ", ".join(sorted(seg_breakdown.keys())) if seg_breakdown else ""
        segments_html = (
            f'<div style="font-size: 10px; color: var(--slate-500); '
            f'margin-bottom: 4px;">{segment_count}/{total_segments}</div>'
            f"{_escape(seg_names)}"
        )

        parts.append("<tr>")
        parts.append(f'<td class="theme-cell">{label}</td>')
        parts.append(f'<td class="desc-cell">{description}</td>')
        parts.append(f"<td>{mention_count}</td>")
        parts.append(f"<td>{mention_pct}%</td>")
        if show_sources:
            parts.append(f"<td>{sources_html}</td>")
        if show_segments:
            parts.append(f"<td>{segments_html}</td>")
        parts.append(f'<td class="quote-cell">{quote_html}</td>')
        parts.append("</tr>")

    parts.append("</tbody>")
    # Footer row — tfoot must be direct child of table, not inside tbody
    trailing_colspan = 2 + (1 if show_sources else 0) + (1 if show_segments else 0)
    parts.append("<tfoot><tr>")
    parts.append('<td colspan="2"></td>')
    parts.append(
        f'<td style="font-weight: 700; color: var(--slate-500); '
        f'padding-top: 12px;">n={total_mentions}</td>'
    )
    parts.append(f'<td colspan="{trailing_colspan}"></td>')
    parts.append("</tr></tfoot>")
    parts.append("</table>")
    parts.append("</div>")  # close table-container
    # Scroll hint button — outside scroll container, toggles direction
    parts.append(
        '<div class="table-scroll-hint">'
        '<button class="scroll-btn" aria-label="Scroll table"'
        '>SCROLL &rarr;</button></div>'
    )
    return "\n".join(parts)


def _build_takeaways_grid(question, findings_question):
    """Build full-width pull quote, then synthesis + takeaways grid below."""
    parts = []

    # Full-width pull quote on top
    if "lead_quote_id" in question:
        selected = selected_lead_quote(question, findings_question)
        result = ((_display_quote(selected), selected.get("attribution", ""),
                   selected.get("file_id", "")) if selected else None)
    else:
        result = _select_pull_quote(findings_question) if findings_question else None
    if result:
        quote, attribution, _fid = result
        if quote:
            parts.append('<div style="margin-bottom: 64px;">')
            parts.append(f'  <div class="quote-text">{_render_quote(quote)}</div>')
            if attribution:
                parts.append(f'  <cite class="quote-cite">&mdash; {_escape(attribution)}</cite>')
            parts.append("</div>")

    # Stacked layout: takeaways first, then synthesis below
    takeaways = question.get("takeaways", [])
    if takeaways:
        parts.append('  <div class="takeaways-box">')
        parts.append("    <h4>Key Takeaways</h4>")
        for ta in takeaways:
            parts.append(f'    <div class="takeaway-item">{_escape(ta)}</div>')
        parts.append("  </div>")

    synthesis = question.get("synthesis", "")
    if synthesis:
        parts.append('  <div class="synthesis-block">')
        for paragraph in _normalize_newlines(synthesis).split("\n\n"):
            paragraph = paragraph.strip()
            if paragraph:
                parts.append(
                    f'    <p style="font-size: 16px; color: var(--slate-600); '
                    f'line-height: 1.7; font-weight: 400; margin-bottom: 1rem;">'
                    f'{_escape(paragraph)}</p>'
                )
        parts.append("  </div>")
    insights = question.get("additional_insights", [])
    if insights:
        parts.append('  <div class="additional-insights"><h4>Additional Insights</h4>')
        for insight in insights:
            parts.append(f'    <div class="additional-insight"><p>{_escape(insight["text"])}</p>')
            parts.append(f'      <p class="insight-scope">Scope: {_escape(insight["scope"])}</p></div>')
        parts.append('  </div>')
    return "\n".join(parts)



def _build_outliers_section(question_data):
    """Build collapsible outliers section with styled cards."""
    if not question_data:
        return ""
    outliers = question_data.get("high_intensity_outliers", [])
    if not outliers:
        return ""

    max_shown = 5
    shown = outliers[:max_shown]
    remaining = len(outliers) - max_shown

    parts = ['<div class="outliers-section">']
    parts.append("  <details>")
    parts.append("    <summary>")
    parts.append(f'      <div class="summary-content">{_DROPDOWN_ARROW} '
                 f"View Outlier Perspectives ({len(outliers)})</div>")
    parts.append("    </summary>")
    parts.append('    <div class="outliers-grid">')

    for i, outlier in enumerate(shown):
        quote_text = _render_quote(_display_quote(outlier))
        # Prefer quote_attribution (customer name), fall back to humanized file_id
        attribution = outlier.get("quote_attribution", "")
        raw_file_id = outlier.get("file_id", "")
        source_label = _escape(attribution if attribution else _humanize_file_id(raw_file_id))

        if i > 0:
            parts.append('      <hr class="outlier-divider">')

        parts.append('      <div class="outlier-card">')
        if quote_text:
            parts.append(f'        <p class="outlier-quote">{quote_text}</p>')
        parts.append(f'        <span class="outlier-source">&mdash; {source_label}</span>')
        parts.append("      </div>")

    if remaining > 0:
        parts.append(
            f'      <p style="font-size: 12px; color: var(--slate-400); '
            f'margin-top: 16px; font-style: italic;">'
            f"(+{remaining} more not shown)</p>"
        )

    parts.append("    </div>")
    parts.append("  </details>")
    parts.append("</div>")
    return "\n".join(parts)


def _build_question_section(question, findings_question, question_index):
    """Build HTML for one question section."""
    qid = question["question_id"]
    text = _escape(question.get("question_text", ""))
    idx_str = str(question_index + 1).zfill(2)

    parts = [f'<section id="{_escape(qid)}" class="question-section">']
    parts.append('  <div class="report-section">')
    parts.append(f'    <div class="question-meta">Analysis: Question {idx_str}</div>')
    parts.append(f'    <h3 class="question-title">{text}</h3>')

    if findings_question:
        # Sparse data disclaimer for low-mention questions
        if findings_question.get("sparse"):
            total_m = findings_question.get("total_mentions", 0)
            parts.append(
                f'<div style="background: var(--surface-container-low); border-left: 3px solid var(--slate-400); '
                f'padding: 12px 16px; margin-bottom: 24px; font-size: 12px; color: var(--slate-500);">'
                f'Based on {total_m} mentions. Treat as directional signals, not patterns.</div>'
            )

        chart = _build_bar_chart_css(findings_question)
        if chart:
            parts.append(chart)

        detail_table = _build_theme_detail_table(findings_question)
        if detail_table:
            parts.append(detail_table)

    # Takeaways grid (synthesis + takeaways + pull quote below)
    takeaways_grid = _build_takeaways_grid(question, findings_question)
    parts.append(takeaways_grid)

    # Outliers
    if findings_question:
        outliers_html = _build_outliers_section(findings_question)
        if outliers_html:
            parts.append(outliers_html)

    parts.append("  </div>")
    parts.append("</section>")
    return "\n".join(parts)


def _build_methodology_section(report):
    """Build collapsible methodology section with stat cards and AI disclaimer."""
    meth = report.get("methodology", {})

    parts = ['<section id="methodology" class="methodology-section">']
    parts.append('  <div class="report-section">')
    if meth.get("data_quality_notes"):
        parts.append('    <div class="data-quality-notes" role="note">')
        parts.append("      <h3>Data quality notes</h3>")
        for paragraph in str(meth["data_quality_notes"]).splitlines():
            if paragraph.strip():
                parts.append(f"      <p>{_escape(paragraph)}</p>")
        parts.append("    </div>")
    parts.append("    <details>")
    parts.append("      <summary>")
    parts.append(f'        <h2 class="section-title">{_DROPDOWN_ARROW} Methodology</h2>')
    parts.append("      </summary>")
    parts.append('      <div class="method-content">')

    # Stat cards
    source_count = meth.get("source_count", 0)
    question_count = meth.get("question_count", 0)
    excluded_count = meth.get("excluded_count", 0)
    audit_str = _format_audit_pass_rate(meth)

    parts.append('        <div class="method-grid">')
    for label, value, sub in [
        ("Data Sources", f"{source_count} Primary Sources",
         "Extracted from customer calls, support tickets, surveys, and other feedback channels."),
        ("Questions Analyzed", str(question_count),
         "Research questions evaluated against all source data."),
        ("Excluded Sources", f"{excluded_count} Source Files",
         "Sources excluded for quality or relevance reasons." if excluded_count
         else "No source files were excluded. Mention exclusions are reported in the audit coverage below."),
        ("Audit Pass Rate", audit_str or "N/A",
         "Agreement within assessed audit evidence; not a guarantee for all findings."),
    ]:
        parts.append('          <div class="method-item">')
        parts.append(f'            <div class="method-label">{label}</div>')
        parts.append(f'            <div class="method-value">{_escape(value)}</div>')
        parts.append(f'            <div class="method-sub">{_escape(sub)}</div>')
        parts.append("          </div>")
    parts.append("        </div>")
    parts.append('        <div class="audit-coverage">')
    for line in audit_summary_lines(meth):
        parts.append(f"          <p>{_escape(line)}</p>")
    parts.append("        </div>")

    # AI Transparency notice
    parts.append('        <div class="ai-notice">')
    parts.append('          <div class="ai-notice-header">')
    parts.append(
        '            <svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>'
    )
    parts.append("            <h3>AI Transparency Notice</h3>")
    parts.append("          </div>")

    parts.append('          <div class="ai-notice-content">')

    # Disclaimer — prominent
    parts.append('            <div class="ai-disclaimer">')
    parts.append(
        "This report is AI-generated. All findings, summaries, and interpretations "
        "should be reviewed for accuracy before being used for decision-making. "
        "While the system includes automated checks, AI analysis can miss context, "
        "misinterpret tone, or over-generalize from limited data."
    )
    parts.append("            </div>")

    # How it works — condensed
    parts.append('            <div class="ai-notice-section">')
    parts.append("              <h4>How this report was produced</h4>")
    parts.append(
        "              <p>This report was produced by an AI-powered analysis pipeline. "
        "Source files were converted to text, classified, and analyzed against research "
        "questions. An AI extracted key points and verbatim quotes, grouped them into "
        "themes, then wrote the narrative sections. All counting and percentages are "
        "computed programmatically, not by the AI.</p>"
    )
    parts.append("            </div>")

    # How it stays honest
    parts.append('            <div class="ai-notice-section">')
    parts.append("              <h4>How it stays honest</h4>")
    parts.append(
        "              <p>The workflow combines these checks, with audit coverage recorded above:</p>"
    )
    parts.append('              <ul style="list-style: disc; padding-left: 20px;">')
    if meth.get("audit_mode") == "skip":
        audit_description = "Semantic attribution and context checks were skipped."
    elif meth.get("audit_mode") == "sampled":
        audit_description = (
            "Selected mentions are checked against source context, with at most one repair pass. "
            "Unselected and unassessed mentions are not labelled audited."
        )
    else:
        audit_description = "Audit mode and coverage depend on the evidence recorded above."
    parts.append(
        '                <li style="margin-bottom: 16px;"><strong>Audit checks:</strong> '
        + audit_description + "</li>"
    )
    parts.append(
        '                <li style="margin-bottom: 16px;">'
        "<strong>Quote verification:</strong> Scripts check source grounding for extracted "
        "quotes. This mechanical check does not establish semantic context.</li>"
    )
    parts.append(
        '                <li style="margin-bottom: 16px;">'
        "<strong>Number verification:</strong> A script traces every statistic in "
        "the prose back to the computed data. If it doesn't match, the report "
        "doesn't ship.</li>"
    )
    parts.append("              </ul>")
    parts.append("            </div>")

    # Footer
    parts.append('            <div class="ai-footer">')
    parts.append(
        "The AI does the reading, thinking, and writing. Scripts do all the "
        "counting and mechanical checks. These checks do not establish overall accuracy."
    )
    parts.append("            </div>")

    parts.append("          </div>")  # ai-notice-content
    parts.append("        </div>")  # ai-notice

    parts.append("      </div>")  # method-content
    parts.append("    </details>")
    parts.append("  </div>")
    parts.append("</section>")
    return "\n".join(parts)


def _build_footer():
    """Build footer with license and contact links."""
    parts = ['<footer>']
    parts.append('  <div class="footer-left">')
    parts.append('    <div class="footer-title">Customer Feedback Report</div>')
    parts.append("    <details>")
    parts.append("      <summary>")
    parts.append('        <div class="footer-copyright-row">')
    parts.append(
        '          <svg class="dropdown-arrow" aria-hidden="true" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" style="width: 10px; height: 10px;">'
        '<polyline points="9 18 15 12 9 6"/></svg>'
    )
    parts.append("          Copyright (c) 2025-present Denny Hollick. All rights reserved.")
    parts.append("        </div>")
    parts.append("      </summary>")
    parts.append('      <div class="license-details">')
    parts.append(
        "Permission is granted, free of charge, to use and distribute this skill and "
        'its associated files or created files ("the Skill"), subject to the following '
        "conditions:\n\n"
        "Attribution. Redistributions must include this LICENSE file and clearly credit "
        '"Denny Hollick" as the original author.\n\n'
        "No Modification. You may adapt the Skill for personal use, but you may not "
        "distribute modified versions. Distribute it as-is or not at all.\n\n"
        "No Monetization. You may not sell or charge fees for the Skill itself. "
        "Using it in the course of paid work is fine.\n\n"
        "Revocable. This license may be revoked at any time at the sole discretion "
        "of the copyright holder.\n\n"
        'No Warranty. Provided "as is" without warranty of any kind.\n\n'
        "Contact: hello@dennyhollick.com"
    )
    parts.append("      </div>")
    parts.append("    </details>")
    parts.append("  </div>")
    parts.append('  <div class="footer-right">')
    parts.append('    <div class="footer-link-item">')
    parts.append(
        '      <a href="https://linkedin.com/in/dennyhollick" target="_blank">'
        "Connect on LinkedIn</a>"
    )
    parts.append("    </div>")
    parts.append('    <div class="footer-link-item">')
    parts.append('      <a href="mailto:hello@dennyhollick.com">Email: hello@dennyhollick.com</a>')
    parts.append("    </div>")
    parts.append("  </div>")
    parts.append("</footer>")
    return "\n".join(parts)


def _build_audit_banner(audit_skipped, methodology=None):
    """Make skipped, sampled, and incomplete audit coverage visible on opening."""
    meth = _effective_methodology({"methodology": methodology or {}}, audit_skipped)
    mode = meth.get("audit_mode")
    if mode not in {"skip", "sampled"} and meth.get("audit_status") != "incomplete":
        return ""
    title = "Audit skipped" if mode == "skip" else "Sampled audit"
    if meth.get("audit_status") == "incomplete":
        title = "Incomplete audit"
    return (
        '<div class="audit-banner" role="note">'
        f'<strong>{title}</strong> '
        + _escape(" ".join(audit_summary_lines(meth)))
        + '</div>'
    )


def generate_html(report, findings, audit_skipped=False):
    """Generate complete HTML report.

    Args:
        report: Parsed report.json dict.
        findings: Parsed findings.json dict.
        audit_skipped: True if user chose Skip audit at Phase 2.5; renders a banner.

    Returns:
        HTML string.
    """
    # Validate inputs against schemas (warn, don't crash — renderer is robust)
    for label, data, schema in [("report.json", report, "report"), ("findings.json", findings, "findings")]:
        errors = validate_json(data, schema)
        for e in errors[:3]:
            print(f"WARNING: {label} schema issue: {e}", file=sys.stderr)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    meth = _effective_methodology(report, audit_skipped)

    # Build findings lookup by question_id
    findings_by_qid = {}
    for q in findings.get("questions", []):
        findings_by_qid[q["question_id"]] = q

    # Build question sections
    question_sections = []
    # Wrap all questions in a parent section for the "Questions" nav link
    question_sections.append('<section id="questions">')
    for i, question in enumerate(report.get("questions", [])):
        fq = findings_by_qid.get(question["question_id"])
        question_sections.append(_build_question_section(question, fq, i))
    question_sections.append("</section>")

    # Substitute markers
    result = template
    result = result.replace("{{TITLE}}", _escape(
        report.get("executive_summary", {}).get("headline") or "Customer Feedback Analysis"
    ))
    result = result.replace("{{SKIP_LINK}}",
        '<a href="#main-content" class="skip-link">Skip to main content</a>')
    result = result.replace("{{SIDEBAR_NAV}}", _build_sidebar_nav(report))
    result = result.replace("{{HERO_SECTION}}", _build_hero(report))
    result = result.replace("{{AUDIT_BANNER}}", _build_audit_banner(audit_skipped, meth))
    result = result.replace("{{EXECUTIVE_SUMMARY}}", _build_executive_summary(report))
    result = result.replace("{{QUESTIONS_SECTIONS}}", "\n".join(question_sections))
    result = result.replace("{{METHODOLOGY_SECTION}}", _build_methodology_section({"methodology": meth}))
    result = result.replace("{{FOOTER}}", _build_footer())

    return result


def main():
    parser = argparse.ArgumentParser(description="Generate HTML report")
    parser.add_argument("report", help="Path to report.json")
    parser.add_argument("findings", help="Path to findings.json")
    parser.add_argument("-o", "--output", help="Output HTML file (default: stdout)")
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    with open(args.findings, encoding="utf-8") as f:
        findings = json.load(f)

    # Detect audit-skipped flag relative to report.json's location.
    # report.json lives at synthesis/output/report.json; flag at synthesis/audit/audit_skipped.flag.
    report_path = Path(args.report).resolve()
    audit_flag = report_path.parent.parent / "audit" / "audit_skipped.flag"
    audit_skipped = audit_flag.exists()

    result = generate_html(report, findings, audit_skipped=audit_skipped)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()

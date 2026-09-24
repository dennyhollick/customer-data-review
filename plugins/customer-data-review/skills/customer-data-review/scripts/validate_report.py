"""Full report validation: schema + semantics + number verification.

Usage: python -m v2.scripts.validate_report report.json plan.json findings.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

from skill.schemas.validate import validate_json
from skill.scripts.generate_markdown import selected_lead_quote, finding_text
from skill.scripts.verify_numbers import verify_numbers
from skill.scripts.verify_report_evidence import _narrative_fields


def _normalize_newlines(text):
    """Normalize double-escaped newlines from LLM JSON output."""
    if not text:
        return ""
    text = text.replace("\\n\\n", "\n\n")
    text = text.replace("\\n", "\n")
    return text


def validate_schema(report):
    """Validate report against report.schema.json.

    Returns list of Issue dicts.
    """
    errors = validate_json(report, "report")
    return [
        {
            "severity": "error",
            "check": "schema",
            "message": error,
            "context": "report.schema.json",
        }
        for error in errors
    ]


def validate_question_coverage(report, plan):
    """Check that every plan question has a synthesis in the report.

    Returns list of Issue dicts.
    """
    plan_qids = {q["id"] for q in plan.get("questions", [])}
    report_qids = {q["question_id"] for q in report.get("questions", [])}

    issues = []
    for qid in sorted(plan_qids - report_qids):
        issues.append({
            "severity": "error",
            "check": "question_coverage",
            "message": f"Plan question {qid} missing from report",
            "context": "report.questions",
        })

    return issues


def validate_field_lengths(report):
    """Check field length constraints.

    - synthesis: 80-220 words (warn)
    - synthesis: max 3 paragraphs (warn)
    - headline: <=120 chars (error)
    """
    issues = []

    headline = report.get("executive_summary", {}).get("headline") or ""
    if len(headline) > 120:
        issues.append({
            "severity": "error",
            "check": "field_length",
            "message": f"Headline is {len(headline)} chars (limit 120)",
            "context": "executive_summary.headline",
        })

    for i, question in enumerate(report.get("questions", [])):
        synthesis = question.get("synthesis") or ""
        word_count = len(synthesis.split()) if synthesis.strip() else 0
        qid = question.get("question_id", i)
        optional_editorial = "lead_quote_id" in question or question.get("synthesis") is None

        if word_count < 80 and not optional_editorial:
            issues.append({
                "severity": "warning",
                "check": "field_length",
                "message": (
                    f"Question {qid} synthesis is {word_count} words "
                    f"(recommended 80-220)"
                ),
                "context": f"questions[{i}].synthesis",
            })
        elif word_count > 220:
            issues.append({
                "severity": "warning",
                "check": "field_length",
                "message": (
                    f"Question {qid} synthesis is {word_count} words "
                    f"(recommended 80-220)"
                ),
                "context": f"questions[{i}].synthesis",
            })

        # Takeaway count check
        takeaways = question.get("takeaways", [])
        num_takeaways = len(takeaways)
        if num_takeaways > 3:
            issues.append({
                "severity": "warning",
                "check": "field_length",
                "message": (
                    f"Question {qid} has {num_takeaways} takeaways "
                    f"(hard max 3)"
                ),
                "context": f"questions[{i}].takeaways",
            })
        elif num_takeaways < 2 and not optional_editorial:
            issues.append({
                "severity": "warning",
                "check": "field_length",
                "message": (
                    f"Question {qid} has {num_takeaways} takeaways "
                    f"(minimum 2)"
                ),
                "context": f"questions[{i}].takeaways",
            })

        # Paragraph count backstop
        paragraphs = [p.strip() for p in _normalize_newlines(synthesis).split("\n\n") if p.strip()]
        if len(paragraphs) < 2 and not optional_editorial:
            issues.append({
                "severity": "warning",
                "check": "field_length",
                "message": (
                    f"Question {qid} synthesis has {len(paragraphs)} "
                    f"paragraph(s) (recommended minimum 2)"
                ),
                "context": f"questions[{i}].synthesis",
            })
        elif len(paragraphs) > 3:
            issues.append({
                "severity": "warning",
                "check": "field_length",
                "message": (
                    f"Question {qid} synthesis has {len(paragraphs)} "
                    f"paragraphs (recommended max 3)"
                ),
                "context": f"questions[{i}].synthesis",
            })

    return issues


def validate_lead_quotes(report, findings):
    """Require an explicit lead to reference exactly one eligible display quote.

    Semantic and source-context acceptance happen before display findings are
    frozen. This boundary catches stale IDs, cross-question IDs and demotions.
    """
    by_question = {q["question_id"]: q for q in findings.get("questions", [])}
    issues = []
    for i, question in enumerate(report.get("questions", [])):
        if question.get("lead_quote_id") is None:
            continue
        selected = selected_lead_quote(question, by_question.get(question.get("question_id")))
        if selected is None or not (selected.get("display_quote") or selected.get("quote")):
            issues.append({
                "severity": "error", "check": "lead_quote_reference",
                "message": "Leading quote must identify one accepted display quote in this question.",
                "context": f"questions[{i}].lead_quote_id",
            })
    return issues


def validate_word_counts(report):
    """Check executive summary word counts (warnings, not errors).

    - Each key_findings bullet must be <=20 words
    - Combined headline + body + key_questions must be 200-600 words
    - Each key_questions item must be <=40 words
    """
    issues = []
    exec_summary = report.get("executive_summary", {})

    # Bullet word count
    for i, bullet in enumerate(exec_summary.get("key_findings", [])):
        word_count = len(finding_text(bullet).split())
        if word_count > 20:
            issues.append({
                "severity": "warning",
                "check": "word_count",
                "message": f"key_findings[{i}] is {word_count} words (limit 20)",
                "context": f"executive_summary.key_findings[{i}]",
            })

    # Combined word count: headline + body + all key_questions
    combined_text = " ".join([
        exec_summary.get("headline") or "",
        exec_summary.get("body") or "",
        *exec_summary.get("key_questions", []),
    ])
    combined_words = len(combined_text.split())
    optional_editorial = (
        any("lead_quote_id" in q for q in report.get("questions", []))
        or exec_summary.get("headline") is None
        or exec_summary.get("body") is None
    )
    if combined_words > 600:
        issues.append({
            "severity": "warning",
            "check": "word_count",
            "message": (
                f"Executive summary combined word count is "
                f"{combined_words} (limit 600)"
            ),
            "context": "executive_summary",
        })
    elif combined_words < 200 and not optional_editorial:
        issues.append({
            "severity": "warning",
            "check": "word_count",
            "message": (
                f"Executive summary combined word count is "
                f"{combined_words} (minimum 200 for narrative depth)"
            ),
            "context": "executive_summary",
        })
    elif combined_words < 300 and not optional_editorial:
        issues.append({
            "severity": "warning",
            "check": "word_count",
            "message": (
                f"Executive summary combined word count is "
                f"{combined_words} (target 300-600 for narrative depth)"
            ),
            "context": "executive_summary",
        })

    # Notable outcomes word count
    for i, no in enumerate(exec_summary.get("notable_outcomes", [])):
        outcome_text = no.get("outcome", "") if isinstance(no, dict) else ""
        outcome_words = len(outcome_text.split())
        if outcome_words > 25:
            issues.append({
                "severity": "warning",
                "check": "word_count",
                "message": (
                    f"notable_outcomes[{i}].outcome is {outcome_words} words "
                    f"(limit 25)"
                ),
                "context": f"executive_summary.notable_outcomes[{i}].outcome",
            })

    # Key questions word count
    for i, kq in enumerate(exec_summary.get("key_questions", [])):
        kq_words = len(kq.split())
        if kq_words > 40:
            issues.append({
                "severity": "warning",
                "check": "word_count",
                "message": (
                    f"Key question {i + 1} is {kq_words} words "
                    f"(limit 40)"
                ),
                "context": f"executive_summary.key_questions[{i}]",
            })

    return issues


def _check_escaped_newlines(report):
    """Flag literal \\n sequences in string fields."""
    issues = []

    def _scan(text, path):
        if isinstance(text, str) and "\\n" in text:
            issues.append({
                "severity": "warning",
                "check": "escaped_newlines",
                "message": f"{path} contains literal '\\n' sequences — may render incorrectly",
            })

    for path, text, _qid in _narrative_fields(report):
        _scan(text, path)

    return issues


def _source_observation_number_view(report, packets, inventory):
    """Exclude only exact, provenance-checked source text from corpus arithmetic.

    Scope and free executive prose remain checked. No report-side flag or
    matching numeric literal grants an exemption.
    """
    import copy
    from skill.scripts.claim_quantities import assess_claim_quantities
    from skill.scripts.section_evidence import report_questions, section_presentation
    if packets is None or inventory is None:
        return report
    try:
        expected = {q["question_id"]: q for q in report_questions(packets, inventory)}
        questions = {q["question_id"]: q for q in inventory["questions"]}
        packet_map = {q["question_id"]: q for q in packets["questions"]}
        exact = {}
        for q in packets["questions"]:
            for claim in q["claims"]:
                if "source_observation" not in claim:
                    continue
                assessment = assess_claim_quantities(claim, questions[q["question_id"]])
                if not assessment["hard_errors"] and not assessment["editorial_errors"]:
                    exact[claim["claim_id"]] = claim
        result = copy.deepcopy(report)
        for q in result.get("questions", []):
            qid = q["question_id"]
            presentation = section_presentation(packet_map[qid])
            cid = presentation["summary_claim_id"]
            if cid in exact and q.get("synthesis") == expected[qid]["synthesis"]:
                q["synthesis"] = exact[cid]["scope"]
            for field, ids in (("takeaways", presentation["takeaway_claim_ids"]),
                               ("additional_insights", presentation["additional_claim_ids"])):
                for i, cid in enumerate(ids):
                    if (cid in exact and i < len(q.get(field, []))
                            and q[field][i] == expected[qid][field][i]):
                        if field == "takeaways":
                            q[field][i] = exact[cid]["scope"]
                        else:
                            q[field][i]["text"] = ""
        from skill.scripts.batch_report import executive_annotations
        for finding in result.get("executive_summary", {}).get("key_findings", []):
            if not isinstance(finding, dict):
                continue
            for annotation in finding.get("evidence_annotations", []):
                cid = annotation["claim_id"]
                if cid in exact and annotation == executive_annotations([cid], packets, inventory)[0]:
                    annotation["claim_text"] = ""
        return result
    except (ValueError, KeyError, TypeError, IndexError):
        # The projection validator reports corruption. It cannot authorize a skip.
        return report


def validate_numbers(report, findings, statistics=None, *, packets=None, inventory=None):
    """Delegate to verify_numbers for number verification.

    Returns list of Issue dicts.
    """
    return verify_numbers(_source_observation_number_view(report, packets, inventory), findings, statistics)


def validate_claim_projections(report, packets=None, inventory=None):
    """Check new display fields against locked, accepted claim projections.

    Annotation integers are compared as complete code-owned objects, never
    accepted merely because their values occur somewhere in the report.
    """
    from skill.scripts.claim_quantities import assess_claim_quantities
    from skill.scripts.section_evidence import report_questions, section_presentation

    issues = []

    def issue(path, message):
        issues.append({"severity": "error", "check": "claim_projection",
                       "context": path, "message": message})

    findings = report.get("executive_summary", {}).get("key_findings", [])
    from skill.scripts.batch_report import _qualitative_quantity_errors
    for i, finding in enumerate(findings):
        if isinstance(finding, dict):
            for message in _qualitative_quantity_errors(finding["text"]):
                issue(f"executive_summary.key_findings[{i}].text", message)
    has_new_content = (any(q.get("additional_insights") for q in report.get("questions", []))
                       or any(isinstance(f, dict) and f.get("evidence_annotations") for f in findings))
    if packets is None or inventory is None:
        if has_new_content:
            issue("report", "Additional insights and evidence annotations require locked section packets and inventory.")
        return issues
    try:
        expected = {q["question_id"]: q for q in report_questions(packets, inventory)}
        packet_map = {q["question_id"]: q for q in packets["questions"]}
        question_map = {q["question_id"]: q for q in inventory["questions"]}
        claims = {c["claim_id"]: (c, q["question_id"])
                  for q in packets["questions"] for c in q["claims"]}
        for i, question in enumerate(report.get("questions", [])):
            qid = question["question_id"]
            if qid not in expected:
                issue(f"questions[{i}]", "Question is absent from the accepted section packets.")
                continue
            for field in ("synthesis", "takeaways", "additional_insights"):
                actual = question.get(field, [] if field != "synthesis" else None)
                if actual != expected[qid][field]:
                    issue(f"questions[{i}].{field}", "Display prose must exactly match the accepted claim projection.")
            for cid in section_presentation(packet_map[qid])["additional_claim_ids"]:
                claim = claims[cid][0]
                assessment = assess_claim_quantities(claim, question_map[qid])
                for message in assessment["hard_errors"] + assessment["editorial_errors"]:
                    issue(f"questions[{i}].additional_insights", f"{cid}: {message}")

        # Local import avoids the rendering/admission import cycle.
        from skill.scripts.batch_report import executive_annotations
        for i, finding in enumerate(findings):
            if not isinstance(finding, dict):
                continue
            annotations = finding.get("evidence_annotations", [])
            ids = [a["claim_id"] for a in annotations]
            if len(ids) != len(set(ids)):
                issue(f"executive_summary.key_findings[{i}]", "Evidence annotation claims must be distinct.")
                continue
            expected_annotations = executive_annotations(ids, packets, inventory)
            if annotations != expected_annotations:
                issue(f"executive_summary.key_findings[{i}]", "Evidence annotations must match exact accepted claim text, scope, question, and source counts.")
            for annotation in annotations:
                claim, qid = claims[annotation["claim_id"]]
                assessment = assess_claim_quantities(claim, question_map[qid])
                for message in assessment["hard_errors"] + assessment["editorial_errors"]:
                    issue(f"executive_summary.key_findings[{i}].evidence_annotations", message)
    except (ValueError, KeyError, TypeError) as exc:
        issue("report", f"Invalid accepted evidence projection: {exc}")
    return issues


def validate_report(report, plan, findings, statistics=None, *, packets=None, inventory=None):
    """Run all validation checks.

    Returns list of Issue dicts.
    """
    issues = []
    issues.extend(validate_schema(report))
    if issues:
        return issues
    issues.extend(validate_question_coverage(report, plan))
    issues.extend(validate_lead_quotes(report, findings))
    issues.extend(validate_field_lengths(report))
    issues.extend(validate_word_counts(report))
    issues.extend(validate_numbers(report, findings, statistics, packets=packets, inventory=inventory))
    issues.extend(validate_claim_projections(report, packets, inventory))
    issues.extend(_check_escaped_newlines(report))
    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate report.json")
    parser.add_argument("report", help="Path to report.json")
    parser.add_argument("plan", help="Path to plan.json")
    parser.add_argument("findings", help="Path to findings.json")
    parser.add_argument("--packets", help="Accepted section-packets.json (defaults to report sibling when present)")
    parser.add_argument("--inventory", help="Locked section-inventory.json (defaults to report sibling when present)")
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    with open(args.findings, encoding="utf-8") as f:
        findings = json.load(f)

    def optional_evidence(value, name):
        path = Path(value) if value else Path(args.report).parent / name
        return json.loads(path.read_text()) if value or path.exists() else None

    packets = optional_evidence(args.packets, "section-packets.json")
    inventory = optional_evidence(args.inventory, "section-inventory.json")
    statistics = ({q["question_id"]: [s for c in q["claims"] for s in c.get("claim_stats", [])]
                   for q in packets["questions"]} if packets else None)
    issues = validate_report(report, plan, findings, statistics, packets=packets, inventory=inventory)
    for issue in issues:
        print(f"[{issue['severity'].upper()}] {issue['message']}")
    sys.exit(1 if any(i["severity"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()

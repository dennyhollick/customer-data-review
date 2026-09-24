"""Render at most five opportunities using retained quotes and computed counts."""

import argparse
import html
import os
import re
import tempfile
from pathlib import Path

from skill.schemas.validate import validate_json
from skill.scripts.assemble_methodology import assemble_methodology
from skill.scripts.bounded_audit import read_json, read_records, verify_gate
from skill.scripts.compute_findings import compute_findings
from skill.scripts.generate_markdown import audit_summary_lines
from skill.scripts.validate_assignments import validate_assignments
from skill.scripts.verify_numbers import verify_numbers
from skill.scripts.verify_report_evidence import evidence_index, verify_report_evidence

FIELDS = {"title", "problem", "mention_id", "question_id", "theme_id", "hypothesis", "next_step"}
COUNT_FIELDS = ("mention_count", "mention_pct", "source_type_count", "source_type_pct",
                "source_type_breakdown", "segment_count", "segment_pct", "segment_breakdown", "mention_ids")
_NUMERAL = re.compile(r"(?<!\w)(?:[$€£])?\d+(?:,\d{3})*(?:\.\d+)?(?:%|[kKmMxX])?(?!\w)")


def _markdown(text):
    """Treat evidence and candidate prose as text, not executable Markdown."""
    escaped = html.escape(str(text), quote=False)
    return re.sub(r"([\\`*_{}\[\]#!|])", r"\\\1", escaped)


def _numeric_tokens(text):
    return {match.group().replace(",", "").lower() for match in _NUMERAL.finditer(text)}


def _validate_problem_numbers(candidate, question, theme, mention, statistics=None):
    """Combine the existing prose check with literal checks it intentionally skips.

    Units, causal interpretations, and number words still require the semantic
    drafting check. A matching numeral alone is not proof of a complete claim.
    """
    text = candidate["title"] + ". " + candidate["problem"]
    if statistics is not None:
        from skill.scripts.claim_quantities import validate_quantities, _quantities
        errors = validate_quantities(text, statistics)
        # Remove only recognized, scope-validated count expressions. Business numerals
        # (prices, durations, ROI) still need literal support in the selected quote.
        uncounted = text
        for expression in reversed(_quantities(text)):
            uncounted = uncounted[:expression["start"]] + " " + uncounted[expression["end"]:]
        unsupported = _numeric_tokens(uncounted) - _numeric_tokens(mention["quote"])
        if unsupported:
            errors.append(f"Unsupported numeric literals in title/problem: {sorted(unsupported)}")
        if errors:
            raise ValueError("; ".join(errors))
        return
    scoped_theme = {**theme, "sample_quotes": [{"quote": mention["quote"]}]}
    scoped_findings = {"questions": [{**question, "themes": [scoped_theme]}]}
    issues = verify_numbers({"questions": [{"question_id": question["question_id"], "synthesis": text}]}, scoped_findings)
    errors = [issue["message"] for issue in issues if issue["severity"] == "error"]
    allowed = _numeric_tokens(mention["quote"])
    for field in ("mention_count", "source_type_count", "segment_count"):
        if field in theme:
            allowed.add(str(theme[field]))
    for field in ("mention_pct", "source_type_pct", "segment_pct"):
        if field in theme:
            allowed.add(str(theme[field]) + "%")
    for field in ("total_mentions", "total_source_types", "total_segments"):
        if field in question:
            allowed.add(str(question[field]))
    unsupported = _numeric_tokens(text) - allowed
    if unsupported:
        errors.append(f"Unsupported numeric literals in title/problem: {sorted(unsupported)}")
    if errors:
        raise ValueError("; ".join(errors))


def render_opportunities(candidates, findings, records, plan, assignments, theme_definitions, methodology, *, opportunity_statistics=None):
    """Validate references and render prose; never accept model-provided quotes/counts."""
    if not isinstance(candidates, list) or len(candidates) > 5:
        raise ValueError("Opportunities must be an array of zero to five candidates.")
    if opportunity_statistics is not None and (not isinstance(opportunity_statistics, list) or len(opportunity_statistics) != len(candidates)):
        raise ValueError("Opportunity statistics must cover exactly the accepted candidates.")
    for i, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or set(candidate) != FIELDS:
            raise ValueError(f"Opportunity {i + 1}: use exactly the documented seven fields; do not supply quotes or counts.")
        if any(not isinstance(value, str) or not value.strip() for value in candidate.values()):
            raise ValueError(f"Opportunity {i + 1}: every field must be a nonempty string.")

    errors = validate_json(findings, "findings")
    if errors:
        raise ValueError(f"Invalid findings: {errors}")
    entries = evidence_index(records)
    retained = {entry["mention_id"]: entry for entry in entries}
    question_ids = {question["id"] for question in plan["questions"]}
    if set(assignments) != question_ids or set(theme_definitions) != question_ids:
        raise ValueError("Assignments and theme definitions must cover the exact locked questions.")
    rows_by_question = {}
    assignment_lookup = {}
    for qid in question_ids:
        definition = theme_definitions[qid]
        assignment = assignments[qid]
        if definition.get("question_id") != qid or assignment.get("question_id") != qid:
            raise ValueError(f"Question ownership mismatch in {qid} artifacts.")
        errors = validate_json(definition, "themes")
        errors += validate_assignments(
            assignment,
            {mid for mid, entry in retained.items() if entry["question_id"] == qid},
            {theme["theme_id"] for theme in definition["themes"]},
        )
        if errors:
            raise ValueError(f"Invalid assignments/themes for {qid}: {errors}")
        rows_by_question[qid] = assignment["assignments"]
        for row in assignment["assignments"]:
            assignment_lookup[(qid, row["mention_id"])] = row["theme_id"]

    # Use the existing computation engine so duplicate, stale, or invented
    # published counts cannot become the opportunities' evidence statistics.
    recomputed = compute_findings(rows_by_question, records, plan, theme_definitions)
    computed_questions = {q["question_id"]: q for q in recomputed["questions"]}
    saved_questions = {q["question_id"]: q for q in findings["questions"]}
    if len(saved_questions) != len(findings["questions"]) or set(saved_questions) != question_ids:
        raise ValueError("Findings do not cover the exact locked questions once each.")

    parts = ["# Strategic opportunities", "",
             "Customer problems below are observations from the supplied evidence. Strategic hypotheses and proposed next steps require validation.", "",
             "## Evidence and limitations", ""]
    parts.extend(f"- {_markdown(line)}" for line in audit_summary_lines(methodology))
    if methodology.get("data_quality_notes"):
        parts.extend(["", "**Data quality notes:** " + _markdown(methodology["data_quality_notes"])])
    if not candidates:
        parts.extend(["", "## Evidence gap", "",
                      "No defensible strategic opportunities were selected from the retained evidence. This does not establish that no opportunities exist. No additional audit or research was started.", ""])
        return "\n".join(parts)

    for i, candidate in enumerate(candidates, 1):
        qid, tid, mid = (candidate[field] for field in ("question_id", "theme_id", "mention_id"))
        mention = retained.get(mid)
        if mention is None or mention["question_id"] != qid:
            raise ValueError(f"Opportunity {i}: mention is not retained for the stated question.")
        if assignment_lookup.get((qid, mid)) != tid:
            raise ValueError(f"Opportunity {i}: mention is not assigned to the stated theme.")
        question = computed_questions[qid]
        theme = next((theme for theme in question["themes"] if theme["theme_id"] == tid), None)
        saved_question = saved_questions[qid]
        saved_themes = [theme for theme in saved_question["themes"] if theme["theme_id"] == tid]
        if theme is None or len(saved_themes) != 1:
            raise ValueError(f"Opportunity {i}: theme is missing or duplicated in findings.")
        saved_theme = saved_themes[0]
        if any(saved_theme.get(key) != theme.get(key) for key in COUNT_FIELDS) or any(
            saved_question.get(key) != question.get(key)
            for key in ("total_mentions", "total_source_types", "total_segments")
        ):
            raise ValueError(f"Opportunity {i}: findings counts or evidence membership are stale; do not invent replacements.")
        _validate_problem_numbers(candidate, question, theme, mention,
                                  statistics=opportunity_statistics[i - 1] if opportunity_statistics is not None else None)
        quote_check = verify_report_evidence(
            {"questions": [{"question_id": qid,
                            "synthesis": candidate["title"] + "\n" + candidate["problem"],
                            "takeaways": [candidate["hypothesis"], candidate["next_step"]]}]},
            [{"file_id": mention["file_id"], "mentions": [mention]}],
        )
        if quote_check:
            raise ValueError(f"Opportunity {i}: candidate quotations must trace to its selected mention: {quote_check}")

        parts.extend(["", f"## {i}. {_markdown(candidate['title']).replace(chr(10), ' ')}", "",
                      "**Observed problem:** " + _markdown(candidate["problem"]), "",
                      "**Customer evidence:**", ""])
        parts.extend("> " + _markdown(line) for line in mention["quote"].splitlines())
        parts.extend(["", "— " + _markdown(mention["quote_attribution"]), "",
                      f"**Evidence reference:** `{mid}` · `{qid}` · `{tid}` · source `{mention['file_id']}`", "",
                      f"**Theme:** {_markdown(theme['label'])}", "",
                      f"**Whole-theme context:** {theme['mention_count']} of {question['total_mentions']} retained mentions ({theme['mention_pct']}%) for this question; {theme['source_type_count']} source types; {theme['segment_count']} recorded segments. These totals describe the entire theme, not the number supporting this specific opportunity.", "",
                      "**Strategic hypothesis — to validate:** " + _markdown(candidate["hypothesis"]), "",
                      "**Proposed next step:** " + _markdown(candidate["next_step"])])
        if theme.get("demoted"):
            parts.extend(["", "**Sparse evidence:** This theme falls below the report's display threshold; treat this opportunity as an individual signal."])
    return "\n".join(parts) + "\n"


def generate_opportunities(root, input_path="synthesis/output/opportunities.json"):
    root = Path(root).resolve()
    gate = verify_gate(root)
    if not gate["complete"]:
        raise ValueError(gate["reason"])
    plan = read_json(root / "synthesis/plan.json")
    records = read_records(root / "synthesis/audit/validated_mentions.jsonl")
    findings = read_json(root / "synthesis/output/findings.json")
    assignments = {}
    definitions = {}
    for question in plan["questions"]:
        qid = question["id"]
        assignments[qid] = read_json(root / "synthesis/pipeline" / f"merged_{qid}.json")
        definitions[qid] = read_json(root / "synthesis/pipeline" / f"themes_{qid}.json")
    methodology = assemble_methodology(plan, project_root=root)
    report_path = root / "synthesis/output/report.json"
    if report_path.exists():
        report_notes = read_json(report_path).get("methodology", {}).get("data_quality_notes", "")
        if report_notes and report_notes not in methodology["data_quality_notes"]:
            methodology["data_quality_notes"] += " " + report_notes
    candidates = read_json(root / input_path)
    statistics = None
    if read_json(root / "synthesis/request.json").get("writing_mode") == "section_packets_v1":
        from skill.scripts import batch_report
        from skill.scripts.run_control import RunControl
        state = RunControl(root).status()
        step = state["steps"].get("report", {})
        if step.get("status") != "complete":
            raise ValueError("Complete admitted executive output before rendering opportunities.")
        frozen = f"synthesis/batch/report-attempt-{step['attempts']}.json"
        raw = read_json(root / frozen)
        admission, _, _ = batch_report._executive_admission(root, raw)
        batch_report._admission_receipt(root, frozen, admission, verify=True)
        material, _, findings = batch_report._executive_material(root, raw)
        if candidates != material["opportunities"]:
            raise ValueError("Opportunity candidates differ from the immutable executive admission.")
        statistics = material["opportunity_statistics"]
    return render_opportunities(candidates, findings, records, plan,
                                assignments, definitions, methodology, opportunity_statistics=statistics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--input", default="synthesis/output/opportunities.json")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    temporary = None
    try:
        content = generate_opportunities(root, args.input)
        destination = root / "reports/opportunities.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".opportunities-", dir=destination.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, destination)
        print(f"Written {destination}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Opportunities stopped: {exc}\n")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()

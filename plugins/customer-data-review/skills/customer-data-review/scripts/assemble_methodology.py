"""Assemble the methodology section of report.json from plan.json.

Usage: python -m v2.scripts.assemble_methodology plan.json [--deviations-path FILE]
Output: JSON methodology section to stdout.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from skill.schemas.constants import DISPLAY_CATEGORIES


def _count_sources(plan):
    """Count analyzed vs excluded sources.

    Only good-quality, non-excluded sources count as analyzed — matching
    what dispatch_sources actually sends to agents.
    """
    files = plan.get("files", [])
    excluded = sum(1 for s in files if s.get("excluded") is True)
    analyzed = sum(
        1 for s in files
        if s.get("excluded") is not True and s.get("quality") == "good"
    )
    # Sources without quality field are not analyzable; count alongside excluded
    no_quality = sum(
        1 for s in files
        if s.get("excluded") is not True and "quality" not in s
    )
    return analyzed, excluded + no_quality


def _build_source_types(plan):
    """Group analyzed sources by DISPLAY_CATEGORIES.

    Only good-quality, non-excluded sources are counted — matching
    what dispatch_sources actually sends to agents.
    """
    counts = Counter()
    for source in plan.get("files", []):
        if source.get("excluded") is True:
            continue
        if source.get("quality") != "good":
            continue
        category = DISPLAY_CATEGORIES.get(source.get("source_type", "other"), "Other")
        counts[category] += 1
    return dict(counts)


def _compute_audit_pass_rate(deviations_path=None):
    """Compute audit pass rate from deviations JSONL file.

    Computes two rates:
    - raw: first-pass results (round 1 audit_check entries only)
    - corrected: final state (latest result per mention_id after re-extraction)

    The "rate" key is kept as corrected for backwards compatibility.

    Returns:
        dict with keys:
            rate (float 0-1) — corrected rate (backwards compat),
            total (int) — corrected total,
            passed (int) — corrected passed,
            raw_rate (float 0-1), raw_total (int), raw_passed (int).
    """
    empty = {
        "rate": None, "total": 0, "passed": 0,
        "raw_rate": None, "raw_total": 0, "raw_passed": 0,
    }
    if deviations_path is None:
        return empty
    path = Path(deviations_path)
    if not path.is_file():
        return empty

    # Per-mention round-1 results (raw / first-pass)
    per_mention_r1 = {}  # key -> details dict

    # Per-mention latest result (corrected / final state)
    per_mention = {}  # key -> details dict

    legacy_counter = 0

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if entry.get("category") != "audit_result":
                continue
            if entry.get("step") != "audit_check":
                continue

            details = entry.get("details") or {}
            passed_flag = details.get("passed", False)
            mention_id = details.get("mention_id")
            entry_round = details.get("round", 1)

            # Assign a stable key for legacy entries without mention_id
            if mention_id is None:
                key = f"__legacy_{legacy_counter}"
                legacy_counter += 1
            else:
                key = mention_id

            # Raw: track per-mention round-1 results (last round-1 entry wins)
            if entry_round == 1:
                per_mention_r1[key] = details

            # Corrected: track latest result per mention
            existing = per_mention.get(key)
            if existing is None or entry_round >= existing.get("round", 0):
                per_mention[key] = details

    # Raw totals from per-mention round-1
    raw_total = len(per_mention_r1)
    raw_passed = sum(
        1 for d in per_mention_r1.values() if d.get("passed", False)
    )

    # Corrected totals from per-mention latest
    corrected_total = len(per_mention)
    corrected_passed = sum(
        1 for d in per_mention.values() if d.get("passed", False)
    )

    def _rate(p, t):
        if t == 0:
            return None
        return round(p / t, 2)

    return {
        "rate": _rate(corrected_passed, corrected_total),
        "total": corrected_total,
        "passed": corrected_passed,
        "raw_rate": _rate(raw_passed, raw_total),
        "raw_total": raw_total,
        "raw_passed": raw_passed,
    }


def _build_data_quality_notes(plan):
    """Build data quality notes from excluded/bad-quality sources."""
    excluded_notes = []
    other_notes = []
    for source in plan.get("files", []):
        if source.get("excluded") is True:
            reason = source.get("exclusion_reason", "no reason given")
            excluded_notes.append(f"{source.get('file_id', 'unknown')} ({reason})")
        elif source.get("quality") == "bad":
            other_notes.append(f"{source.get('file_id', 'unknown')} (low quality)")
        elif "quality" not in source:
            other_notes.append(f"{source.get('file_id', 'unknown')} (no quality classification)")

    all_notes = excluded_notes + other_notes
    if not all_notes:
        return ""

    prefix = f"{len(excluded_notes)} source{'s' if len(excluded_notes) != 1 else ''} excluded"
    return f"{prefix}: {', '.join(all_notes)}"


def assemble_methodology(plan, deviations_path=None, project_root=None):
    """Assemble methodology section from plan.json.

    Args:
        plan: Parsed plan.json dict.
        deviations_path: Optional path to deviations JSONL file.

    Returns:
        dict matching report.schema.json#methodology.
    """
    included, excluded = _count_sources(plan)
    questions = plan.get("questions", [])

    total_interactions = sum(
        s.get("interaction_count", 1) for s in plan.get("files", [])
        if s.get("excluded") is not True and s.get("quality") == "good"
    )

    audit = _compute_audit_pass_rate(deviations_path)

    result = {
        "source_count": included,
        "excluded_count": excluded,
        "source_types": _build_source_types(plan),
        "question_count": len(questions),
        "questions": [f"{q['id']}: {q['text']}" for q in questions],
        "audit_pass_rate": audit["rate"],
        "audit_total": audit["total"],
        "audit_passed": audit["passed"],
        "audit_pass_rate_raw": audit["raw_rate"],
        "audit_pass_rate_corrected": audit["rate"],
        "confidence_model": "Mention % = signal strength. Source type prevalence = signal breadth.",
        "data_quality_notes": _build_data_quality_notes(plan),
    }
    if project_root is not None:
        from skill.scripts.bounded_audit import verify_gate
        verdict = verify_gate(project_root)
        if not verdict["complete"]:
            raise ValueError(verdict["reason"])
        audit_summary = verdict["summary"]
        result.update({
            "audit_mode": audit_summary["mode"],
            "audit_status": audit_summary["status"],
            "audit_sample_size": audit_summary["sample_size"],
            "audit_population": audit_summary["population"],
            "audit_excluded": audit_summary["excluded"],
            "audit_unassessed": audit_summary["unassessed"],
            "audit_total": audit_summary["sample_size"],
            "audit_passed": audit_summary["passed"],
            "audit_pass_rate": audit_summary["corrected_pass_rate"],
            "audit_pass_rate_raw": audit_summary["raw_pass_rate"],
            "audit_pass_rate_corrected": audit_summary["corrected_pass_rate"],
            "audit_raw_pass_rate": audit_summary["raw_pass_rate"],
            "audit_corrected_pass_rate": audit_summary["corrected_pass_rate"],
        })
        audit_notes = audit_summary["limitations"]
        if audit_summary["excluded"]:
            audit_notes += f" {audit_summary['excluded']} known failed/uncertain mentions excluded; all counts use retained evidence."
        from pathlib import Path
        omitted_sources, omitted_count = [], 0
        for source in plan.get("files", []):
            if source.get("quality") != "good" or source.get("excluded"):
                continue
            fid = source["file_id"]
            note_path = Path(project_root) / "synthesis/pipeline/extraction-notes" / f"{fid}.json"
            if note_path.exists():
                note = json.loads(note_path.read_text(encoding="utf-8"))
                omitted = note.get("omitted_ungrounded_ids", [])
                if note.get("file_id") != fid or not isinstance(omitted, list) or not all(isinstance(mid, str) for mid in omitted):
                    raise ValueError(f"Invalid extraction omission note: {fid}")
                if omitted:
                    omitted_sources.append(fid)
                    omitted_count += len(omitted)
        if omitted_count:
            audit_notes += (f" During extraction, {omitted_count} ungrounded candidate mentions were omitted after one repair "
                            f"across {len(omitted_sources)} sources ({', '.join(omitted_sources)}). "
                            "These extraction omissions are outside the later semantic audit denominator.")
        result["data_quality_notes"] = " ".join(filter(None, [result["data_quality_notes"], audit_notes]))
    if total_interactions != included:
        result["total_interactions"] = total_interactions
    return result


def main():
    parser = argparse.ArgumentParser(description="Assemble methodology section from plan.json")
    parser.add_argument("plan", help="Path to plan.json")
    parser.add_argument("--deviations-path", help="Path to deviations JSONL file")
    parser.add_argument("--project-root", help="Bounded run root; source of authoritative audit summary")
    args = parser.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    methodology = assemble_methodology(plan, args.deviations_path, args.project_root)
    print(json.dumps(methodology, indent=2))


if __name__ == "__main__":
    main()

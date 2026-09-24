"""Phase 3c — Computation engine: assignments + mentions + plan → findings.json."""

import argparse
import json
import math
import re
import sys
from collections import defaultdict

from skill.schemas.constants import DISPLAY_CATEGORIES
from skill.schemas.validate import validate_json
from skill.scripts._quote_quality import has_markdown_formatting, is_low_signal_quote, looks_like_csv_row


def _extract_file_id(mention_id: str) -> str:
    """Extract file_id from a mention_id like 'sales_01_Q1_0' → 'sales_01'."""
    # Greedy .+ is correct here: the $ anchor forces backtracking to the
    # rightmost _Q<digits>_<digits> suffix, so file_ids containing _Q are safe.
    match = re.match(r"^(.+)_Q\d+_\d+$", mention_id)
    if not match:
        raise ValueError(f"Cannot extract file_id from '{mention_id}'")
    return match.group(1)


_STOPWORDS = frozenset({
    "the", "and", "for", "are", "has", "was", "with", "from", "that",
    "they", "this", "their", "have", "been", "were", "who", "what",
    "which", "when", "where", "how", "not", "but", "can", "all",
    "more", "its", "into", "than", "also", "other", "any", "each",
    "about", "our", "your", "out", "new", "one", "use", "does",
    "issues", "using", "without", "across", "between",
    # Role titles — prevent self-introductions from falsely matching theme
    # keywords (e.g., "trainer" matching "training" via substring).
    "manager", "director", "trainer", "coordinator", "instructor",
    "coach", "owner", "founder", "president", "officer", "supervisor",
    "specialist", "head", "lead",
})


def _extract_theme_keywords(theme_def: dict) -> set[str]:
    """Extract lowercase keyword tokens from a theme's include and description fields.

    Filters stopwords and words shorter than 3 characters to prevent
    over-matching on generic terms.
    """
    words = set()
    for field in ("include", "description"):
        text = theme_def.get(field, "")
        for phrase in text.replace(",", " ").split():
            w = phrase.lower().strip(".,;:()")
            if len(w) >= 3 and w not in _STOPWORDS:
                words.add(w)
    return words


def _relevance_penalty(quote_text: str, theme_keywords: set[str]) -> int:
    """Return 1 if the quote has zero keyword overlap with the theme, else 0.

    Uses bidirectional substring matching for words of 4+ characters
    to handle stemming (e.g., "report" matches "reporting"). Exact match
    is required for 3-character keywords to avoid false positives.
    """
    if not theme_keywords:
        return 0
    quote_words = set()
    for word in quote_text.lower().split():
        w = word.strip(".,;:()\"'!?-")
        if len(w) >= 3 and w not in _STOPWORDS:
            quote_words.add(w)
    if not quote_words:
        return 0  # can't determine relevance from very short quotes
    for kw in theme_keywords:
        for qw in quote_words:
            if len(kw) >= 4 and len(qw) >= 4:
                if kw in qw or qw in kw:
                    return 0
            elif kw == qw:
                return 0
    return 1


_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}")


def _low_signal_penalty(quote: str) -> int:
    """Return 1 if the quote is a self-introduction or meta-commentary."""
    return 1 if is_low_signal_quote(quote) else 0


def _disqualifying_penalty(quote: str) -> int:
    """Return 1 for quotes so bad they should lose notable priority.

    Extreme length violations and transcript markers override notable status
    because a 500-word transcript dump or a 3-word filler quote cannot be a
    meaningful pull quote regardless of how the extraction agent flagged it.
    """
    words = len(quote.split())
    if words > 150 or words < 5:
        return 1
    if _TIMESTAMP_RE.search(quote):
        return 1
    return 0


def _length_penalty(quote: str) -> int:
    """Penalize quotes outside the ideal 15-80 word range."""
    words = len(quote.split())
    if words < 15 or words > 80:
        return 1
    return 0


def _select_quotes(
    mention_ids: list[str],
    mentions_by_id: dict[str, dict],
    max_quotes: int = 5,
    theme_keywords: set[str] | None = None,
) -> list[dict]:
    """Select up to max_quotes sample quotes, prioritizing notable_quote=true.

    When theme_keywords is provided, quotes with zero keyword overlap are
    deprioritized (sorted to the bottom) but not rejected.

    Sort order:
    1. Disqualifying penalty (extreme length/transcript markers override everything)
    2. Low-signal penalty (self-introductions/meta-commentary)
    3. Notable flag (notable=True preferred)
    4. Format quality (CSV rows, markdown formatting)
    5. Length penalty (outside 15-80 words)
    6. Relevance (keyword overlap with theme)
    7. mention_id (stability)
    """
    candidates = []
    for rid in mention_ids:
        mention_obj = mentions_by_id.get(rid)
        if not mention_obj:
            continue
        candidates.append(mention_obj)

    # Filter out mentions with empty quotes or quotes exceeding 100 words
    candidates = [
        c for c in candidates
        if c.get("quote") and len(c["quote"].split()) <= 100
    ]

    def _quality_penalty(quote: str) -> int:
        penalty = 0
        if looks_like_csv_row(quote):
            penalty += 2
        if has_markdown_formatting(quote):
            penalty += 1
        return penalty

    candidates.sort(key=lambda r: (
        _disqualifying_penalty(r.get("quote", "")),
        _low_signal_penalty(r.get("quote", "")),
        not r.get("notable_quote", False),
        _quality_penalty(r.get("quote", "")),
        _length_penalty(r.get("quote", "")),
        _relevance_penalty(r.get("quote", ""), theme_keywords or set()),
        r["mention_id"],
    ))

    # Source diversity: cap quotes per file_id at ceil(max_quotes / 2).
    # Relaxed to max_quotes when all candidates share one source.
    unique_sources = {
        _extract_file_id(c["mention_id"])
        for c in candidates
        if re.match(r"^(.+)_Q\d+_\d+$", c["mention_id"])
    }
    per_source_cap = (
        max_quotes if len(unique_sources) <= 1
        else math.ceil(max_quotes / 2)
    )

    seen_quote_texts = set()
    source_counts: dict[str, int] = {}
    selected = []
    for candidate in candidates:
        normalized = " ".join(candidate["quote"].lower().split())
        if normalized in seen_quote_texts:
            continue
        try:
            fid = _extract_file_id(candidate["mention_id"])
        except ValueError:
            continue
        if source_counts.get(fid, 0) >= per_source_cap:
            continue
        seen_quote_texts.add(normalized)
        source_counts[fid] = source_counts.get(fid, 0) + 1
        selected.append(candidate)
        if len(selected) >= max_quotes:
            break

    quotes = []
    for mention_obj in selected:
        try:
            fid = _extract_file_id(mention_obj["mention_id"])
        except ValueError:
            continue
        quotes.append({
            "quote": mention_obj.get("quote", ""),
            "attribution": mention_obj.get("quote_attribution", ""),
            "file_id": fid,
            "notable": mention_obj.get("notable_quote", False),
            "mention": mention_obj.get("mention", ""),
            "sentiment": mention_obj.get("sentiment", ""),
        })
    return quotes


def compute_findings(
    assignments: dict[str, list[dict]],
    mentions: list[dict],
    plan: dict,
    themes: dict[str, dict],
) -> dict:
    """Compute findings.json from merged assignments, mentions, plan, and themes.

    Args:
        assignments: question_id → list of {"mention_id", "theme_id"} dicts.
        mentions: Parsed validated_mentions.jsonl lines (each has file_id + mentions list).
        plan: Parsed plan.json.
        themes: question_id → parsed themes_{qid}.json.

    Returns:
        findings.json structure ready for schema validation.
    """
    # Build question lookup from plan
    question_map = {q["id"]: q["text"] for q in plan.get("questions", [])}

    # Build source lookups: file_id → source_type, file_id → segment
    source_type_map = {}
    segment_map = {}
    for src in plan.get("files", []):
        if src.get("excluded") is not True:
            source_type_map[src["file_id"]] = src["source_type"]
            segment_map[src["file_id"]] = src.get("segment", "unknown")

    # Build mention lookup: mention_id → mention dict (flattened)
    mentions_by_id: dict[str, dict] = {}
    for line in mentions:
        file_id = line["file_id"]
        for mention_obj in line.get("mentions", []):
            obj_copy = dict(mention_obj)
            obj_copy["_file_id"] = file_id
            obj_copy["_source_type"] = source_type_map.get(file_id, "other")
            obj_copy["_segment"] = segment_map.get(file_id, "unknown")
            mentions_by_id[obj_copy["mention_id"]] = obj_copy

    output_questions = []

    plan_qids = sorted(question_map.keys())
    for qid in plan_qids:
        q_assignments = assignments.get(qid, [])
        q_themes = themes.get(qid, {}).get("themes", [])
        theme_map = {t["theme_id"]: t for t in q_themes}

        # All mention_ids for this question (from mentions data, includes unassigned)
        all_q_rids = {
            rid for rid, mention_obj in mentions_by_id.items()
            if mention_obj.get("question_id") == qid
        }
        # Also include any IDs from assignments (belt-and-suspenders)
        all_q_rids.update(a["mention_id"] for a in q_assignments)

        # All unique source types and segments for this question
        all_source_types = set()
        all_segments = set()
        for rid in all_q_rids:
            mention_obj = mentions_by_id.get(rid)
            if mention_obj:
                stype = mention_obj.get("_source_type", "other")
                display_cat = DISPLAY_CATEGORIES.get(stype, "Other")
                all_source_types.add(display_cat)
                all_segments.add(mention_obj.get("_segment", "unknown"))

        total_mentions = len(all_q_rids)
        total_source_types = len(all_source_types)
        total_segments = len(all_segments)

        # Group assignments by theme
        theme_rids: dict[str, list[str]] = defaultdict(list)
        assigned_rids: set[str] = set()
        for a in q_assignments:
            theme_rids[a["theme_id"]].append(a["mention_id"])
            assigned_rids.add(a["mention_id"])

        # Build theme findings — iterate ALL defined themes, not just assigned ones
        theme_findings = []
        all_theme_ids = sorted(set(theme_map.keys()) | set(theme_rids.keys()))
        for tid in all_theme_ids:
            rids = theme_rids.get(tid, [])
            theme_def = theme_map.get(tid, {})

            # Source type breakdown — count unique files per display category
            cat_sources: dict[str, set[str]] = defaultdict(set)
            for rid in rids:
                mention_obj = mentions_by_id.get(rid)
                if mention_obj:
                    try:
                        fid = _extract_file_id(rid)
                    except ValueError:
                        continue
                    stype = mention_obj.get("_source_type", "other")
                    display_cat = DISPLAY_CATEGORIES.get(stype, "Other")
                    cat_sources[display_cat].add(fid)
            breakdown = {cat: len(fids) for cat, fids in cat_sources.items()}

            # Segment breakdown — count unique files per segment
            seg_sources: dict[str, set[str]] = defaultdict(set)
            for rid in rids:
                mention_obj = mentions_by_id.get(rid)
                if mention_obj:
                    try:
                        fid = _extract_file_id(rid)
                    except ValueError:
                        continue
                    seg = mention_obj.get("_segment", "unknown")
                    seg_sources[seg].add(fid)
            seg_breakdown = {seg: len(fids) for seg, fids in seg_sources.items()}

            mention_count = len(rids)
            source_type_count = len(breakdown)
            mention_pct = round(mention_count / total_mentions * 100) if total_mentions else 0
            source_type_pct = round(source_type_count / total_source_types * 100) if total_source_types else 0

            theme_kw = _extract_theme_keywords(theme_def)
            sample_quotes = _select_quotes(rids, mentions_by_id, theme_keywords=theme_kw)

            theme_findings.append({
                "theme_id": tid,
                "label": theme_def.get("label", ""),
                "description": theme_def.get("description", ""),
                "type": theme_def.get("type", "other"),
                "mention_count": mention_count,
                "mention_pct": mention_pct,
                "source_type_count": source_type_count,
                "source_type_pct": source_type_pct,
                "source_type_breakdown": dict(breakdown),
                "segment_count": len(seg_breakdown),
                "segment_pct": round(len(seg_breakdown) / total_segments * 100) if total_segments else 0,
                "segment_breakdown": dict(seg_breakdown),
                "sample_quotes": sample_quotes,
                "mention_ids": sorted(rids),
            })

        # Inclusion floor: demote themes below a proportional threshold.
        # The floor scales with dataset size: max(2, ceil(total * 0.03)).
        # For very sparse questions (<10 mentions), skip demotion entirely.
        inclusion_floor = max(2, math.ceil(total_mentions * 0.03))
        non_demoted_count = sum(
            1 for t in theme_findings
            if t["mention_count"] >= inclusion_floor
        )
        # Safety valve: skip all demotions if data is too sparse or
        # fewer than 2 themes would survive.
        if total_mentions >= 10 and non_demoted_count >= 2:
            for t in theme_findings:
                if t["mention_count"] < inclusion_floor:
                    t["demoted"] = True
                    # Move demoted theme's mentions into outlier pool
                    for rid in t["mention_ids"]:
                        assigned_rids.discard(rid)

        # High-intensity outliers: mentions for this question not assigned to any theme
        outlier_rids = all_q_rids - assigned_rids
        outliers = []
        for rid in sorted(outlier_rids):
            mention_obj = mentions_by_id.get(rid, {})
            try:
                fid = _extract_file_id(rid)
            except ValueError:
                continue  # Skip outliers with malformed mention_ids
            outliers.append({
                "mention_id": rid,
                "mention": mention_obj.get("mention", ""),
                "quote": mention_obj.get("quote", ""),
                "file_id": fid,
            })

        # Compute source breakdown for outliers (same pattern as themes)
        outlier_cat_sources: dict[str, set[str]] = defaultdict(set)
        for o in outliers:
            rid = o["mention_id"]
            mention_obj = mentions_by_id.get(rid)
            if mention_obj:
                stype = mention_obj.get("_source_type", "other")
                display_cat = DISPLAY_CATEGORIES.get(stype, "Other")
                outlier_cat_sources[display_cat].add(o["file_id"])
        outlier_breakdown = {cat: len(fids) for cat, fids in outlier_cat_sources.items()}

        output_questions.append({
            "question_id": qid,
            "question_text": question_map.get(qid, ""),
            "total_mentions": total_mentions,
            "total_source_types": total_source_types,
            "total_segments": total_segments,
            "sparse": total_mentions < 15,
            "themes": theme_findings,
            "high_intensity_outliers": outliers,
            "outlier_source_type_count": len(outlier_breakdown),
            "outlier_source_type_breakdown": outlier_breakdown,
        })

    return {"questions": output_questions}


def collect_demotion_entries(findings: dict) -> list[dict]:
    """Scan findings for demoted themes and return deviation log entries.

    Pure function — caller handles file writes. Each demoted theme
    produces one entry suitable for appending to deviations.jsonl.
    """
    from skill.scripts.log_deviation import build_entry

    entries = []
    for q in findings.get("questions", []):
        qid = q["question_id"]
        for theme in q.get("themes", []):
            if theme.get("demoted"):
                entries.append(build_entry(
                    phase="3c",
                    step="theme_demotion",
                    category="data_quality",
                    description=(
                        f"Theme {theme['theme_id']} demoted: "
                        f"{theme['mention_count']} mention(s) below "
                        f"inclusion floor"
                    ),
                    resolution="logged_only",
                    question_id=qid,
                    details={
                        "theme_id": theme["theme_id"],
                        "mention_count": theme["mention_count"],
                        "mention_ids": theme.get("mention_ids", []),
                    },
                ))
    return entries


def _is_deletion_only_display(original: str, display: str) -> bool:
    """Allow word deletions and case/punctuation cleanup, never added words."""
    def words(text):
        return re.findall(r"\w+(?:'\w+)*", text.replace("’", "'").casefold())

    source_words = iter(words(original))
    display_words = words(display)
    return bool(display_words) and all(
        any(word == candidate for candidate in source_words)
        for word in display_words
    )


def preserve_display_quotes(findings: dict, previous: dict) -> int:
    """Reuse verified cleanup only for the same quote, attribution, and source.

    Findings are modified in place. Ambiguous earlier display variants and
    legacy outliers with no attribution are deliberately not reused.
    """
    def quotes(document):
        for question in document.get("questions", []):
            for theme in question.get("themes", []):
                yield from theme.get("sample_quotes", [])
            yield from question.get("high_intensity_outliers", [])

    def identity(quote):
        key = tuple(quote.get(field) for field in ("quote", "attribution", "file_id"))
        return key if all(isinstance(value, str) and value for value in key) else None

    candidates = defaultdict(set)
    for quote in quotes(previous):
        key = identity(quote)
        display = quote.get("display_quote")
        if key and isinstance(display, str) and _is_deletion_only_display(key[0], display):
            candidates[key].add(display)

    reused = 0
    for quote in quotes(findings):
        options = candidates.get(identity(quote), set())
        if len(options) == 1:
            quote["display_quote"] = next(iter(options))
            reused += 1
    return reused


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute findings from merged assignments")
    parser.add_argument("--assignments-dir", required=True, help="Dir with merged_Q*.json files")
    parser.add_argument("--mentions-jsonl", required=True, help="validated_mentions.jsonl")
    parser.add_argument("--plan-json", required=True, help="plan.json")
    parser.add_argument("--themes-dir", required=True, help="Dir with themes_Q*.json files")
    parser.add_argument("--output", required=True, help="Output findings.json path")
    args = parser.parse_args()

    from pathlib import Path

    # Load assignments
    assignments: dict[str, list[dict]] = {}
    adir = Path(args.assignments_dir)
    for f in sorted(adir.glob("merged_Q*.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        qid = data.get("question_id", f.stem.replace("merged_", ""))
        assignments[qid] = data.get("assignments", [])

    # Load mentions
    mentions = []
    with open(args.mentions_jsonl, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                mentions.append(json.loads(line))

    # Load plan
    with open(args.plan_json, encoding="utf-8") as fh:
        plan = json.load(fh)

    # Load themes
    themes_data: dict[str, dict] = {}
    tdir = Path(args.themes_dir)
    for f in sorted(tdir.glob("themes_Q*.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        qid = data.get("question_id", f.stem.replace("themes_", ""))
        themes_data[qid] = data

    findings = compute_findings(assignments, mentions, plan, themes_data)

    # Recomputing counts must not silently erase valid display cleanup. Read
    # the previous artifact before opening the output for writing.
    output_path = Path(args.output)
    if output_path.exists():
        try:
            with output_path.open(encoding="utf-8") as fh:
                previous = json.load(fh)
            if not isinstance(previous, dict):
                raise ValueError("findings root must be an object")
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot safely read prior findings: {exc}", file=sys.stderr)
            sys.exit(1)
        preserve_display_quotes(findings, previous)

    # Validate output
    errors = validate_json(findings, "findings")
    if errors:
        print("ERROR: findings output has schema errors:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()

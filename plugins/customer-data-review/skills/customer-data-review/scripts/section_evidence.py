"""Bounded section handoff: exact accounting, explicit selections and checked context.

The writer assesses meaning once. Scores and context declarations are judgments,
not independent proof of semantic accuracy. This module rejects broken references,
stale inventories and unchecked quotes without adding a model review loop.
"""

import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from skill.scripts.compute_findings import compute_findings
from skill.scripts._quote_quality import is_low_signal_quote, looks_like_csv_row
from skill.scripts.verify_quotes import normalize_text
from skill.scripts.verify_report_evidence import quoted_spans, _blockquote_texts

VERSION = 1
QUALITY_FIELDS = ("relevance", "support", "added_value", "clarity")
STATUSES = {"supported", "thin", "conflicted", "context_missing", "no_evidence"}
SECTION_FORMAT = {
    "questions": [{
        "question_id": "Q1", "status": "thin",
        "claims": [{"claim_id": "Q1_C1", "type": "observation", "text": "A bounded useful claim.",
                    "theme_ids": ["Q1_T1"], "support_mention_ids": ["source_Q1_0"],
                    "counterevidence_ids": [], "scope": "One source file; prevalence is unknown.",
                    "stat_refs": ["Q1_T1:sources"],
                    "quality": {"relevance": 2, "support": 2, "added_value": 2, "clarity": 2,
                                "reason": "Explains a concrete constraint in the cited evidence."}}],
        "summary_claim_id": "Q1_C1", "takeaway_claim_ids": [], "lead_quote_id": None,
        "theme_quote_ids": {}, "quote_reviews": {}, "omission_reasons": ["No useful leading quote."],
        "gaps": ["Breadth outside these source files is unknown."], "exec_candidates": ["Q1_C1"],
    }]
}
QUOTE_REVIEW_FORMAT = {
    "context_check_id": "copy source_contexts[candidate.context_ref].context_check_id",
    "speaker": "the named speaker in the context, or anonymous respondent",
    "claim_owner": "who owns the claim; preserve reported speech and seller claims",
    "context_sufficient": True,
    "quality": {"relevance": 2, "support": 2, "added_value": 2, "clarity": 2,
                "reason": "Explain why this quote illustrates this question and theme."},
}
SECTION_OUTPUT_CONTRACT = {
    "shape": "Return only {questions: [...]}, covering every question exactly once. Use exactly the required fields in format; claims may additionally include count_groups as documented. Code-created source observations additionally carry source_observation; never apply this marker to new prose. Examples are placeholders, not required content.",
    "status": {
        "supported": "Evidence supports useful commentary.",
        "thin": "Evidence is limited; bound any accepted claims.",
        "conflicted": "Preserve material tensions or counterevidence.",
        "context_missing": "Context is insufficient for useful interpretation; omit affected components.",
        "no_evidence": "Use if and only if this question has no mentions.",
    },
    "claims": "claim_id is unique within the question: Q1_C1, Q1_C2, etc. type is observation, implication or hypothesis. text and scope are nonempty. All ID arrays contain distinct IDs from this question. Each claim needs nonempty theme_ids and support_mention_ids; support must belong to those themes. counterevidence_ids cannot also be support.",
    "statistics": "stat_refs refer only to this question or the claim's themes. Copy exact unit, numerator, denominator, percentage and scope. A theme total is not a narrower claim's support count. Membership remains in mentions; source sets overlap. Do not sum across themes/questions or turn source files into unique accounts.",
    "count_groups": "Optional claim count_groups is an array of {field: text or scope, text: exact unique complete clause, basis: support or contrast, mention_ids: distinct IDs}. Support groups use support_mention_ids only; contrast groups may use support plus counterevidence. Bind the full quantitative expression including its unit and denominator. Outside bound clauses, counts use only claim support. Simpler nonnumeric subgroup prose is welcome.",
    "quantity_units": "Prefer concise qualitative scope; derived statistics already preserve exact counts. When a number matters, write explicit units: 40% of source files or 3 of 10 mentions, not standalone 40%. Both text and scope are checked. Source files are not unique customers, prospects or accounts without a verified identity map.",
    "selections": "summary_claim_id is an accepted claim ID or null; takeaway_claim_ids has 0-3 accepted claim IDs with meaning distinct from the summary; exec_candidates contains accepted claim IDs. Other accepted nonredundant claims appear as additional insights with their scope. lead_quote_id is a candidate quote_id or null, never a demoted theme. theme_quote_ids maps theme_id to 0-3 candidate quote IDs belonging to it. IDs are unique within each array.",
    "quote_reviews": "Map exactly the union of lead_quote_id and theme_quote_ids to quote_review_format objects. candidate.quote_id identifies the full mention (quote, attribution, theme, source). Read inventory.source_contexts[candidate.context_ref]; select only literal_verified=true contexts. Copy its context_check_id, set context_sufficient=true only when justified, use the speaker name as it appears in the span (or anonymous respondent), and establish claim_owner. Unlisted candidates have no checked context and cannot publish. Inline quotations must exactly reuse selected, reviewed quote text.",
    "quality": "Every accepted claim and selected quote needs integer relevance, support, added_value and clarity scores of 2 or 3 plus a nonempty reason. Omit a component failing any dimension; 3 is not a target.",
    "omissions": "Use null for absent summary/lead quote, [] for absent claims/takeaways/exec candidates, and {} for absent quote mappings. omission_reasons and gaps are arrays of nonempty strings (or []). Supply a concise omission reason if summary, takeaways or lead quote is absent. Every question still returns all format fields; omitted prose never removes evidence or counts.",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalized_offsets(text):
    """Match existing quote normalization while preserving raw character offsets."""
    chars, offsets = [], []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] != "\\":
            i += 1
        raw = text[i]
        if raw.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                offsets.append(i)
        else:
            for char in normalize_text(raw):
                chars.append(char)
                offsets.append(i)
        i += 1
    if chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


def _source_context(text, source_hash, quote, prepared):
    normalized, offsets = prepared
    needle = normalize_text(quote)
    matches = list(re.finditer(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalized)) if needle else []
    if len(matches) != 1:
        return {"literal_verified": False, "reason": "ambiguous_span" if matches else "span_not_found",
                "source_sha256": source_hash}
    match = matches[0]
    start, end = offsets[match.start()], offsets[match.end() - 1] + 1
    # One bounded context batch, gathered before drafting. Never silently retrieve
    # another span after rejection. The writer must omit when this is insufficient.
    left, right = max(0, start - 900), min(len(text), end + 900)
    context = {"source_sha256": source_hash, "start": start, "end": end,
               "context_start": left, "context_end": right, "text": text[left:right],
               "literal_verified": True}
    # Preserve the exact header of this speaker turn when the bounded excerpt
    # begins midway through a long answer. Never use a name from another turn.
    headers = list(re.finditer(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s+-\s+[^\r\n]+", text))
    previous = [i for i, header in enumerate(headers) if header.end() <= start]
    if previous:
        index = previous[-1]
        header = headers[index]
        turn_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        if end <= turn_end:
            context["speaker_turn"] = {"header": header.group(), "header_start": header.start(),
                                       "header_end": header.end(), "turn_end": turn_end}
    context["context_check_id"] = _digest(context)
    return context


def _stat(ref, unit, numerator, denominator, scope):
    numerator, denominator = sorted(set(numerator)), sorted(set(denominator))
    return {"stat_ref": ref, "unit": unit, "numerator_ids": numerator, "denominator_ids": denominator,
            "numerator": len(numerator), "denominator": len(denominator),
            "percentage": round(len(numerator) / len(denominator) * 100) if denominator else 0,
            "scope": scope}


def build_inventory(root, plan, records, findings, definitions, assignments):
    """Return full compact evidence, exact set statistics and <=3 quotes/theme.

    definitions[qid]['quote_nominations'] optionally maps theme_id -> mention_id.
    Only the nominee/first candidate gets one bounded source-context retrieval per
    theme before drafting. Other compact candidates cannot publish without checked
    context. Missing/ambiguous spans also remain visible but cannot publish.
    Source files are never treated as unique customers or accounts.
    """
    root = Path(root)
    question_map = {q["id"]: q for q in plan["questions"]}
    mentions, source_ids = {}, set()
    for record in records:
        source_ids.add(record["file_id"])
        for mention in record.get("mentions", []):
            mid = mention["mention_id"]
            if mid in mentions:
                raise ValueError(f"Duplicate retained mention ID: {mid}")
            if mention["question_id"] not in question_map:
                raise ValueError(f"Unknown retained question: {mention['question_id']}")
            mentions[mid] = {**mention, "source_id": record["file_id"]}
    if set(assignments) != set(question_map) or set(definitions) != set(question_map):
        raise ValueError("Every question must have locked definitions and assignments.")
    actual_questions = {q["question_id"]: q for q in findings["questions"]}
    if len(actual_questions) != len(findings["questions"]) or set(actual_questions) != set(question_map):
        raise ValueError("Findings must contain every question exactly once.")
    for qid in question_map:
        ids = [a["mention_id"] for a in assignments[qid]]
        expected = {mid for mid, m in mentions.items() if m["question_id"] == qid}
        themes = [t["theme_id"] for t in definitions[qid]["themes"]]
        if len(set(themes)) != len(themes):
            raise ValueError(f"{qid}: duplicate theme IDs.")
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError(f"{qid}: assignments must exactly partition retained mentions.")
        if any(a["theme_id"] not in themes for a in assignments[qid]):
            raise ValueError(f"{qid}: assignment has an unknown theme.")
    expected_findings = compute_findings(assignments, records, plan, definitions)
    # Recompute all computed fields, not just totals. Existing narrative display
    # fields may change; all original arithmetic and membership must agree.
    for expected in expected_findings["questions"]:
        actual = actual_questions[expected["question_id"]]
        for key, value in expected.items():
            if key not in {"themes", "high_intensity_outliers"} and actual.get(key) != value:
                raise ValueError(f"{expected['question_id']}: stale findings field {key}.")
        actual_themes = {t["theme_id"]: t for t in actual["themes"]}
        if len(actual_themes) != len(actual["themes"]) or set(actual_themes) != {t["theme_id"] for t in expected["themes"]}:
            raise ValueError("Findings theme membership changed.")
        for theme in expected["themes"]:
            for key, value in theme.items():
                if key != "sample_quotes" and actual_themes[theme["theme_id"]].get(key) != value:
                    raise ValueError(f"{theme['theme_id']}: stale findings field {key}.")
    path_file = root / "synthesis/source-paths.json"
    paths = json.loads(path_file.read_text(encoding="utf-8")) if path_file.exists() else {}
    source_cache = {}

    def context_for(mention):
        fid = mention["source_id"]
        if fid not in source_cache:
            try:
                raw = (root / paths[fid]).read_bytes()
                text = raw.decode("utf-8")
                source_cache[fid] = (text, hashlib.sha256(raw).hexdigest(), _normalized_offsets(text))
            except (KeyError, OSError, UnicodeError):
                source_cache[fid] = None
        source = source_cache[fid]
        if source is None:
            return {"literal_verified": False, "reason": "source_unavailable"}
        return _source_context(source[0], source[1], mention.get("quote", ""), source[2])

    questions = []
    for qid, question in question_map.items():
        qmentions = {mid: m for mid, m in mentions.items() if m["question_id"] == qid}
        sources = {m["source_id"] for m in qmentions.values()}
        assignments_by_id = {a["mention_id"]: a["theme_id"] for a in assignments[qid]}
        stats = [_stat(f"{qid}:mentions", "mentions", qmentions, qmentions, qid),
                 _stat(f"{qid}:sources", "source_files", sources, sources, qid)]
        theme_rows, candidates = [], []
        nominations = definitions[qid].get("quote_nominations", {})
        if not isinstance(nominations, dict) or set(nominations) - {t["theme_id"] for t in definitions[qid]["themes"]}:
            raise ValueError(f"{qid}: invalid quote nominations.")
        for theme in actual_questions[qid]["themes"]:
            tid, mids = theme["theme_id"], theme["mention_ids"]
            nominee = nominations.get(tid)
            if nominee is not None and nominee not in mids:
                raise ValueError(f"{tid}: quote nominee is outside its assigned theme.")
            eligible = [qmentions[mid] for mid in mids if qmentions[mid].get("quote") and
                        5 <= len(qmentions[mid]["quote"].split()) <= 100]
            eligible.sort(key=lambda m: (m["mention_id"] != nominee,
                                        is_low_signal_quote(m["quote"]), looks_like_csv_row(m["quote"]),
                                        not m.get("notable_quote", False),
                                        not 15 <= len(m["quote"].split()) <= 80, m["mention_id"]))
            chosen, seen, per_source = [], set(), Counter()
            diverse = len({m["source_id"] for m in eligible}) > 1
            for m in eligible:
                normalized = normalize_text(m["quote"])
                if normalized in seen or (diverse and per_source[m["source_id"]] >= 2):
                    continue
                chosen.append(m)
                seen.add(normalized)
                per_source[m["source_id"]] += 1
                if len(chosen) == 3:
                    break
            for candidate_index, m in enumerate(chosen):
                candidates.append({"quote_id": m["mention_id"], "mention_id": m["mention_id"],
                                   "theme_id": tid, "source_id": m["source_id"], "quote": m["quote"],
                                   "attribution": m.get("quote_attribution", ""),
                                   "source_context": context_for(m) if candidate_index == 0 else
                                   {"literal_verified": False, "reason": "context_budget_not_requested"}})
            definition = next(t for t in definitions[qid]["themes"] if t["theme_id"] == tid)
            theme_rows.append({**definition, "demoted": theme.get("demoted", False),
                               "quote_candidate_ids": [m["mention_id"] for m in chosen]})
            theme_sources = {qmentions[mid]["source_id"] for mid in mids}
            stats.extend([_stat(f"{tid}:mentions", "mentions", mids, qmentions, qid),
                          _stat(f"{tid}:sources", "source_files", theme_sources, sources, qid)])
        compact = [{"mention_id": mid, "theme_id": assignments_by_id[mid], "source_id": m["source_id"],
                    **{key: m[key] for key in ("mention", "quote", "quote_attribution", "sentiment", "intensity", "notable_quote") if key in m}}
                   for mid, m in sorted(qmentions.items())]
        questions.append({"question_id": qid, "question_text": question["text"], "mentions": compact,
                          "themes": theme_rows, "stats": stats, "quote_candidates": candidates})
    inventory = {"version": VERSION, "findings_digest": _digest(findings), "source_file_ids": sorted(source_ids), "questions": questions}
    inventory["evidence_digest"] = _digest(inventory)
    return inventory


def section_model_view(inventory):
    """Project locked evidence without duplicating membership or quote text.

    This is a model input, not a replacement for the immutable inventory. Every
    mention and theme definition remains available. Statistics retain exact
    values; their membership can be reconstructed from mention/theme/source IDs.
    Only unrequested display-shortlist rows are omitted. Requested contexts,
    including failures, remain byte-for-byte equal as JSON values behind hashes.
    """
    if not _inventory_valid(inventory):
        raise ValueError("Inventory digest does not match locked evidence.")
    view = {"view_format": "section_model_view_v1",
            **{key: copy.deepcopy(inventory[key]) for key in
               ("version", "findings_digest", "evidence_digest", "source_file_ids")},
            "source_contexts": {}, "questions": []}
    for question in inventory["questions"]:
        projected = copy.deepcopy(question)
        projected["stats"] = [{key: copy.deepcopy(value) for key, value in stat.items()
                               if key not in {"numerator_ids", "denominator_ids"}}
                              for stat in question["stats"]]
        for theme in projected["themes"]:
            theme.pop("quote_candidate_ids", None)
        projected["quote_candidates"] = []
        for candidate in question["quote_candidates"]:
            context = candidate["source_context"]
            if context.get("reason") == "context_budget_not_requested":
                continue
            context_ref = _digest(context)
            view["source_contexts"][context_ref] = copy.deepcopy(context)
            projected["quote_candidates"].append({"quote_id": candidate["quote_id"],
                                                   "context_ref": context_ref})
        view["questions"].append(projected)
    return view


def _quality_ok(quality):
    return (isinstance(quality, dict) and
            all(type(quality.get(field)) is int and 2 <= quality[field] <= 3 for field in QUALITY_FIELDS) and
            isinstance(quality.get("reason"), str) and bool(quality["reason"].strip()))


def _strings(value, unique=False):
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value) and (not unique or len(set(value)) == len(value))


def validate_claim_quantities(text, referenced_stats):
    """Compatibility wrapper; all new admission uses the typed claim assessor."""
    from skill.scripts.claim_quantities import validate_quantities
    return validate_quantities(text, referenced_stats)


def _inventory_valid(inventory):
    payload = {key: value for key, value in inventory.items() if key != "evidence_digest"}
    return inventory.get("evidence_digest") == _digest(payload)


def admission_issue(kind, code, component, component_id, message, question_id=None):
    return {"kind": kind, "code": code, "component": component, "component_id": component_id,
            "question_id": question_id, "message": message}


def quality_issues(quality, *, reason=True):
    fields = set(QUALITY_FIELDS) | ({"reason"} if reason else set())
    if (not isinstance(quality, dict) or set(quality) != fields or
            any(type(quality.get(k)) is not int or not 0 <= quality[k] <= 3 for k in QUALITY_FIELDS) or
            (reason and (not isinstance(quality.get("reason"), str) or not quality["reason"].strip()))):
        return [("hard", "quality_structure", "Quality needs the documented integer scores and a nonempty reason when required.")]
    if any(quality[k] < 2 for k in QUALITY_FIELDS):
        return [("editorial", "low_quality", "Omit components scoring below 2 on any quality dimension.")]
    return []


def _inline_quotes(text):
    spans, unmatched = quoted_spans(text)
    return [normalize_text(s["quote"]) for s in spans] + [normalize_text(s) for s in _blockquote_texts(text)], unmatched


def build_source_observation(question, mention_id, claim_id, quality):
    """Create a typed case by exact reference; selection remains a semantic choice."""
    from skill.scripts.claim_quantities import source_observation_fields, assess_claim_quantities
    if not isinstance(question, dict) or not isinstance(question.get("mentions"), list):
        raise ValueError("Source observation needs a locked question inventory.")
    matches = [m for m in question["mentions"] if isinstance(m, dict) and m.get("mention_id") == mention_id]
    if len(matches) != 1:
        raise ValueError("Source observation must select exactly one mention from this question.")
    claim = {"claim_id": claim_id, **source_observation_fields(matches[0]), "quality": copy.deepcopy(quality)}
    errors = assess_claim_quantities(claim, question)["hard_errors"]
    if errors:
        raise ValueError("Source observation: " + "; ".join(errors))
    return claim


def _inline_quotes_allowed(claim, question, checked):
    """A copied source case can retain literal fragments of its own locked quote."""
    source_quote = None
    if "source_observation" in claim:
        from skill.scripts.claim_quantities import assess_claim_quantities
        if not assess_claim_quantities(claim, question)["hard_errors"]:
            mid = claim["source_observation"]["mention_id"]
            mention = next(m for m in question["mentions"] if m["mention_id"] == mid)
            source_quote = normalize_text(mention.get("quote", ""))
    for field in ("text", "scope"):
        if not isinstance(claim.get(field), str):
            continue  # Structural admission reports this separately.
        quoted, unmatched = _inline_quotes(claim[field])
        if unmatched:
            return False
        for text in quoted:
            literal_source_fragment = (field == "text" and source_quote and text and
                                       re.search(r"(?<!\w)" + re.escape(text) + r"(?!\w)", source_quote))
            allowed = bool(literal_source_fragment) if source_quote is not None and field == "text" else text in checked
            if not allowed:
                return False
    return True


def assess_section_packets(draft, inventory):
    """Typed complete raw preflight: hard failures cannot be hidden by abstention."""
    from skill.scripts.claim_quantities import assess_claim_quantities
    issues = []
    def add(kind, code, component, cid, message, qid=None):
        issues.append(admission_issue(kind, code, component, cid, (f"{qid}: " if qid else "") + message, qid))
    if not _inventory_valid(inventory):
        add("hard", "stale_inventory", "document", None, "Inventory digest does not match locked evidence.")
        return issues
    if not isinstance(draft, dict) or set(draft) != {"questions"} or not isinstance(draft["questions"], list):
        add("hard", "structure", "document", None, "Section draft must contain only a questions array.")
        return issues
    questions = {q["question_id"]: q for q in inventory["questions"]}
    rows = draft["questions"]
    if (any(not isinstance(r, dict) or not isinstance(r.get("question_id"), str) for r in rows) or
            len(rows) != len(questions) or {r["question_id"] for r in rows} != set(questions)):
        add("hard", "question_coverage", "document", None, "Sections must cover every question exactly once.")
        return issues
    for row in rows:
        qid = row["question_id"]
        question = questions[qid]
        def emit(kind, code, component, cid, message):
            add(kind, code, component, cid, message, qid)
        if set(row) != set(SECTION_FORMAT["questions"][0]):
            emit("hard", "structure", "section", qid, "Section fields must match SECTION_FORMAT exactly.")
            continue
        mentions = {m["mention_id"]: m for m in question["mentions"]}
        themes = {t["theme_id"]: t for t in question["themes"]}
        quotes = {q["quote_id"]: q for q in question["quote_candidates"]}
        stats = {s["stat_ref"]: s for s in question["stats"]}
        if not isinstance(row["status"], str) or row["status"] not in STATUSES or ((row["status"] == "no_evidence") != (not mentions)):
            emit("hard", "status", "section", qid, "Status must match evidence availability.")
        if not _strings(row["omission_reasons"]) or not _strings(row["gaps"]):
            emit("hard", "structure", "section", qid, "Omission reasons and gaps must be text arrays.")
        raw_claims = row["claims"]
        if not isinstance(raw_claims, list) or any(not isinstance(c, dict) for c in raw_claims):
            emit("hard", "structure", "section", qid, "Claims must be an array of objects.")
            raw_claims = []
        claims = {}
        claim_fields = set(SECTION_FORMAT["questions"][0]["claims"][0])
        for claim in raw_claims:
            cid = claim.get("claim_id")
            if not isinstance(cid, str) or not re.fullmatch(re.escape(qid) + r"_C[1-9]\d*", cid) or cid in claims:
                emit("hard", "claim_identity", "claim", cid, "Claims need unique question-scoped IDs such as Q1_C1.")
                continue
            claims[cid] = claim
            if not claim_fields <= set(claim) or set(claim) - claim_fields - {"count_groups", "source_observation"}:
                emit("hard", "structure", "claim", cid, "Claim fields must match SECTION_FORMAT with optional count_groups or source_observation.")
                continue
            if not isinstance(claim["type"], str) or claim["type"] not in {"observation", "implication", "hypothesis"} or not all(isinstance(claim[k], str) and claim[k].strip() for k in ("text", "scope")):
                emit("hard", "structure", "claim", cid, "Claim needs type, text and explicit scope.")
            for field, allowed in (("theme_ids", themes), ("support_mention_ids", mentions), ("counterevidence_ids", mentions), ("stat_refs", stats)):
                if not _strings(claim[field], unique=True) or set(claim[field]) - set(allowed):
                    emit("hard", "reference", "claim", cid, f"Invalid {field}.")
            if not claim["support_mention_ids"] or not claim["theme_ids"]:
                emit("hard", "reference", "claim", cid, "Accepted claims need support and theme IDs.")
            if _strings(claim["support_mention_ids"]) and _strings(claim["theme_ids"]):
                if any(mid in mentions and mentions[mid]["theme_id"] not in claim["theme_ids"] for mid in claim["support_mention_ids"]):
                    emit("hard", "reference", "claim", cid, "Support must belong to named themes.")
            if _strings(claim["support_mention_ids"]) and _strings(claim["counterevidence_ids"]) and set(claim["support_mention_ids"]) & set(claim["counterevidence_ids"]):
                emit("hard", "reference", "claim", cid, "The same mention cannot be support and counterevidence.")
            if _strings(claim["stat_refs"]) and _strings(claim["theme_ids"]) and any(ref.split(":")[0] not in {qid, *claim["theme_ids"]} for ref in claim["stat_refs"]):
                emit("hard", "reference", "claim", cid, "Statistic references must share the claim's theme scope.")
            quantities = assess_claim_quantities(claim, question)
            for kind in ("hard", "editorial"):
                for error in quantities[kind + "_errors"]:
                    emit(kind, "quantity_" + kind, "claim", cid, error)
            for kind, code, message in quality_issues(claim["quality"]):
                emit(kind, code, "claim", cid, message)
        summary = row["summary_claim_id"]
        if summary is not None and (not isinstance(summary, str) or summary not in claims):
            emit("hard", "reference", "section", qid, "Summary must reference an accepted claim or null.")
        for field in ("takeaway_claim_ids", "exec_candidates"):
            if not _strings(row[field], unique=True) or set(row[field]) - set(claims):
                emit("hard", "reference", "section", qid, f"{field} must reference accepted claims only.")
        if isinstance(row["takeaway_claim_ids"], list) and len(row["takeaway_claim_ids"]) > 3:
            emit("hard", "structure", "section", qid, "At most three takeaways are allowed.")
        if not isinstance(row["theme_quote_ids"], dict) or not isinstance(row["quote_reviews"], dict):
            emit("hard", "structure", "section", qid, "theme_quote_ids and quote_reviews must be mappings.")
            continue
        selected = set()
        for tid, ids in row["theme_quote_ids"].items():
            if tid not in themes or not _strings(ids, unique=True) or len(ids) > 3:
                emit("hard", "reference", "section", qid, "Invalid theme quote selection.")
                continue
            selected.update(ids)
            if any(mid not in quotes or quotes[mid]["theme_id"] != tid for mid in ids):
                emit("hard", "reference", "section", qid, "Quote must belong to its named theme and question.")
        lead = row["lead_quote_id"]
        if lead is not None:
            if not isinstance(lead, str) or lead not in quotes:
                emit("hard", "reference", "section", qid, "Leading quote must be a candidate ID or null.")
            else:
                selected.add(lead)
                if themes[quotes[lead]["theme_id"]].get("demoted"):
                    emit("editorial", "demoted_lead", "lead_quote", lead, "A demoted outlier quote cannot imply a leading section pattern.")
        if set(row["quote_reviews"]) != selected:
            emit("hard", "reference", "section", qid, "Review exactly the selected quotes; no rejected draft may enter the handoff.")
        for mid in sorted(selected):
            quote, review = quotes.get(mid), row["quote_reviews"].get(mid)
            if not quote or not isinstance(review, dict) or set(review) != set(QUOTE_REVIEW_FORMAT):
                emit("hard", "structure", "quote", mid, "Missing candidate or malformed quote review.")
                continue
            context = quote["source_context"]
            check = review["context_check_id"]
            if check not in (None, "") and check != context.get("context_check_id"):
                emit("hard", "stale_context", "quote", mid, "Stale source-context check ID.")
            elif not context.get("literal_verified") or not check:
                emit("editorial", "missing_context", "quote", mid, "Missing source-context check; omit the quote.")
            if type(review["context_sufficient"]) is not bool:
                emit("hard", "structure", "quote", mid, "context_sufficient must be boolean.")
            elif not review["context_sufficient"]:
                emit("editorial", "insufficient_context", "quote", mid, "Insufficient context; omit the quote.")
            for kind, code, message in quality_issues(review["quality"]):
                emit(kind, code, "quote", mid, message)
            for field in ("speaker", "claim_owner"):
                value = review[field]
                if not isinstance(value, str) or not value.strip() or value.lower().strip() in {"unknown", "unclear", "n/a"}:
                    emit("hard", "unknown_speaker", "quote", mid, f"{field} must be established from context.")
            speaker = review["speaker"]
            known = context.get("text") or quote["attribution"]
            if context.get("speaker_turn"):
                known += "\n" + context["speaker_turn"]["header"]
            if isinstance(speaker, str) and speaker and speaker != "anonymous respondent" and normalize_text(speaker) not in normalize_text(known):
                emit("hard", "unknown_speaker", "quote", mid, "Speaker is absent from the checked context.")
            # The reviewed speaker and displayed label must agree. A nearby
            # name's presence alone cannot validate a different person's label.
            # Withhold a conflicting display quote without changing its count.
            displayed = re.sub(r"\s*\([^()]*\)\s*$", "", quote["attribution"]).strip().split(",", 1)[0]
            anonymous = normalize_text(displayed).startswith(("unnamed", "anonymous", "unknown"))
            if isinstance(speaker, str) and speaker and speaker != "anonymous respondent" and not anonymous:
                if normalize_text(speaker) not in normalize_text(displayed) and normalize_text(displayed) not in normalize_text(speaker):
                    emit("editorial", "speaker_label_conflict", "quote", mid, "Reviewed speaker and displayed attribution disagree; quote withheld.")
            turn = context.get("speaker_turn")
            if turn and not anonymous and normalize_text(displayed) not in normalize_text(turn["header"]):
                emit("editorial", "speaker_turn_conflict", "quote", mid, "Displayed attribution disagrees with the source turn; quote withheld.")
        checked = {normalize_text(quotes[mid]["quote"]) for mid in selected if mid in quotes}
        for cid, claim in claims.items():
            if not _inline_quotes_allowed(claim, question, checked):
                emit("editorial", "inline_quote", "claim", cid, "Inline quotes must reuse selected, context-checked quotes or literal fragments of the exact typed source observation's own retained quote.")
        if (summary is None or not row["takeaway_claim_ids"] or lead is None) and not row["omission_reasons"]:
            emit("hard", "structure", "section", qid, "Record a concise reason for omitted commentary or leading quote.")
    return issues


def validate_section_packets(draft, inventory):
    return [issue["message"] for issue in assess_section_packets(draft, inventory)]


def admit_section_packets(draft, inventory):
    issues = assess_section_packets(draft, inventory)
    hard = [i for i in issues if i["kind"] == "hard"]
    if hard:
        raise ValueError("Section evidence: " + "; ".join(i["message"] for i in hard))
    accepted = copy.deepcopy(draft)
    omissions = list(issues)
    for row in accepted["questions"]:
        qid = row["question_id"]
        local = [i for i in issues if i["question_id"] == qid]
        bad_quotes = {i["component_id"] for i in local if i["component"] == "quote"}
        if row["lead_quote_id"] in bad_quotes or any(i["component"] == "lead_quote" for i in local):
            row["lead_quote_id"] = None
        row["theme_quote_ids"] = {tid: [mid for mid in ids if mid not in bad_quotes] for tid, ids in row["theme_quote_ids"].items()}
        selected = {mid for ids in row["theme_quote_ids"].values() for mid in ids} | ({row["lead_quote_id"]} if row["lead_quote_id"] else set())
        row["quote_reviews"] = {mid: review for mid, review in row["quote_reviews"].items() if mid in selected}
        bad_claims = {i["component_id"] for i in local if i["component"] == "claim"}
        # One deterministic dependency closure: removed quote -> inline claim -> selected prose.
        question = next(q for q in inventory["questions"] if q["question_id"] == qid)
        checked = {normalize_text(q["quote"]) for q in question["quote_candidates"] if q["quote_id"] in selected}
        for claim in row["claims"]:
            if claim["claim_id"] in bad_claims:
                continue
            if not _inline_quotes_allowed(claim, question, checked):
                bad_claims.add(claim["claim_id"])
                omissions.append(admission_issue("editorial", "quote_dependency", "claim", claim["claim_id"], "Referenced quote was withheld.", qid))
        row["claims"] = [c for c in row["claims"] if c["claim_id"] not in bad_claims]
        if row["summary_claim_id"] in bad_claims:
            row["summary_claim_id"] = None
        for field in ("takeaway_claim_ids", "exec_candidates"):
            row[field] = [cid for cid in row[field] if cid not in bad_claims]
        messages = [i["message"] for i in omissions if i["question_id"] == qid]
        if messages:
            row["omission_reasons"] = list(dict.fromkeys([*row["omission_reasons"], *messages]))
            if not row["claims"] and question["mentions"]:
                row["status"] = "context_missing" if any(i["code"] == "missing_context" for i in local) else "thin"
    errors = validate_section_packets(accepted, inventory)
    if errors:
        raise ValueError("Accepted section evidence: " + "; ".join(errors))
    receipt = {"version": 1, "raw_digest": _digest(draft), "accepted_digest": _digest(accepted),
               "evidence_digest": inventory["evidence_digest"], "omissions": omissions, "accepted": accepted}
    receipt["admission_digest"] = _digest(receipt)
    return receipt


def build_section_packets(draft, inventory):
    """Validate once and derive a compact, accepted-only executive handoff."""
    admission = admit_section_packets(draft, inventory)
    draft = admission["accepted"]
    by_question = {q["question_id"]: q for q in inventory["questions"]}
    packets = copy.deepcopy(draft)
    packets["version"] = VERSION
    packets["evidence_digest"] = inventory["evidence_digest"]
    packets["admission_digest"] = admission["admission_digest"]
    for packet in packets["questions"]:
        question = by_question[packet["question_id"]]
        mentions = {m["mention_id"]: m for m in question["mentions"]}
        for claim in packet["claims"]:
            from skill.scripts.claim_quantities import assess_claim_quantities
            claim["claim_stats"] = assess_claim_quantities(claim, question)["stats"]
            claim["source_file_ids"] = sorted({mentions[mid]["source_id"] for mid in claim["support_mention_ids"]})
            claim["counterevidence_source_file_ids"] = sorted({mentions[mid]["source_id"] for mid in claim["counterevidence_ids"]})
            claim["counterevidence"] = [{key: mentions[mid][key] for key in ("mention_id", "source_id", "mention", "sentiment", "intensity") if key in mentions[mid]}
                                        for mid in claim["counterevidence_ids"]]
        packet["stats"] = copy.deepcopy(question["stats"])
        packet["evidence_source_file_ids"] = sorted({m["source_id"] for m in question["mentions"]})
        # Preserve full evidence qualifications/countercases separately from display
        # selection, without passing full quotes/transcripts to the executive stage.
        packet["evidence_inventory"] = [{key: m[key] for key in ("mention_id", "theme_id", "source_id", "mention", "sentiment", "intensity") if key in m}
                                        for m in question["mentions"]]
    packets["packet_digest"] = _digest(packets)
    return packets


def _check_packets(packets, inventory):
    if not _inventory_valid(inventory) or packets.get("evidence_digest") != inventory["evidence_digest"]:
        raise ValueError("Section packets refer to stale evidence.")
    if packets.get("packet_digest") != _digest({k: v for k, v in packets.items() if k != "packet_digest"}):
        raise ValueError("Accepted section packets changed after lock.")


def section_presentation(packet):
    """Choose visible accepted claims without rewriting or discarding evidence.

    Exact normalized repetitions are removed mechanically. Meaningfully different
    paraphrases remain the writer's responsibility; this is not a semantic audit.
    Distinct scope on identical text remains visible in additional insights, where
    scope is displayed, instead of creating indistinguishable takeaways.
    """
    claims = {c["claim_id"]: c for c in packet["claims"]}
    summary = packet["summary_claim_id"]
    shown, text_keys, complete_keys, takeaways, additional, duplicates = set(), set(), set(), [], [], []

    def keys(cid):
        claim = claims[cid]
        normalize = lambda value: re.sub(r"[^\w]+", " ", normalize_text(value).casefold()).strip()
        text = (claim["type"], normalize(claim["text"]))
        return text, (*text, normalize(claim["scope"]))

    def select(cid):
        text, complete = keys(cid)
        shown.add(cid)
        text_keys.add(text)
        complete_keys.add(complete)

    if summary:
        select(summary)
    for cid in packet["takeaway_claim_ids"]:
        if cid not in shown and keys(cid)[0] not in text_keys and len(takeaways) < 3:
            takeaways.append(cid)
            select(cid)
    for cid in claims:
        if cid in shown:
            continue
        if keys(cid)[1] in complete_keys and "source_observation" not in claims[cid]:
            duplicates.append(cid)
            continue
        additional.append(cid)
        select(cid)
    return {"summary_claim_id": summary, "takeaway_claim_ids": takeaways,
            "additional_claim_ids": additional, "duplicate_claim_ids": duplicates}


def claim_display_text(claim, *, include_scope=False):
    """Copy admitted prose and keep its qualification with every visible claim."""
    prefix = "Hypothesis: " if claim["type"] == "hypothesis" else "Implication: " if claim["type"] == "implication" else ""
    text = prefix + claim["text"]
    return text + " Scope: " + claim["scope"] if include_scope else text


def report_questions(packets, inventory):
    _check_packets(packets, inventory)
    questions = {q["question_id"]: q for q in inventory["questions"]}
    result = []
    for packet in packets["questions"]:
        claims = {c["claim_id"]: c for c in packet["claims"]}
        presentation = section_presentation(packet)
        result.append({"question_id": packet["question_id"], "question_text": questions[packet["question_id"]]["question_text"],
                       "synthesis": claim_display_text(claims[packet["summary_claim_id"]], include_scope=True) if packet["summary_claim_id"] else None,
                       "takeaways": [claim_display_text(claims[cid], include_scope=True) for cid in presentation["takeaway_claim_ids"]],
                       "additional_insights": [{"text": claim_display_text(claims[cid]), "scope": claims[cid]["scope"]}
                                               for cid in presentation["additional_claim_ids"]],
                       "lead_quote_id": packet["lead_quote_id"]})
    return result


def selected_findings(packets, inventory, findings):
    """Copy findings and change quotation display only; every count remains intact."""
    _check_packets(packets, inventory)
    if inventory.get("findings_digest") != _digest(findings):
        raise ValueError("Findings changed after the section evidence inventory was locked.")
    output = copy.deepcopy(findings)
    packets_by_id = {q["question_id"]: q for q in packets["questions"]}
    inventories = {q["question_id"]: q for q in inventory["questions"]}
    for question in output["questions"]:
        packet = packets_by_id[question["question_id"]]
        evidence = inventories[question["question_id"]]
        candidates = {q["quote_id"]: q for q in evidence["quote_candidates"]}
        mentions = {m["mention_id"]: m for m in evidence["mentions"]}
        selected = set()
        for theme in question["themes"]:
            ids = list(packet["theme_quote_ids"].get(theme["theme_id"], []))
            lead = packet["lead_quote_id"]
            if lead and candidates[lead]["theme_id"] == theme["theme_id"] and lead not in ids:
                ids.append(lead)
            selected.update(ids)
            theme["sample_quotes"] = [{"mention_id": mid, "quote": candidates[mid]["quote"],
                                        "attribution": candidates[mid]["attribution"], "file_id": candidates[mid]["source_id"],
                                        "notable": bool(mentions[mid].get("notable_quote")), "mention": mentions[mid].get("mention", ""),
                                        "sentiment": mentions[mid].get("sentiment", "")} for mid in ids]
        for outlier in question["high_intensity_outliers"]:
            if outlier["mention_id"] not in selected:
                outlier["quote"] = ""
                outlier.pop("display_quote", None)
    return output

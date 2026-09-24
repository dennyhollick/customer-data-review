"""One bounded analysis draft and one writing draft; all checks stay deterministic."""

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from skill.schemas.validate import load_schema, validate_json
from skill.scripts.assemble_methodology import assemble_methodology
from skill.scripts.bounded_audit import read_json, read_records, verify_gate
from skill.scripts.compute_findings import compute_findings, preserve_display_quotes
from skill.scripts.generate_html import generate_html
from skill.scripts.generate_markdown import generate_markdown
from skill.scripts.generate_opportunities import FIELDS, generate_opportunities, render_opportunities
from skill.scripts.run_control import RunControl, atomic_json, file_hash, validate_execution_mode
from skill.scripts.validate_assignments import check_distribution, validate_assignments
from skill.scripts.validate_html import validate_html
from skill.scripts.validate_markdown import validate_markdown
from skill.scripts.validate_merge import validate_merge
from skill.scripts.validate_report import validate_report
from skill.scripts.validate_themes import check_theme_budget, normalize_themes, validate_themes
from skill.scripts.verify_attributions import verify_attributions
from skill.scripts.verify_crossref import verify_crossrefs
from skill.scripts.verify_report_evidence import verify_report_evidence

PLAN = "synthesis/plan.json"
RETAINED = "synthesis/audit/validated_mentions.jsonl"
SUMMARY = "synthesis/audit/audit-summary.json"
FINDINGS = "synthesis/output/findings.json"
REPORT = "synthesis/output/report.json"
CANDIDATES = "synthesis/output/opportunities.json"
WRITING_INPUTS = [PLAN, RETAINED, SUMMARY, FINDINGS]
INVENTORY = "synthesis/output/section-inventory.json"
PACKETS = "synthesis/output/section-packets.json"
DISPLAY = "synthesis/output/display-findings.json"
NOMINATIONS = "synthesis/output/quote-nominations.json"


def _packet_mode(root):
    return read_json(root / "synthesis/request.json").get("writing_mode") == "section_packets_v1"


def _executive_mode(root):
    request = read_json(root / "synthesis/request.json")
    mode = request.get("executive_mode", "legacy")
    if mode not in ("legacy", "qualitative_annotations_v1"):
        raise ValueError("Unknown executive_mode.")
    if mode != "legacy" and request.get("writing_mode") != "section_packets_v1":
        raise ValueError("Qualitative executive annotations require section_packets_v1.")
    return mode


def _root(value):
    return Path(value).resolve()


def _gate(root):
    result = verify_gate(root)
    if not result["complete"]:
        raise ValueError(result["reason"])
    return result


def _require_stage(state, key):
    if state["steps"].get(key, {}).get("status") != "complete":
        raise ValueError(f"Complete validated {key} before starting this stage.")


def _prepare(root, key, inputs, repair=False):
    control = RunControl(root)
    state = control.status()
    old = state["steps"].get(key)
    if old and old["status"] == "complete":
        return {"action": "reuse", "outputs": sorted(old["outputs"])}
    if old and not repair:
        if state["status"] not in ("active", "exhausted"):
            raise ValueError(f"Cannot resume work on a {state['status']} run.")
        if control._hash_paths(inputs) != old["inputs"]:
            raise ValueError("Inputs changed during the reserved stage; do not reuse stale work.")
        return {"action": "resume", "attempt": old["attempts"]}
    if repair and old is None:
        raise ValueError("No initial attempt exists to repair.")
    return control.claim(key, inputs)


def _submission(root, key, input_path):
    """Freeze each submitted model answer before validation, including bad JSON.

    A changed answer consumes the explicit repair claim. Re-running deterministic
    validation on identical bytes does not create a new model attempt.
    """
    control = RunControl(root)
    state = control.status()
    step = state["steps"].get(key)
    if step and step["status"] == "complete":
        return control, None, None
    if not step or step["status"] != "running":
        raise ValueError(f"Run prepare for {key} before drafting or finishing.")
    if state["status"] not in ("active", "exhausted"):
        raise ValueError(f"Cannot finish a {state['status']} run.")
    if key in ("phase-3", "sections") and _partition_mode(root):
        from skill.scripts.batch_partitions import verify_assembly
        verify_assembly(root, key, input_path)
    path = (root / input_path).resolve()
    path.relative_to(root)
    frozen = root / "synthesis/batch" / f"{key}-attempt-{step['attempts']}.json"
    content = path.read_bytes()
    anchor_key = f"{key}:{step['attempts']}"
    anchors = state.setdefault("batch_submissions", {})
    expected = {"path": str(frozen.relative_to(root)), "sha256": hashlib.sha256(content).hexdigest()}
    if anchor_key in anchors:
        if not frozen.is_file() or file_hash(frozen) != anchors[anchor_key]["sha256"]:
            raise ValueError("Changed/missing frozen model submission; never restore an attempt by deleting its bytes.")
        if anchors[anchor_key] != expected:
            raise ValueError("The submitted answer changed. Use prepare with --repair before replacing it; each stage allows an initial draft plus two repairs.")
    else:
        if frozen.exists() and frozen.read_bytes() != content:
            raise ValueError("The submitted answer changed. Use prepare with --repair before replacing it; each stage allows an initial draft plus two repairs.")
        # Anchor the exact bytes before parsing. A missing copy after an interrupted write
        # fails closed; a preserved copy can replay deterministically without another draft.
        anchors[anchor_key] = expected
        atomic_json(control.path, state)
        if not frozen.exists():
            _atomic_text(frozen, content)
    return control, json.loads(content), str(frozen.relative_to(root))


def _atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".batch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value if isinstance(value, bytes) else value.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _shape(value, required, optional=(), label="object"):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError(f"{label}: use exactly required fields {sorted(required)} and optional fields {sorted(optional)}.")


def _questions(rows, plan):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("questions must be an array of objects.")
    expected = {q["id"] for q in plan["questions"]}
    actual = [row.get("question_id") for row in rows]
    if any(not isinstance(qid, str) for qid in actual) or len(set(actual)) != len(actual) or set(actual) != expected:
        raise ValueError("questions must contain every locked question exactly once, with no extra question IDs.")
    return {row["question_id"]: row for row in rows}


def _raise_errors(issues, label):
    errors = [issue["message"] if isinstance(issue, dict) else issue
              for issue in issues if not isinstance(issue, dict) or issue.get("severity") == "error"]
    if errors:
        raise ValueError(f"{label}: " + "; ".join(errors))


def _partition_mode(root):
    return validate_execution_mode(read_json(_root(root) / "synthesis/request.json")) == "question_partitions_v1"


def _reject_combined_after_parts(root, stage, input_path=None, assembled=None):
    control = RunControl(_root(root))
    if not control.path.exists():
        return  # later checks report the missing run state
    state = control._read()
    if stage not in state.get("question_parts", {}):
        return
    if input_path is None or Path(input_path).resolve() != (_root(root) / assembled).resolve():
        raise ValueError(f"This run drafts {stage} one question at a time; use --question.")
    from skill.scripts import question_parts as parts
    plan = read_json(_root(root) / PLAN)
    if not all(parts.accepted(state, stage, q["id"]) for q in plan["questions"]):
        raise ValueError(f"Accept every question with --question before {stage} assembles.")
    expected = {"questions": [parts.load(root, stage, q["id"]) for q in plan["questions"]]}
    if read_json(Path(input_path)) != expected:
        raise ValueError("The assembled file must match the accepted questions exactly; use --question.")


def prepare_analysis(root, repair=False):
    _reject_combined_after_parts(root, "phase-3")
    if _partition_mode(root):
        raise ValueError("Use batch_partitions prepare for partition-mode analysis.")
    return _prepare_analysis(root, repair)


def _prepare_analysis(root, repair=False):
    root = _root(root)
    _gate(root)
    prepared = _prepare(root, "phase-3", [PLAN, RETAINED, SUMMARY], repair)
    if prepared["action"] == "reuse":
        return prepared
    plan, records = read_json(root / PLAN), read_records(root / RETAINED)
    questions = []
    for question in plan["questions"]:
        mentions = [{"file_id": row["file_id"], **mention}
                    for row in records for mention in row["mentions"] if mention["question_id"] == question["id"]]
        questions.append({**question, "mentions": mentions})
    return {**prepared, "business_context": plan["business_context"], "questions": questions,
            "format": {"questions": [{"question_id": "Q1", "themes": ["theme objects matching theme_schema"],
                                       "assignments": [{"mention_id": "source_Q1_0", "theme_id": "Q1_T1"}],
                                       "quote_nominations": {"Q1_T1": "source_Q1_0"}}]},
            "theme_schema": load_schema("themes")["properties"]["themes"]["items"],
            "rules": ["Create themes and assignments together in one pass, covering every question exactly once.",
                      "Assign every retained mention exactly once to a theme belonging to its own question.",
                      "Use empty themes and assignments for an empty question; never invent evidence.",
                      "Themes must answer this question's dimension: pain, previous workflow, request, competitor, choice, discovery, objection or demo response. Do not substitute a pain taxonomy for a discovery question.",
                      "Give themes meaningful include/exclude boundaries; preserve contradictions, rare risks and uncertainty (web search does not prove organic search).",
                      "Optionally nominate one promising quote mention_id per theme from the entire inventory; no extra draft or retrieval here. Quality can justify no nomination.",
                      "Use only supplied evidence. Distribution and word-count warnings do not trigger more passes."]}


ANALYSIS_ASSEMBLED = "synthesis/batches/analysis-assembled.json"
ANALYSIS_CHAR_CAP = 240_000   # about 60k tokens of mention text per model input
ASSIGN_CHUNK = 300            # mentions per assignment chunk for very large questions


def _question_mentions(plan, records, qid):
    if qid not in {q["id"] for q in plan["questions"]}:
        raise ValueError(f"Unknown locked question {qid}.")
    return [{"mention_id": m["mention_id"], "mention": m["mention"], "quote": m["quote"],
             "sentiment": m.get("sentiment"), "source_id": row["file_id"], "notable_quote": m.get("notable_quote", False)}
            for row in records for m in row["mentions"] if m["question_id"] == qid]


def _compact(mentions, with_quote=True):
    keys = ("mention_id", "mention", "quote", "sentiment") if with_quote else ("mention_id", "mention")
    return [{k: m[k] for k in keys} for m in mentions]


def _chunked(mentions):
    return len(json.dumps(_compact(mentions), ensure_ascii=False)) > ANALYSIS_CHAR_CAP


def _theme_sample(mentions):
    """A spread of examples for drafting themes when a question is very large."""
    from skill.scripts.concise_workflow import spread_sample
    chosen, size = [], 2
    for m in spread_sample(mentions, lambda m: m["source_id"]):
        size += len(json.dumps(_compact([m]), ensure_ascii=False))
        if size > ANALYSIS_CHAR_CAP:
            break
        chosen.append(m)
    return chosen


def _chunks(mentions):
    ids = sorted(m["mention_id"] for m in mentions)
    return [ids[i:i + ASSIGN_CHUNK] for i in range(0, len(ids), ASSIGN_CHUNK)]


_ANALYSIS_RULES = [
    "Themes must answer this question's dimension: pain, previous workflow, request, competitor, choice, discovery, objection or demo response. Do not substitute a pain taxonomy for a discovery question.",
    "Give themes meaningful include/exclude boundaries; preserve contradictions, rare risks and uncertainty.",
    "Distribution and word-count warnings do not trigger more passes."]


def prepare_analysis_question(root, qid):
    """Next bounded analysis task for one question (see phases/phase_3.md)."""
    from skill.scripts import question_parts as parts
    if _partition_mode(root):
        raise ValueError("Use batch_partitions prepare for partition-mode analysis.")
    root = _root(root)
    _gate(root)
    parts.open_stage(root, "phase-3")
    prepared = _prepare(root, "phase-3", [PLAN, RETAINED, SUMMARY])
    if prepared["action"] == "reuse":
        return prepared
    plan, records = read_json(root / PLAN), read_records(root / RETAINED)
    mentions = _question_mentions(plan, records, qid)
    state = RunControl(root).status()
    done = [q["id"] for q in plan["questions"] if parts.accepted(state, "phase-3", q["id"])]
    base = {**prepared, "question": next(q for q in plan["questions"] if q["id"] == qid),
            "business_context": plan["business_context"], "accepted_questions": done,
            "remaining_questions": [q["id"] for q in plan["questions"] if q["id"] not in done],
            "theme_schema": load_schema("themes")["properties"]["themes"]["items"]}
    if parts.accepted(state, "phase-3", qid):
        return {**base, "task": "done", "next": "Move to the next remaining question."}
    if not _chunked(mentions):
        return {**base, "task": "themes_and_assignments", "mentions": _compact(mentions),
                "format": {"question_id": qid, "themes": ["theme objects matching theme_schema"],
                           "assignments": [{"mention_id": f"source_{qid}_0", "theme_id": f"{qid}_T1"}],
                           "quote_nominations": {f"{qid}_T1": f"source_{qid}_0"}},
                "finish": f"finish-analysis --question {qid} --input analysis-{qid}.json",
                "rules": ["Create this question's themes and assignments together in one pass.",
                          "Assign every supplied mention exactly once to one of this question's themes.",
                          "Use empty themes and assignments when no mentions are supplied; never invent evidence.",
                          *_ANALYSIS_RULES,
                          "Optionally nominate one promising quote mention_id per theme."]}
    themes = parts.load(root, "phase-3", f"{qid}:themes")
    if themes is None:
        sample = _theme_sample(mentions)
        return {**base, "task": "themes", "mentions": _compact(sample),
                "mentions_note": f"This question has {len(mentions)} mentions, too many for one pass. These {len(sample)} are a spread across sources for drafting themes; you will assign every mention afterwards in chunks.",
                "format": {"question_id": qid, "themes": ["theme objects matching theme_schema"]},
                "finish": f"finish-analysis --question {qid} --part themes --input themes-{qid}.json",
                "rules": ["Draft themes that can hold every mention of this question, including an Other theme if needed.", *_ANALYSIS_RULES]}
    for index, chunk in enumerate(_chunks(mentions)):
        if not parts.accepted(state, "phase-3", f"{qid}:c{index}"):
            wanted = set(chunk)
            return {**base, "task": "assign", "chunk": index, "chunks_total": len(_chunks(mentions)),
                    "locked_themes": themes["themes"],
                    "mentions": _compact([m for m in mentions if m["mention_id"] in wanted], with_quote=False),
                    "format": {"question_id": qid, "assignments": [{"mention_id": "ID", "theme_id": f"{qid}_T1"}]},
                    "finish": f"finish-analysis --question {qid} --part c{index} --input assign-{qid}-c{index}.json",
                    "rules": ["Assign every supplied mention exactly once to one of the locked themes. Do not change themes."]}
    return {**base, "task": "assemble", "next": f"finish-analysis --question {qid} --part assemble"}


def finish_analysis_question(root, qid, input_path=None, part=None):
    """Validate one analysis part; accept the question when complete; assemble at the end."""
    from skill.scripts import question_parts as parts
    root = _root(root)
    plan, records = read_json(root / PLAN), read_records(root / RETAINED)
    mentions = _question_mentions(plan, records, qid)
    state = RunControl(root).status()
    if state["steps"].get("phase-3", {}).get("status") == "complete":
        return {"status": "reused", "stage": "phase-3"}
    if part in (None, "") and not _chunked(mentions):
        control, draft, frozen, _ = parts.submit(root, "phase-3", qid, input_path)
        if draft is not None:
            if isinstance(draft, dict) and set(draft) == {"questions"} and isinstance(draft["questions"], list) and len(draft["questions"]) == 1:
                draft = draft["questions"][0]
            validate_analysis_question(draft, qid, records)
            parts.accept(root, "phase-3", qid, draft, frozen)
    elif part in ("themes",) and not _chunked(mentions):
        raise ValueError(f"{qid} fits in one pass; finish it without --part.")
    elif part == "themes":
        control, draft, frozen, _ = parts.submit(root, "phase-3", f"{qid}:themes", input_path)
        if draft is not None:
            _shape(draft, {"question_id", "themes"}, label=f"{qid} themes")
            if draft["question_id"] != qid:
                raise ValueError(f"Themes must be for {qid}.")
            normalized, _ = normalize_themes({"question_id": qid, "themes": draft["themes"]})
            if [t.get("theme_id") for t in draft["themes"]] != [t.get("theme_id") for t in normalized["themes"]]:
                raise ValueError(f"{qid}: use valid {qid}_T<number> theme_id values.")
            _raise_errors(validate_themes(normalized, qid), qid)
            if not normalized["themes"]:
                raise ValueError(f"{qid} has mentions, so it needs at least one theme.")
            parts.accept(root, "phase-3", f"{qid}:themes", normalized, frozen)
    elif isinstance(part, str) and part.startswith("c") and part[1:].isdigit():
        themes = parts.load(root, "phase-3", f"{qid}:themes")
        if themes is None:
            raise ValueError(f"Accept {qid} themes before assigning chunks.")
        chunks = _chunks(mentions)
        index = int(part[1:])
        if index >= len(chunks):
            raise ValueError(f"{qid} has {len(chunks)} chunks.")
        control, draft, frozen, _ = parts.submit(root, "phase-3", f"{qid}:{part}", input_path)
        if draft is not None:
            _shape(draft, {"question_id", "assignments"}, label=f"{qid} chunk {index}")
            valid = {t["theme_id"] for t in themes["themes"]}
            _raise_errors(validate_json({"question_id": qid, "assignments": draft["assignments"]}, "assignments"), qid)
            _raise_errors(validate_assignments({"question_id": qid, "assignments": draft["assignments"]}, set(chunks[index]), valid), qid)
            parts.accept(root, "phase-3", f"{qid}:{part}", draft, frozen)
    elif part == "assemble":
        pass
    else:
        raise ValueError("Use --part themes, --part c<N> or --part assemble for a chunked question; omit --part otherwise.")
    state = RunControl(root).status()
    if _chunked(mentions) and not parts.accepted(state, "phase-3", qid):
        chunks = _chunks(mentions)
        if parts.accepted(state, "phase-3", f"{qid}:themes") and all(parts.accepted(state, "phase-3", f"{qid}:c{i}") for i in range(len(chunks))):
            themes = parts.load(root, "phase-3", f"{qid}:themes")
            full = {"question_id": qid, "themes": themes["themes"],
                    "assignments": [a for i in range(len(chunks)) for a in parts.load(root, "phase-3", f"{qid}:c{i}")["assignments"]],
                    "quote_nominations": {}}
            validate_analysis_question(full, qid, records)
            path = root / f"synthesis/batches/analysis-{qid}-assembled.json"
            atomic_json(path, full)
            control, draft, frozen, _ = parts.submit(root, "phase-3", qid, str(path.relative_to(root)))
            if draft is not None:
                parts.accept(root, "phase-3", qid, draft, frozen)
        else:
            return {"status": "accepted", "question_id": qid, "next": f"prepare-analysis --question {qid}"}
    state = RunControl(root).status()
    remaining = [q["id"] for q in plan["questions"] if not parts.accepted(state, "phase-3", q["id"])]
    if remaining:
        return {"status": "accepted", "question_id": qid, "remaining_questions": remaining}
    assembled = {"questions": [parts.load(root, "phase-3", q["id"]) for q in plan["questions"]]}
    atomic_json(root / ANALYSIS_ASSEMBLED, assembled)
    try:
        return finish_analysis(root, ANALYSIS_ASSEMBLED)
    except ValueError as exc:
        raise ValueError(f"All questions were accepted but assembly failed: {exc}. Write reports/incomplete.md and stop.") from exc


def validate_analysis_question(row, qid, records):
    """Pure question validation shared by combined and partitioned analysis."""
    if not isinstance(row, dict) or row.get("question_id") != qid:
        raise ValueError(f"{qid}: fragment question ownership mismatch.")
    _shape(row, {"question_id", "themes", "assignments"}, {"quote_nominations"}, label=qid)
    original = {"question_id": qid, "themes": row["themes"]}
    # Check shape first; normalization intentionally accepts include/exclude
    # lists but must never silently rename IDs already used in assignments.
    if not isinstance(row["themes"], list) or any(not isinstance(t, dict) for t in row["themes"]):
        raise ValueError(f"{qid}: themes must be an array of objects.")
    normalized, fixes = normalize_themes(original)
    if [t.get("theme_id") for t in original["themes"]] != [t.get("theme_id") for t in normalized["themes"]]:
        raise ValueError(f"{qid}: use valid {qid}_T<number> theme_id values in both themes and assignments; no implicit ID remapping.")
    _raise_errors(validate_themes(normalized, qid), qid)
    assignment = {"question_id": qid, "assignments": row["assignments"]}
    _raise_errors(validate_json(assignment, "assignments"), qid)
    expected = {m["mention_id"] for r in records for m in r["mentions"] if m["question_id"] == qid}
    valid_themes = {theme["theme_id"] for theme in normalized["themes"]}
    if not expected and normalized["themes"]:
        raise ValueError(f"{qid}: a question without retained evidence must have no themes.")
    _raise_errors(validate_assignments(assignment, expected, valid_themes), qid)
    _raise_errors(validate_merge(assignment["assignments"], expected, valid_themes), qid)
    nominees = row.get("quote_nominations", {})
    assigned = {a["mention_id"]: a["theme_id"] for a in row["assignments"]}
    if not isinstance(nominees, dict) or any(t not in valid_themes or not isinstance(m, str) or assigned.get(m) != t for t, m in nominees.items()):
        raise ValueError(f"{qid}: quote nominations must map own theme IDs to assigned mention IDs.")
    warnings = fixes + check_theme_budget(normalized, len(expected))
    warnings.extend(check_distribution(assignment, {t["theme_id"]: t["type"] for t in normalized["themes"]}))
    return normalized, assignment, nominees, warnings


def finish_analysis(root, input_path):
    root = _root(root)
    _reject_combined_after_parts(root, "phase-3", root / input_path, ANALYSIS_ASSEMBLED)
    control, draft, frozen = _submission(root, "phase-3", input_path)
    if draft is None:
        return {"status": "reused", "stage": "phase-3"}
    _gate(root)
    _shape(draft, {"questions"}, label="analysis draft")
    plan, records = read_json(root / PLAN), read_records(root / RETAINED)
    rows = _questions(draft["questions"], plan)
    definitions, assignments, warnings, artifacts, nominations = {}, {}, [], {}, {}
    for question in plan["questions"]:
        qid = question["id"]
        row = rows[qid]
        normalized, assignment, nominees, question_warnings = validate_analysis_question(row, qid, records)
        nominations[qid] = nominees
        warnings.extend(question_warnings)
        definitions[qid], assignments[qid] = normalized, assignment["assignments"]
        artifacts[f"synthesis/pipeline/themes_{qid}.json"] = normalized
        artifacts[f"synthesis/pipeline/merged_{qid}.json"] = assignment
    findings = compute_findings(assignments, records, plan, definitions)
    if (root / FINDINGS).exists():
        preserve_display_quotes(findings, read_json(root / FINDINGS))
    _raise_errors(validate_json(findings, "findings"), "findings")
    _questions(findings["questions"], plan)
    artifacts[FINDINGS] = findings
    artifacts[NOMINATIONS] = nominations
    for path, content in artifacts.items():
        atomic_json(root / path, content)
    control.finish("phase-3", [frozen, *artifacts, *_partition_outputs(root, "phase-3")])
    return {"status": "complete", "stage": "phase-3", "outputs": sorted(artifacts), "warnings": warnings}


def _partition_outputs(root, stage):
    if not _partition_mode(root):
        return []
    from skill.scripts.batch_partitions import verify_assembly
    return verify_assembly(root, stage)


def _definitions(root, plan):
    themes, assignments = {}, {}
    for question in plan["questions"]:
        qid = question["id"]
        themes[qid] = read_json(root / "synthesis/pipeline" / f"themes_{qid}.json")
        assignments[qid] = read_json(root / "synthesis/pipeline" / f"merged_{qid}.json")
    return themes, assignments


def prepare_writing(root, repair=False):
    root = _root(root)
    if _packet_mode(root):
        raise ValueError("Use prepare-sections, then prepare-executive for this locked writing mode.")
    state = RunControl(root).status()
    _require_stage(state, "phase-3")
    _gate(root)
    prepared = _prepare(root, "report", WRITING_INPUTS, repair)
    if prepared["action"] == "reuse":
        return prepared
    # Reserve the entire writing/delivery batch before the model starts. The
    # opportunity stage performs no model work and must be able to finish the
    # already-reserved batch if drafting overruns the soft launch-time budget.
    # A report repair never creates an additional opportunity attempt.
    _prepare(root, "opportunities", WRITING_INPUTS)
    plan, records, findings = read_json(root / PLAN), read_records(root / RETAINED), read_json(root / FINDINGS)
    schema = load_schema("report")["properties"]
    question_schema = copy.deepcopy(schema["questions"]["items"])
    question_schema["required"].remove("question_text")
    del question_schema["properties"]["question_text"]
    evidence = {m["mention_id"]: {"file_id": row["file_id"], **m} for row in records for m in row["mentions"]}
    paths = read_json(root / "synthesis/source-paths.json")
    # Reuse existing selected evidence instead of echoing the entire retained
    # corpus again. Include IDs so opportunities can point to exact assignments.
    for question in findings["questions"]:
        for theme in question["themes"]:
            for quote in theme["sample_quotes"]:
                matches = [mid for mid in theme["mention_ids"]
                           if evidence[mid]["quote"] == quote["quote"]
                           and evidence[mid]["file_id"] == quote["file_id"]
                           and evidence[mid]["quote_attribution"] == quote["attribution"]]
                if matches:
                    quote["mention_id"] = matches[0]
                    quote["source_path"] = paths[quote["file_id"]]
    return {**prepared, "business_context": plan["business_context"], "findings": findings,
            "audit": read_json(root / SUMMARY),
            "format": {"executive_summary": "object matching executive_schema", "questions": ["objects matching question_schema"],
                       "opportunities": [dict.fromkeys(sorted(FIELDS), "nonempty string")]},
            "executive_schema": schema["executive_summary"], "question_schema": question_schema,
            "rules": ["Draft all question syntheses, executive summary and 0-5 opportunity candidates together in one pass.",
                      "Do not supply question_text, methodology, quotes or counts as opportunity fields; the helper copies/computes them.",
                      "Use supplied counts and exact quotes. Do not invent titles, market facts or missing business context.",
                      "Make thin evidence and missing marketing/ownership context explicit; do not pad short sections.",
                      "Check each opportunity's selected evidence against source context already read; source_path identifies any necessary targeted reread.",
                      "Strategic hypotheses and next steps are proposals, not established facts.",
                      "No new audit, theme refinement or prose-polishing pass. Repairs are for hard errors only."]}


def finish_writing(root, input_path):
    root = _root(root)
    if _packet_mode(root):
        raise ValueError("Use finish-sections and finish-executive for this locked writing mode.")
    control, draft, frozen = _submission(root, "report", input_path)
    if draft is None:
        return {"status": "reused", "stage": "report"}
    _gate(root)
    _shape(draft, {"executive_summary", "questions", "opportunities"}, label="writing draft")
    plan, records, findings = read_json(root / PLAN), read_records(root / RETAINED), read_json(root / FINDINGS)
    rows = _questions(draft["questions"], plan)
    questions = []
    for question in plan["questions"]:
        row = rows[question["id"]]
        _shape(row, {"question_id", "synthesis", "takeaways"}, {"notable_outcomes"}, question["id"])
        questions.append({**row, "question_text": question["text"]})
    return _publish(root, control, draft, frozen, questions, findings)


def _publish(root, control, draft, frozen, questions, findings, additional_outputs=()):
    report, html, markdown, _, warnings = _validated_delivery(root, draft, questions, findings)
    artifacts = {REPORT: report, CANDIDATES: draft["opportunities"]}
    for question in questions:
        artifacts[f"synthesis/output/question_{question['question_id']}.json"] = question
    for path, content in artifacts.items():
        atomic_json(root / path, content)
    _atomic_text(root / "reports/report.html", html)
    _atomic_text(root / "reports/report.md", markdown)
    outputs = [frozen, *additional_outputs, *artifacts, "reports/report.html", "reports/report.md"]
    control.finish("report", outputs)
    return {"status": "complete", "stage": "report", "outputs": outputs, "warnings": warnings}


def _validated_delivery(root, draft, questions, findings):
    """Deterministic rendering only; does not reserve attempts or close a run."""
    _gate(root)
    plan, records = read_json(root / PLAN), read_records(root / RETAINED)
    report = {"executive_summary": draft["executive_summary"], "questions": questions,
              "methodology": assemble_methodology(plan, project_root=root)}
    _raise_errors(validate_json(report, "report"), "report schema")
    statistics = draft.get("admitted_statistics") if _packet_mode(root) else None
    provenance = ({"packets": read_json(root / PACKETS), "inventory": read_json(root / INVENTORY)}
                  if _packet_mode(root) else {})
    issues = validate_report(report, plan, findings, statistics, **provenance)
    issues += verify_crossrefs(report, findings, draft.get("claim_bindings"))
    issues += verify_attributions(report, findings)[0]
    issues += verify_report_evidence(report, records)
    _raise_errors(issues, "report validation")
    themes, assignments = _definitions(root, plan)
    # Candidate failures must consume the writing stage's single repair, before
    # the report and candidate file become immutable completed checkpoints.
    opportunities = render_opportunities(draft["opportunities"], findings, records, plan, assignments, themes, report["methodology"],
                                         opportunity_statistics=draft.get("opportunity_statistics"))
    html, markdown = generate_html(report, findings), generate_markdown(report, findings)
    empty_exec = not any(report["executive_summary"].get(k) for k in ("body", "key_findings"))
    rendered_issues = validate_html(html) + validate_markdown(
        markdown, allow_empty_executive=_packet_mode(root) and empty_exec)
    _raise_errors(rendered_issues, "rendered report")
    return report, html, markdown, opportunities, [i["message"] for i in issues + rendered_issues if i["severity"] != "error"]


def finish_opportunities(root):
    root = _root(root)
    control = RunControl(root)
    state = control.status()
    _require_stage(state, "report")
    # report and candidate bytes are immutable outputs of the completed report
    # checkpoint, verified by status() above. Do not add new launch-time work.
    prepared = _prepare(root, "opportunities", WRITING_INPUTS)
    if prepared["action"] == "reuse":
        return {"status": "reused", "stage": "opportunities"}
    content = generate_opportunities(root)
    output = "reports/opportunities.md"
    _atomic_text(root / output, content)
    control.finish("opportunities", [output])
    return {"status": "complete", "stage": "opportunities", "outputs": [output]}


def prepare_sections(root, repair=False):
    if _partition_mode(root):
        raise ValueError("Use batch_partitions prepare for partition-mode sections.")
    return _prepare_sections(root, repair)


def _prepare_sections(root, repair=False):
    from skill.scripts.section_evidence import (build_inventory, section_model_view,
                                               SECTION_FORMAT, QUOTE_REVIEW_FORMAT, SECTION_OUTPUT_CONTRACT)
    root = _root(root)
    state = RunControl(root).status()
    _require_stage(state, "phase-3")
    _gate(root)
    plan, records, findings = read_json(root / PLAN), read_records(root / RETAINED), read_json(root / FINDINGS)
    definitions, assignments = _definitions(root, plan)
    nominations = read_json(root / NOMINATIONS) if (root / NOMINATIONS).exists() else {}
    for qid, definition in definitions.items():
        definition["quote_nominations"] = nominations.get(qid, {})
    inventory = build_inventory(root, plan, records, findings, definitions,
                                {qid: obj["assignments"] for qid, obj in assignments.items()})
    if not (root / INVENTORY).exists():
        atomic_json(root / INVENTORY, inventory)
    elif read_json(root / INVENTORY) != inventory:
        raise ValueError("Section inventory changed after preparation; do not reuse stale evidence.")
    prepared = _prepare(root, "sections", [*WRITING_INPUTS, INVENTORY], repair)
    if prepared["action"] == "reuse":
        return prepared
    return {**prepared, "business_context": plan["business_context"], "inventory": section_model_view(inventory),
            "format": SECTION_FORMAT, "quote_review_format": QUOTE_REVIEW_FORMAT,
            "output_contract": SECTION_OUTPUT_CONTRACT,
            "rules": [
                "Draft all section packets together once. Use every retained mention to understand themes; quote candidates are only a display shortlist.",
                "Read provided source contexts before approving quotes. Speaker, claim owner, referent and important qualifiers must be clear; literal matching alone is insufficient.",
                "Publish only components scoring at least 2/3 each for relevance, support, added_value and clarity. A 3 is not a completion target; omit weak components now.",
                "Choose 0-1 lead quote, 0-3 takeaways and 0-1 summary. Generic advice or a table paraphrase adds no value. Preserve useful illustrations, contradictions and rare risks.",
                "Claims must answer the question and identify observation, implication or hypothesis. Do not convert prospects into customers, intentions into outcomes or source files into unique accounts.",
                "Use registered exact statistics and source scopes; mention counts partition within a question, source sets overlap. Do not sum across themes or questions.",
                "No new audit, research, alternate prose draft or polishing loop. Repairs are for hard errors only; warnings do not justify one."]}


def _admission_receipt(root, frozen, admission, *, verify=False):
    path = str(Path(frozen).with_suffix(".admission.json"))
    payload = {**admission, "raw_path": frozen, "raw_sha256": file_hash(root / frozen)}
    if (root / path).exists():
        if read_json(root / path) != payload:
            raise ValueError("Admission provenance differs from its frozen raw-to-accepted derivation.")
    elif verify:
        raise ValueError("Missing immutable admission provenance.")
    else:
        atomic_json(root / path, payload)
    return path


def _section_products(root, draft, frozen, *, verify=False):
    from skill.scripts.section_evidence import admit_section_packets, build_section_packets, selected_findings
    inventory = read_json(root / INVENTORY)
    admission = admit_section_packets(draft, inventory)
    receipt = _admission_receipt(root, frozen, admission, verify=verify)
    packets = build_section_packets(draft, inventory)
    display = selected_findings(packets, inventory, read_json(root / FINDINGS))
    _raise_errors(validate_json(display, "findings"), "display findings")
    if verify and (read_json(root / PACKETS) != packets or read_json(root / DISPLAY) != display):
        raise ValueError("Accepted sections differ from their frozen raw admission.")
    return packets, display, receipt


def _verify_sections_admission(root):
    state = RunControl(root).status()
    step = state["steps"]["sections"]
    frozen = f"synthesis/batch/sections-attempt-{step['attempts']}.json"
    return _section_products(root, read_json(root / frozen), frozen, verify=True)


def finish_sections(root, input_path):
    root = _root(root)
    control, draft, frozen = _submission(root, "sections", input_path)
    if draft is None:
        _verify_sections_admission(root)
        return {"status": "reused", "stage": "sections"}
    _gate(root)
    packets, display, receipt = _section_products(root, draft, frozen)
    atomic_json(root / PACKETS, packets)
    atomic_json(root / DISPLAY, display)
    control.finish("sections", [frozen, receipt, PACKETS, DISPLAY, *_partition_outputs(root, "sections")])
    return {"status": "complete", "stage": "sections", "outputs": [PACKETS, DISPLAY, receipt]}


def prepare_executive(root, repair=False, *, contract=None):
    root = _root(root)
    mode = _executive_mode(root)
    if contract not in (None, "accepted_bindings_v1"):
        raise ValueError("Unknown executive contract.")
    if contract and mode != "qualitative_annotations_v1":
        raise ValueError("Accepted executive bindings require qualitative_annotations_v1.")
    state = RunControl(root).status()
    _require_stage(state, "sections")
    _gate(root)
    prepared = _prepare(root, "report", [*WRITING_INPUTS, INVENTORY, PACKETS, DISPLAY], repair)
    if prepared["action"] == "reuse":
        return prepared
    # Reserve deterministic delivery before the final model stage starts.
    _prepare(root, "opportunities", WRITING_INPUTS)
    _verify_sections_admission(root)
    packets, inventory = read_json(root / PACKETS), read_json(root / INVENTORY)
    if contract:
        return {**prepared, **_accepted_executive_input(root, packets, inventory)}
    component = {"text": "useful supported synthesis", "claim_ids": ["Q1_C1"],
                 "quality": {"relevance": 2, "support": 2, "added_value": 2, "clarity": 2}}
    # Accepted claims already carry evidence/source IDs. Do not echo the full
    # retained inventory, repeated denominator sets or transcript contexts again.
    sections = [{k: q[k] for k in ("question_id", "status", "claims", "gaps", "omission_reasons", "exec_candidates")}
                for q in packets["questions"]]
    for section in sections:
        section["claims"] = [{**c, "claim_stats": [_compact_stat(s) for s in c["claim_stats"]]} for c in section["claims"]]
    statistics = {q["question_id"]: [_compact_stat(s)
                                   for c in q["claims"] for s in c["claim_stats"]] for q in packets["questions"]}
    evidence = [{"question_id": q["question_id"], "theme_id": t["theme_id"],
                 **{k: quote[k] for k in ("mention_id", "quote", "attribution", "file_id")}}
                for q in read_json(root / DISPLAY)["questions"] for t in q["themes"] for quote in t["sample_quotes"]]
    result = {**prepared, "business_context": read_json(root / PLAN)["business_context"],
            "accepted_sections": sections, "statistics": statistics,
            "display_evidence": evidence,
            "format": {"headline": component, "body": component, "findings": [component],
                       "opportunities": [{**dict.fromkeys(sorted(FIELDS), "nonempty string"), "claim_ids": ["Q1_C1"], "quality": component["quality"]}]},
            "rules": [
                "Write the overall executive synthesis last using accepted section claims, exact statistics, counterevidence and gaps only. No new facts, source reading or research.",
                "Headline is at most 120 characters. Headline and body may be null; findings 0-3, opportunities 0-5. Keep only relevant, supported, insightful components with all quality scores >=2/3. Do not repeat a chart as insight.",
                "Explain what is happening, why it matters and the supported decision or test; label implications/hypotheses and unresolved uncertainty. Do not invent causality, ROI, ideal customer profiles or ownership.",
                "Use explicit quantitative units such as 40% of source files or 3 of 10 mentions. Counts describe only the cited claims’ support; reuse a bound contrast clause exactly or omit its number.",
                "Every component needs supporting accepted claim_ids. All opportunity claim_ids must belong to its question_id, with an approved displayed quote from that evidence. Overall executive components may cite across questions while preserving each question's separate count scope; the renderer supplies exact quote and counts.",
                "Named competitor/profile details are welcome when supported; sparse evidence is not absence. Preserve concentration and contradiction without forcing balanced findings.",
                "Repairs are for hard errors only; no polishing pass, no new audit."]}
    if mode == "qualitative_annotations_v1":
        result["executive_mode"] = mode
        result["format"]["findings"] = [{**component, "statistic_claim_ids": ["Q1_C1"]}]
        result["rules"][3] = (
            "Write qualitative executive prose, including opportunities: no numerical prevalence, percentages, ratios, "
            "counts of sources/people, or majority/universal frequency claims. Do not copy numbers from claim scope into prose. "
            "Each finding must include statistic_claim_ids: zero to two distinct accepted claim IDs from that finding's "
            "claim_ids, selected only when their specific claim adds useful evidence. Code separately labels each selected "
            "claim with its exact unique supporting source-file count and question-specific denominator. These annotations "
            "describe that individual claim, never prevalence of the whole multi-claim synthesis. Do not combine claim counts.")
    return result


OPPORTUNITY_BINDING_FIELDS = {"binding_id", "title", "problem", "hypothesis", "next_step", "quality"}


def _opportunity_bindings(packets, inventory, display):
    """Enumerate support-only claim/approved display pairs, never new evidence."""
    from skill.scripts.section_evidence import _digest, _check_packets
    _check_packets(packets, inventory)
    questions = {q["question_id"]: q for q in inventory["questions"]}
    owners = {q["question_id"]: q for q in packets["questions"]}
    bindings, quotes = [], {}
    for question in display["questions"]:
        qid = question["question_id"]
        candidates = {c["quote_id"]: c for c in questions[qid]["quote_candidates"]}
        for theme in question["themes"]:
            for quote in theme["sample_quotes"]:
                mid = quote["mention_id"]
                candidate = candidates.get(mid)
                if (candidate is None or candidate["theme_id"] != theme["theme_id"]
                        or any(candidate[a] != quote[b] for a, b in
                               (("quote", "quote"), ("attribution", "attribution"), ("source_id", "file_id")))):
                    raise ValueError("Opportunity display evidence differs from locked quote provenance.")
                for claim in owners[qid]["claims"]:
                    if mid not in claim["support_mention_ids"]:
                        continue
                    identity = {"packet_digest": packets["packet_digest"],
                                "evidence_digest": inventory["evidence_digest"],
                                "claim_id": claim["claim_id"], "quote_id": mid}
                    bindings.append({"binding_id": "OB_" + _digest(identity),
                                     "claim_id": claim["claim_id"], "quote_id": mid,
                                     "question_id": qid, "theme_id": theme["theme_id"]})
                    quotes[mid] = {"quote_id": mid, "quote": quote["quote"],
                                   "attribution": quote["attribution"], "source_id": quote["file_id"]}
    return bindings, list(quotes.values())


def _accepted_executive_input(root, packets, inventory):
    """One copy of each accepted meaning, contradiction, quote and scoped count."""
    from skill.scripts.section_evidence import selected_findings
    display = read_json(root / DISPLAY)
    if display != selected_findings(packets, inventory, read_json(root / FINDINGS)):
        raise ValueError("Display findings changed after accepted quote selection.")
    bindings, quotes = _opportunity_bindings(packets, inventory, display)
    sections = []
    for question in packets["questions"]:
        claims = []
        for claim in question["claims"]:
            annotation = executive_annotations([claim["claim_id"]], packets, inventory)[0]
            keep = ("claim_id", "type", "text", "scope", "theme_ids", "source_file_ids",
                    "counterevidence", "counterevidence_source_file_ids", "source_observation")
            claims.append({**{k: copy.deepcopy(claim[k]) for k in keep if k in claim},
                           "annotation_support": {k: annotation[k] for k in
                                                  ("supporting_source_files", "question_source_files")}})
        sections.append({**{k: copy.deepcopy(question[k]) for k in
                             ("question_id", "status", "gaps", "omission_reasons", "exec_candidates")},
                         "claims": claims})
    return {"executive_contract": "accepted_bindings_v1", "executive_mode": "qualitative_annotations_v1",
            "business_context": read_json(root / PLAN)["business_context"], "accepted_sections": sections,
            "opportunity_bindings": bindings, "displayed_quotes": quotes,
            "rules": [
                "Write last from the complete accepted claim text, source-local scope, contradictions and gaps supplied here. No new facts, source reading or research.",
                "A source observation describes only its stated source and conditions; it is not population prevalence, verified outcomes or a cross-source pattern. Preserve this scope in any synthesis.",
                "Keep headline/body optional, headline at most 120 characters, findings zero to three and opportunities zero to five. Each finding is at most 400 characters and states one main insight rather than retelling anecdotes. Keep only useful, supported components with every quality score at least two.",
                "Executive prose stays qualitative: no source/people counts, percentages, ratios or majority/universal frequency claims. Each finding selects zero to two statistic_claim_ids among its own claim_ids; code separately displays each exact claim and its own support, never the pooled finding's prevalence.",
                "Every headline/body/finding cites accepted claim_ids. Opportunity prose chooses one supplied binding_id; code copies its accepted claim, approved displayed quote and question/theme references. Do not return those references or invent quote text.",
                "Bindings authorize evidence selection, not your interpretation. The selected claim and quotation must support the stated problem; hypotheses and next steps remain proposals to validate. Preserve opposing requirements, named alternatives and uncertainty.",
                "Do not use inline quotations, quoted fragments, or straight/curly double-quote characters in any free prose. Apostrophes and contractions are allowed. Select statistic_claim_ids for exact source anecdotes, business quantities and literal detail; code copies their complete accepted text and scope separately. No source/person/customer/account counts in prose, including one prospect or a single customer. Displayed quotes are evidence for selecting opportunity bindings, not text to quote in prose. Repairs are for hard errors only; no audit or polishing pass."]}


def resolve_executive_bindings(root, answer):
    """Resolve valid choices; leave malformed items for scoped admission repair.

    Never filter a failed choice or accept caller-supplied claim/theme overrides.
    The unchanged raw model response remains in the host's transport receipt.
    """
    root = _root(root)
    if _executive_mode(root) != "qualitative_annotations_v1":
        raise ValueError("Accepted executive bindings require qualitative_annotations_v1.")
    _verify_sections_admission(root)
    payload = _accepted_executive_input(root, read_json(root / PACKETS), read_json(root / INVENTORY))
    choices = {b["binding_id"]: b for b in payload["opportunity_bindings"]}
    result = copy.deepcopy(answer)
    if not isinstance(result, dict) or not isinstance(result.get("opportunities"), list):
        return result
    for i, value in enumerate(result["opportunities"]):
        if (not isinstance(value, dict) or set(value) != OPPORTUNITY_BINDING_FIELDS
                or not isinstance(value["binding_id"], str) or value["binding_id"] not in choices):
            if isinstance(value, dict) and set(value) == FIELDS | {"claim_ids", "quality"}:
                # A caller cannot bypass choice selection with already-canonical
                # references. Keep the supplied fields available for local repair.
                result["opportunities"][i] = {**value, "binding_id": None}
            continue
        binding = choices[value["binding_id"]]
        result["opportunities"][i] = {**{k: v for k, v in value.items() if k != "binding_id"},
                                      "claim_ids": [binding["claim_id"]], "mention_id": binding["quote_id"],
                                      "question_id": binding["question_id"], "theme_id": binding["theme_id"]}
    return result


def finish_executive(root, input_path):
    root = _root(root)
    control, draft, frozen = _submission(root, "report", input_path)
    if draft is None:
        state = control.status()
        frozen = f"synthesis/batch/report-attempt-{state['steps']['report']['attempts']}.json"
        admission, _, _ = _executive_admission(root, read_json(root / frozen))
        _admission_receipt(root, frozen, admission, verify=True)
        return {"status": "reused", "stage": "report"}
    _require_stage(control.status(), "sections")
    _gate(root)
    admission, _, _ = _executive_admission(root, draft)
    receipt = _admission_receipt(root, frozen, admission)
    material, questions, display = _executive_material(root, draft)
    return _publish(root, control, material, frozen, questions, display, additional_outputs=[receipt])


def _compact_stat(stat):
    return {k: v for k, v in stat.items() if k not in {"numerator_ids", "denominator_ids", "mention_ids"}}


def _claim_statistics(ids, packets, inventory):
    from skill.scripts.claim_quantities import claim_support_stats
    claims = {c["claim_id"]: c for q in packets["questions"] for c in q["claims"]}
    owners = {c["claim_id"]: q["question_id"] for q in packets["questions"] for c in q["claims"]}
    questions = {q["question_id"]: q for q in inventory["questions"]}
    statistics = []
    for qid in dict.fromkeys(owners[cid] for cid in ids):
        cited = [claims[cid] for cid in ids if owners[cid] == qid]
        combined = {**cited[0], "text": "Cited accepted evidence.", "scope": "Cited accepted evidence.",
                    "count_groups": [], "stat_refs": sorted({ref for c in cited for ref in c["stat_refs"]}),
                    "support_mention_ids": sorted({mid for c in cited for mid in c["support_mention_ids"]}),
                    "counterevidence_ids": [], "theme_ids": sorted({tid for c in cited for tid in c["theme_ids"]})}
        # This synthetic support union is not a copied source observation.
        combined.pop("source_observation", None)
        statistics.extend(claim_support_stats(combined, questions[qid]))
        statistics.extend(s for c in cited for s in c["claim_stats"] if s.get("binding") is not None)
    return statistics


def executive_annotations(ids, packets, inventory):
    """Project explicitly selected accepted claims; never pool their support."""
    from skill.scripts.claim_quantities import claim_support_stats
    claims = {c["claim_id"]: (q["question_id"], c) for q in packets["questions"] for c in q["claims"]}
    questions = {q["question_id"]: q for q in inventory["questions"]}
    if (not isinstance(ids, list) or len(ids) > 2 or
            any(not isinstance(cid, str) or cid not in claims for cid in ids) or len(set(ids)) != len(ids)):
        raise ValueError("Evidence annotations need zero to two distinct accepted claim IDs.")
    annotations = []
    for cid in ids:
        qid, claim = claims[cid]
        stat = next(s for s in claim_support_stats(claim, questions[qid]) if s["unit"] == "source_files")
        annotations.append({"claim_id": cid, "question_id": qid, "claim_text": claim["text"],
                            "scope": claim["scope"], "supporting_source_files": stat["numerator"],
                            "question_source_files": stat["denominator"]})
    return annotations


def _qualitative_quantity_errors(text):
    """Forbid unbound prevalence in prose; exact code annotations stay separate.

    Reuse the existing quantity recognizer and also catch unitless ratios at
    sentence ends, call counts, and explicit majority/universal quantifiers.
    Quoted source text still goes through the existing literal-quote checks.
    """
    from skill.scripts.claim_quantities import _quantities, _NUMBER, _UNIT, _MOD
    from skill.scripts.verify_report_evidence import quoted_spans
    spans, _ = quoted_spans(text)
    masked = text
    for span in reversed(spans):
        masked = masked[:span["start"]] + " " * (span["end"] - span["start"]) + masked[span["end"]:]
    units = r"(?:" + _UNIT + r"|calls?|interviews?|buyers?|teams?|organizations?|companies)"
    count = r"(?<![\w.,-])" + _NUMBER + r"\s+" + _MOD + units + r"\b"
    ratio = r"(?<![\w.,-])" + _NUMBER + r"\s*(?:/|\s+(?:of|out\s+of)\s+)\s*(?:the\s+)?" + _NUMBER + r"\b"
    frequency = r"\b(?:(?:most|all|every|no)\s+|(?:majority|minority|none)\s+of\s+)(?:the\s+)?" + _MOD + units + r"\b"
    ratios = [m.group() for m in re.finditer(ratio, masked, re.I) if re.sub(r"\s", "", m.group()) != "24/7"]
    if _quantities(text) or ratios or re.search(count + "|" + frequency, masked, re.I):
        return ["Qualitative executive prose cannot state prevalence; select accepted claim IDs for separate code-generated evidence annotations."]
    return []


def _executive_admission(root, draft, *, inspect_only=False):
    """Inspect every original component before omitting any local editorial failure."""
    from skill.scripts.section_evidence import (admission_issue, quality_issues, _inline_quotes,
                                               _digest, report_questions, selected_findings)
    from skill.scripts.claim_quantities import validate_quantities, claim_support_stats
    from skill.scripts.verify_quotes import normalize_text
    from skill.scripts.generate_opportunities import _validate_problem_numbers
    qualitative = _executive_mode(root) == "qualitative_annotations_v1"
    _shape(draft, {"headline", "body", "findings", "opportunities"}, label="executive draft")
    _verify_sections_admission(root)
    packets, inventory, display = read_json(root / PACKETS), read_json(root / INVENTORY), read_json(root / DISPLAY)
    if display != selected_findings(packets, inventory, read_json(root / FINDINGS)):
        raise ValueError("Display findings changed after accepted quote selection.")
    claims = {c["claim_id"]: c for q in packets["questions"] for c in q["claims"]}
    owners = {c["claim_id"]: q["question_id"] for q in packets["questions"] for c in q["claims"]}
    questions = {q["question_id"]: q for q in inventory["questions"]}
    mentions = {m["mention_id"]: (q["question_id"], m) for q in inventory["questions"] for m in q["mentions"]}
    approved = {quote["mention_id"]: normalize_text(quote["quote"]) for q in display["questions"] for t in q["themes"] for quote in t["sample_quotes"]}
    findings = {q["question_id"]: q for q in display["questions"]}
    issues = []
    def add(kind, code, component, key, message):
        issues.append(admission_issue(kind, code, component, key, message))
    def refs(ids, component, key):
        valid = (isinstance(ids, list) and bool(ids) and all(isinstance(cid, str) and cid in claims for cid in ids)
                 and len(set(ids)) == len(ids))
        if not valid:
            add("hard", "claim_reference", component, key, "Every executive component needs unique accepted claim_ids.")
        return valid
    def stats_for(ids):
        return _claim_statistics(ids, packets, inventory)
    def prose(text, ids, component, key, *, quote_ids=None):
        for error in validate_quantities(text, stats_for(ids)):
            add("editorial", "quantity", component, key, error)
        if qualitative:
            for error in _qualitative_quantity_errors(text):
                add("editorial", "qualitative_quantity", component, key, error)
        quoted, unmatched = _inline_quotes(text)
        support = {mid for cid in ids for mid in claims[cid]["support_mention_ids"]}
        permitted = support if quote_ids is None else support & quote_ids
        checked = {approved[mid] for mid in permitted if mid in approved}
        if unmatched or any(value not in checked for value in quoted):
            add("editorial", "inline_quote", component, key, "Quotations must exactly reuse section-approved displayed quotes from cited claims.")
    def component(value, key):
        if value is None:
            return
        expected = {"text", "claim_ids", "quality"}
        if qualitative and key.startswith("findings/"):
            expected.add("statistic_claim_ids")
        if not isinstance(value, dict) or set(value) != expected:
            add("hard", "structure", "executive", key, "Executive component fields must match the format.")
            return
        valid = refs(value["claim_ids"], "executive", key)
        if "statistic_claim_ids" in expected:
            ids = value["statistic_claim_ids"]
            if (not isinstance(ids, list) or len(ids) > 2 or
                    any(not isinstance(cid, str) or cid not in claims for cid in ids) or
                    len(set(ids)) != len(ids) or (valid and not set(ids) <= set(value["claim_ids"]))):
                add("hard", "statistic_reference", "executive", key,
                    "Statistic references must be zero to two distinct accepted claim IDs cited by this finding.")
        if not isinstance(value["text"], str) or not value["text"].strip():
            add("hard", "structure", "executive", key, "An executive component needs text or must be null.")
        elif valid:
            prose(value["text"], value["claim_ids"], "executive", key)
        if key == "headline" and isinstance(value["text"], str) and len(value["text"]) > 120:
            add("hard", "field_length", "executive", key, f"Headline is {len(value['text'])} chars (limit 120).")
        for kind, code, message in quality_issues(value["quality"], reason=False):
            add(kind, code, "executive", key, message)
    component(draft["headline"], "headline")
    component(draft["body"], "body")
    if not isinstance(draft["findings"], list) or len(draft["findings"]) > 3 or any(f is None for f in draft["findings"]):
        add("hard", "structure", "document", None, "Choose zero to three executive findings.")
    else:
        for index, value in enumerate(draft["findings"]):
            component(value, f"findings/{index}")
    if not isinstance(draft["opportunities"], list) or len(draft["opportunities"]) > 5:
        add("hard", "structure", "document", None, "Choose zero to five opportunities.")
    else:
        for index, value in enumerate(draft["opportunities"]):
            if not isinstance(value, dict) or set(value) != FIELDS | {"claim_ids", "quality"}:
                add("hard", "structure", "opportunity", index, "Opportunity fields must match the documented format.")
                continue
            valid = refs(value["claim_ids"], "opportunity", index)
            if any(not isinstance(value[k], str) or not value[k].strip() for k in FIELDS):
                add("hard", "structure", "opportunity", index, "Opportunity fields must be nonempty strings.")
                continue
            qid, tid, mid = value["question_id"], value["theme_id"], value["mention_id"]
            if mid not in mentions or mentions[mid][0] != qid or mentions[mid][1]["theme_id"] != tid:
                add("hard", "evidence_reference", "opportunity", index, "Opportunity evidence must belong to its retained question and assigned theme.")
                valid = False
            if valid and any(owners[cid] != qid for cid in value["claim_ids"]):
                add("hard", "claim_reference", "opportunity", index, "Opportunity claim_ids must belong to its stated question.")
                valid = False
            if valid:
                support = {m for cid in value["claim_ids"] for m in claims[cid]["support_mention_ids"]}
                if mid not in support:
                    add("hard", "claim_reference", "opportunity", index, "Opportunity evidence must belong to a cited accepted claim.")
                    valid = False
                elif mid not in approved:
                    add("editorial", "quote_dependency", "opportunity", index, "No approved displayed quote remains for this opportunity.")
            for kind, code, message in quality_issues(value["quality"], reason=False):
                add(kind, code, "opportunity", index, message)
            if valid:
                for field in ("title", "problem", "hypothesis", "next_step"):
                    prose(value[field], value["claim_ids"], "opportunity", index, quote_ids={mid})
                question = findings[qid]
                theme = next(t for t in question["themes"] if t["theme_id"] == tid)
                # This legacy helper checks only optional title/problem numeric prose.
                # Its failures are editorial; all evidence/assignment references were preflighted above.
                try:
                    _validate_problem_numbers(value, question, theme, mentions[mid][1], statistics=stats_for(value["claim_ids"]))
                except ValueError as exc:
                    add("editorial", "numeric_literal", "opportunity", index, str(exc))
    if inspect_only:
        return issues
    hard = [i for i in issues if i["kind"] == "hard"]
    if hard:
        raise ValueError("Executive evidence: " + "; ".join(i["message"] for i in hard))
    accepted = copy.deepcopy(draft)
    bad_exec = {i["component_id"] for i in issues if i["component"] == "executive"}
    for key in ("headline", "body"):
        if key in bad_exec:
            accepted[key] = None
    accepted["findings"] = [v for i, v in enumerate(accepted["findings"]) if f"findings/{i}" not in bad_exec]
    bad_opportunities = {i["component_id"] for i in issues if i["component"] == "opportunity"}
    accepted["opportunities"] = [v for i, v in enumerate(accepted["opportunities"]) if i not in bad_opportunities]
    receipt = {"version": 1, "raw_digest": _digest(draft), "accepted_digest": _digest(accepted),
               "evidence_digest": packets["packet_digest"], "accepted": accepted, "omissions": issues}
    receipt["admission_digest"] = _digest(receipt)
    return receipt, report_questions(packets, inventory), display


def _executive_material(root, draft):
    admission, questions, display = _executive_admission(root, draft)
    accepted = admission["accepted"]
    # Strict post-projection check, with no second omission or semantic revision.
    checked, _, _ = _executive_admission(root, accepted)
    if checked["omissions"] or checked["accepted"] != accepted:
        raise ValueError("Accepted executive projection failed strict admission validation.")
    material = {"executive_summary": {"headline": accepted["headline"]["text"] if accepted["headline"] else None,
                "body": accepted["body"]["text"] if accepted["body"] else None,
                "key_findings": [v["text"] for v in accepted["findings"]],
                "key_questions": [q["text"] for q in read_json(root / PLAN)["questions"]]},
                "opportunities": [{k: value[k] for k in FIELDS} for value in accepted["opportunities"]]}
    packets, inventory = read_json(root / PACKETS), read_json(root / INVENTORY)
    if _executive_mode(root) == "qualitative_annotations_v1":
        material["executive_summary"]["key_findings"] = [
            {"text": value["text"], "evidence_annotations": executive_annotations(value["statistic_claim_ids"], packets, inventory)}
            for value in accepted["findings"]]
    material["opportunity_statistics"] = [_claim_statistics(value["claim_ids"], packets, inventory) for value in accepted["opportunities"]]
    registered = {q["question_id"]: [s for c in q["claims"] for s in c["claim_stats"]] for q in packets["questions"]}
    components = [v for v in (accepted["headline"], accepted["body"]) if v] + accepted["findings"] + accepted["opportunities"]
    for value in components:
        for stat in _claim_statistics(value["claim_ids"], packets, inventory):
            if stat not in registered[stat["scope"]]:
                registered[stat["scope"]].append(stat)
    material["admitted_statistics"] = registered
    material["claim_bindings"] = {f"executive_summary.{key}": {
        "text": value["text"], "claim_ids": value["claim_ids"],
        "statistics": _claim_statistics(value["claim_ids"], packets, inventory),
        "question_ids": sorted({stat["scope"] for stat in _claim_statistics(value["claim_ids"], packets, inventory)})}
        for key, value in (("headline", accepted["headline"]), ("body", accepted["body"])) if value}
    return material, questions, display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = {"prepare-analysis": prepare_analysis, "finish-analysis": finish_analysis,
               "prepare-writing": prepare_writing, "finish-writing": finish_writing,
               "prepare-sections": prepare_sections, "finish-sections": finish_sections,
               "prepare-executive": prepare_executive, "finish-executive": finish_executive,
               "finish-opportunities": finish_opportunities}
    parser.add_argument("action", choices=actions)
    parser.add_argument("--root", default=".")
    parser.add_argument("--input", help="Draft JSON file inside the run folder")
    parser.add_argument("--output", help="Preserve full prepare context for recovery if tool display truncates.")
    parser.add_argument("--repair", action="store_true", help="Consume a repair attempt on a prepare action")
    parser.add_argument("--question", help="Analysis only: work one locked question at a time (recommended)")
    parser.add_argument("--part", help="Chunked question only: themes, c<N> or assemble")
    args = parser.parse_args()
    try:
        if args.question and args.action not in ("prepare-analysis", "finish-analysis"):
            raise ValueError("--question applies to prepare-analysis and finish-analysis only.")
        if args.question and args.action == "prepare-analysis":
            result = prepare_analysis_question(args.root, args.question)
            if args.output:
                atomic_json(_root(args.root) / args.output, result)
        elif args.question:
            if not args.input and args.part != "assemble":
                raise ValueError("finish-analysis --question requires --input.")
            result = finish_analysis_question(args.root, args.question, args.input, args.part)
        elif args.action.startswith("prepare-"):
            function = actions[args.action]
            result = function(args.root, args.repair)
            if args.output:
                atomic_json(_root(args.root) / args.output, result)
        else:
            if args.repair:
                raise ValueError("Use --repair with a prepare action before changing the submitted draft.")
            if args.action == "finish-opportunities":
                result = finish_opportunities(args.root)
            else:
                if not args.input:
                    raise ValueError("A finish action requires --input draft.json.")
                function = actions[args.action]
                result = function(args.root, args.input)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Batch stage stopped: {exc}\n")


if __name__ == "__main__":
    main()

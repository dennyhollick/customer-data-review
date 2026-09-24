"""Read and checkpoint small extraction/audit batches without model orchestration code."""

import argparse
import copy
import json
from pathlib import Path

from skill.scripts import bounded_audit as audit
from skill.scripts.run_control import RunControl, atomic_json, digest
from skill.schemas.validate import validate_json


def _context(root):
    root = Path(root).resolve()
    return root, RunControl(root), audit.read_json(root / "synthesis/plan.json"), audit.read_json(root / "synthesis/source-paths.json")


def _active(plan):
    return [f["file_id"] for f in plan["files"] if f.get("quality") == "good" and not f.get("excluded")]


def _audit_tasks(root, plan, paths):
    request = audit.read_json(root / "synthesis/request.json")
    manifest = audit.prepare(root, mode=request.get("audit_mode", "sampled"),
                             limit=request.get("audit_sample_limit", audit.SAMPLE_LIMIT),
                             source_limit=request.get("audit_source_limit", audit.SOURCE_LIMIT))
    entries = audit._snapshot(root)[2]
    tasks = {}
    # Reconstruct from locked evidence, never trust editable task copies.
    for mid in manifest["sample_ids"]:
        entry = entries[mid]; fid = entry["file_id"]
        task = tasks.setdefault(fid, {"file_id": fid, "source_path": paths[fid],
                                      "questions": plan["questions"], "business_context": plan["business_context"], "mentions": []})
        task["mentions"].append({"mention_id": mid, "fingerprint": entry["fingerprint"], "mention": entry["mention"]})
    return tasks


def _submission(root, key, attempt, value):
    path = root / "synthesis/batches/submissions" / f"{key.replace(':', '-')}-{attempt}.json"
    if path.exists() and audit.read_json(path)["hash"] != digest(value):
        raise ValueError("Changed output requires --retry before editing; each source has one repair only.")
    if not path.exists():
        atomic_json(path, {"hash": digest(value), "value": value})


def _model_removed_proposals(root, fid, repaired):
    """Compare proposed IDs before deterministic drops; neither set changes counts."""
    initial = root / "synthesis/batches/submissions" / f"extract-{fid}-1.json"
    if not initial.exists():
        return []  # An unparseable initial answer has no recorded proposal IDs.
    value = audit.read_json(initial)["value"]
    mentions = value.get("mentions", []) if isinstance(value, dict) else []
    if not isinstance(mentions, list):
        return []
    initial_ids = {m["mention_id"] for m in mentions if isinstance(m, dict)
                   and isinstance(m.get("mention_id"), str) and m["mention_id"]}
    return sorted(initial_ids - {m["mention_id"] for m in repaired["mentions"]})


def _preserve_valid_verdicts(root, fid, keyed, expected):
    previous = root / "synthesis/batches/submissions" / f"audit-initial-{fid}-1.json"
    if not previous.exists():
        return  # Earlier malformed JSON was never parseable; its attempt still counts.
    rows = audit.read_json(previous)["value"]
    frozen = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            audit._key_results([row])
            mid = row["mention_id"]
            if mid not in expected or row.get("fingerprint") != expected[mid]:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        if mid in frozen and frozen[mid] != row:
            raise ValueError("Conflicting initial judgments cannot be silently reconciled in a structural retry.")
        frozen[mid] = row
    if any(keyed.get(mid) != row for mid, row in frozen.items()):
        raise ValueError("Structural retry must preserve already-valid initial verdicts; use the semantic repair path for a failed mention.")


def _select(control, prefix, eligible, size, retry, parallel=False):
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 4:
        raise ValueError("Batch size must be 1..4; never reserve the corpus.")
    state = control.status()
    running = [fid for fid in eligible if state["steps"].get(f"{prefix}:{fid}", {}).get("status") == "running"]
    if parallel and retry:
        raise ValueError("Parallel reservation cannot retry an in-flight wave.")
    if parallel and audit.read_json(control.root / "synthesis/request.json").get("execution_profile") != "direct_batches_v1":
        raise ValueError("Parallel source batches require the frozen direct_batches_v1 profile.")
    if running and not retry and not parallel:
        raise ValueError("Save the current batch first, or use --retry for its failed output only.")
    if retry:
        return running[:size]
    return [fid for fid in eligible if f"{prefix}:{fid}" not in state["steps"]][:size]


def read_batch(root, size=4, retry=False, semantic_audit=False, parallel=False):
    root, control, plan, paths = _context(root)
    prefix = "audit-initial" if semantic_audit else "extract"
    if semantic_audit:
        tasks = _audit_tasks(root, plan, paths)
        eligible = sorted(tasks)
        inputs = ["synthesis/plan.json", "synthesis/audit/audit-manifest.json", "synthesis/pipeline/mentions.jsonl"]
    else:
        eligible = _active(plan)
        inputs = ["synthesis/plan.json", "synthesis/source-paths.json"]
    selected = _select(control, prefix, eligible, size, retry, parallel)
    items = []
    for fid in selected:
        # Materialize the full readable source before reserving its model attempt.
        text = (root / paths[fid]).read_text(encoding="utf-8")
        item = tasks[fid] if semantic_audit else {"file_id": fid, "source_metadata": next(f for f in plan["files"] if f["file_id"] == fid)}
        if not semantic_audit:
            item["quote_source_descriptor"] = audit.quote_source_descriptor(item["source_metadata"]["source_type"], fid)
        reservation = control.claim(f"{prefix}:{fid}", inputs)
        item.update(source_text=text, attempt=reservation["attempt"])
        items.append(item)
    return {"business_context": plan["business_context"], "questions": plan["questions"],
            "items": items, "batch_size": len(items),
            "instruction": ("Return an object mapping each file_id to its verdict array; preserve supplied fingerprints."
                            if semantic_audit else "Return an array of {file_id, mentions:[...]}, one record per full source. Read every source in this batch. Save before requesting another batch.")}


def save_extractions(root, rows):
    root, control, plan, paths = _context(root)
    if not isinstance(rows, list) or not rows or len(rows) > 4:
        raise ValueError("Supply one to four source records.")
    ids = [row.get("file_id") if isinstance(row, dict) else None for row in rows]
    if not all(isinstance(fid, str) for fid in ids) or len(set(ids)) != len(ids):
        raise ValueError("Source IDs must be present and unique in the batch.")
    state = control.status()
    for fid in ids:
        if fid not in _active(plan) or state["steps"].get(f"extract:{fid}", {}).get("status") != "running":
            raise ValueError(f"{fid}: no active extraction attempt; never overwrite a completed source.")
    saved, errors, warnings = [], {}, {}
    for row in rows:
        row = copy.deepcopy(row)
        fid = row["file_id"]
        source = (root / paths[fid]).read_text(encoding="utf-8")
        source_plan = {**plan, "files": [f for f in plan["files"] if f["file_id"] == fid]}
        dropped, model_removed = [], []
        try:
            _submission(root, f"extract:{fid}", state["steps"][f"extract:{fid}"]["attempts"], row)
            schema_errors = validate_json(row, "mentions")
            if schema_errors:
                raise ValueError(str(schema_errors))
            # A second invalid quote is deterministically excluded, without a third model attempt.
            if state["steps"][f"extract:{fid}"]["attempts"] == 2:
                from skill.scripts.validate_mentions import validate_mentions
                schema_errors, _ = validate_mentions(row, plan, fid)
                if schema_errors:
                    raise ValueError(str(schema_errors))
                model_removed = _model_removed_proposals(root, fid, row)
                dropped = list(audit._grounding_errors(row, source))
                row["mentions"] = [m for m in row["mentions"] if m["mention_id"] not in dropped]
            audit.validate_records([row], source_plan, {fid: source})
        except (ValueError, KeyError, TypeError) as exc:
            errors[fid] = str(exc)
            continue
        output = f"synthesis/pipeline/responses/{fid}.json"
        note = f"synthesis/pipeline/extraction-notes/{fid}.json"
        atomic_json(root / output, row)
        atomic_json(root / note, {"file_id": fid, "omitted_ungrounded_ids": dropped,
                                "model_removed_proposal_ids": model_removed,
                                "zero_retained_mentions": not bool(row["mentions"])})
        control.finish(f"extract:{fid}", [output, note])
        saved.append(fid)
        if dropped:
            warnings[fid] = f"Omitted {len(dropped)} ungrounded candidates after the single repair."
    return {"saved": saved, "errors": errors, "warnings": warnings}


def merge_extractions(root):
    root, control, plan, paths = _context(root)
    state = control.status()
    eligible = _active(plan)
    if any(state["steps"].get(f"extract:{fid}", {}).get("status") != "complete" for fid in eligible):
        raise ValueError("Every active source must finish extraction before merge.")
    inputs = ["synthesis/plan.json"] + [f"synthesis/pipeline/responses/{fid}.json" for fid in eligible]
    result = control.claim("phase-2-merge", inputs)
    if result["action"] == "reuse":
        return {"action": "reuse"}
    rows = [audit.read_json(root / p) for p in inputs[1:]]
    audit.validate_records(rows, plan, {fid: (root / paths[fid]).read_text(encoding="utf-8") for fid in eligible})
    output = "synthesis/pipeline/mentions.jsonl"
    (root / output).parent.mkdir(parents=True, exist_ok=True)
    (root / output).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    control.finish("phase-2-merge", [output])
    return {"sources": len(rows), "mentions": sum(len(r["mentions"]) for r in rows)}


def save_audits(root, results):
    root, control, plan, paths = _context(root)
    if not isinstance(results, dict) or not results or len(results) > 4:
        raise ValueError("Supply one to four file_id/verdict-array pairs.")
    state = control.status()
    tasks = _audit_tasks(root, plan, paths)
    for fid in results:
        if state["steps"].get(f"audit-initial:{fid}", {}).get("status") != "running":
            raise ValueError(f"{fid}: no active audit attempt; never overwrite a completed verdict.")
    saved, errors = [], {}
    for fid, rows in results.items():
        try:
            _submission(root, f"audit-initial:{fid}", state["steps"][f"audit-initial:{fid}"]["attempts"], rows)
            task = tasks[fid]
            expected = {m["mention_id"]: m["fingerprint"] for m in task["mentions"]}
            keyed = audit._key_results(rows)
            if set(keyed) != set(expected) or any(row.get("fingerprint") != expected[mid] for mid, row in keyed.items()):
                raise ValueError("Verdicts must exactly cover the task IDs with unchanged fingerprints.")
            if state["steps"][f"audit-initial:{fid}"]["attempts"] == 2:
                _preserve_valid_verdicts(root, fid, keyed, expected)
        except (ValueError, KeyError, TypeError) as exc:
            errors[fid] = str(exc)
            continue
        output = f"synthesis/audit/results/{fid}.json"
        atomic_json(root / output, rows)
        control.finish(f"audit-initial:{fid}", [output])
        saved.append(fid)
    return {"saved": saved, "errors": errors}


def assess_audit(root):
    root, control, _, _ = _context(root)
    evidence = audit.managed_evidence(root)
    state, summary = evidence["state"], evidence["summary"]
    if (root / "synthesis/audit/audit-summary.json").exists():
        return {**audit.verify_gate(root), "next_action": "sealed"}
    if state.get("audit_incomplete_reason"):
        action = "audit-finalize --allow-incomplete"
    elif state["status"] != "active":
        action = "audit-finalize --allow-incomplete" if state["status"] == "exhausted" else "closed_or_paused"
    elif state.get("audit_correction"):
        batches = state["audit_correction"].get("incoming_batches", [])
        pending_save = bool(batches and not batches[-1]["complete"])
        action = "audit-repair-save" if pending_save or any(state["steps"].get(f"audit-repair:{fid}", {}).get("status") == "running"
                                          for fid in evidence["pending"]) else "audit-repair-read" if evidence["pending"] else "audit-finalize"
    else:
        action = "audit-finalize" if summary["status"] == "ready" else "audit-repair-read" if evidence["cohort"]["groups"] else "audit-finalize --allow-incomplete"
    return {"complete": False, "status": "assessed", "summary": summary,
            "failed_sources": evidence["cohort"]["groups"], "pending_sources": evidence["pending"], "next_action": action}


def read_audit_repairs(root, size=4):
    root, control, plan, paths = _context(root)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 4:
        raise ValueError("Batch size must be 1..4; never reserve the corpus.")
    evidence = audit.managed_evidence(root)
    state = evidence["state"]
    if state["status"] != "active" or state.get("audit_incomplete_reason") or (root / "synthesis/audit/audit-summary.json").exists():
        raise ValueError("No new correction work on a sealed, paused, or exhausted run.")
    anchor = state.get("audit_correction")
    if not anchor:
        if evidence["summary"]["status"] == "ready" or not evidence["cohort"]["groups"]:
            raise ValueError("No correction needed or available; finalize without chasing 100%.")
        path = "synthesis/audit/correction-cohort.json"
        if (root / path).exists() and audit.read_json(root / path) != evidence["cohort"]:
            raise ValueError("Changed correction cohort before reservation.")
        atomic_json(root / path, evidence["cohort"])
        from skill.scripts.run_control import file_hash
        state["audit_correction"] = {"path": path, "hash": file_hash(root / path), "submissions": {}}
        atomic_json(control.path, state)
        evidence = audit.managed_evidence(root)
        state = evidence["state"]
    groups = evidence["cohort"]["groups"]
    batches = state["audit_correction"].get("incoming_batches", [])
    if batches and not batches[-1]["complete"]:
        raise ValueError("Replay the identical pending correction save before reserving more sources; no redispatch.")
    if any(state["steps"].get(f"audit-repair:{fid}", {}).get("status") == "running" for fid in groups):
        raise ValueError("Correction already reserved: reconcile its worker and save the existing draft; no redispatch or retry.")
    selected = [fid for fid in groups if f"audit-repair:{fid}" not in state["steps"]][:size]
    entries = audit._snapshot(root)[2]
    initial = audit._key_results(evidence["initial"])
    items = []
    for fid in selected:
        text = (root / paths[fid]).read_text(encoding="utf-8")
        reservation = control.claim(f"audit-repair:{fid}", evidence["inputs"])
        items.append({"file_id": fid, "source_path": paths[fid], "source_text": text,
                      "attempt": reservation["attempt"], "mentions": [
                          {"mention_id": mid, "fingerprint": entries[mid]["fingerprint"],
                           "mention": entries[mid]["mention"], "initial_verdict": initial[mid]}
                          for mid in groups[fid]]})
    return {"items": items, "batch_size": len(items), "business_context": plan["business_context"],
            "questions": plan["questions"], "instruction": (
                "Read each full source. Correct and recheck only every supplied failed mention in one draft. "
                "Return an object mapping each file_id to an array covering exactly its supplied IDs. "
                "Each row: mention_id, original fingerprint, verdict (pass/fail/uncertain), nonempty reason, "
                "replacement (full corrected mention with unchanged mention_id/question_id for pass; null otherwise). "
                "Use the original mention object as the complete replacement field contract. Literal quotes, names and roles "
                "must remain source grounded and the interpretation must match source meaning. "
                "Omit replacement_fingerprint; the parent fills it deterministically. "
                "Do not add IDs, revisit passing items or request another correction.")}


def _freeze_correction(root, control, fid, value):
    from skill.scripts.run_control import file_hash
    state = control.status()
    path = f"synthesis/audit/correction-submissions/{fid}.json"
    anchor = state["audit_correction"]["submissions"].get(fid)
    if anchor:
        if file_hash(root / anchor["path"]) != anchor["hash"] or audit.read_json(root / anchor["path"]) != {"value": value}:
            raise ValueError("Changed correction draft; only identical interrupted-save replay is permitted.")
    else:
        if (root / path).exists() and audit.read_json(root / path) != {"value": value}:
            raise ValueError("Changed correction draft before receipt completion.")
        atomic_json(root / path, {"value": value})
        state["audit_correction"]["submissions"][fid] = {"path": path, "hash": file_hash(root / path)}
        atomic_json(control.path, state)
    return path


def _stop_bad_correction(root, control, reason, payload=None):
    if payload is not None:
        # Preserve malformed CLI bytes exactly (hex) before any parsing or validation retry is possible.
        path = root / "synthesis/audit/unusable-correction.json"
        if not path.exists():
            atomic_json(path, payload)
    state = control.status()
    state["audit_incomplete_reason"] = f"Unusable single correction draft: {reason}"
    atomic_json(control.path, state)
    control.close(complete=False)
    return {"complete": False, "status": "incomplete", "errors": {"correction": str(reason)}}


def save_audit_repairs(root, results=None, *, input_path=None):
    root, control, _, _ = _context(root)
    evidence = audit.managed_evidence(root)
    state = evidence["state"]
    if state["status"] not in ("active", "exhausted") or state.get("audit_incomplete_reason") or not state.get("audit_correction"):
        raise ValueError("No active correction cohort; closed runs cannot reopen.")
    running = {fid for fid in evidence["cohort"]["groups"]
               if state["steps"].get(f"audit-repair:{fid}", {}).get("status") == "running"}
    batches = state["audit_correction"].setdefault("incoming_batches", [])
    replaying = bool(batches and not batches[-1]["complete"])
    if not running and not replaying:
        raise ValueError("No in-flight correction draft; completed sources cannot be resubmitted.")
    # The whole payload is the model draft. Bind it atomically before parsing or per-source
    # writes, so a crash cannot leave later members or malformed bytes open to replacement.
    raw = (root / input_path).read_bytes() if input_path is not None else None
    payload = {"raw_hex": raw.hex()} if raw is not None else {"value": results}
    if replaying:
        if batches[-1]["payload"] != payload:
            return _stop_bad_correction(root, control, "Changed correction draft; only identical interrupted-save replay is permitted.")
    else:
        batches.append({"payload": payload, "reserved_sources": sorted(running), "complete": False})
        atomic_json(control.path, state)
    if raw is not None:
        try:
            results = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            return _stop_bad_correction(root, control, str(exc), payload)
    completed = {fid for fid in evidence["cohort"]["groups"]
                 if state["steps"].get(f"audit-repair:{fid}", {}).get("status") == "complete"}
    if (not isinstance(results, dict) or not results or len(results) > 4 or
            not set(results) <= running | completed or (not set(results) & running and not replaying)):
        return _stop_bad_correction(root, control, "Supply one to four reserved file_id/verdict-array pairs.", {"value": results})
    # Freeze the entire submitted batch before validating any source.
    submissions = {}
    try:
        for fid, rows in results.items():
            submissions[fid] = _freeze_correction(root, control, fid, rows)
        normalized = {}
        for fid, rows in results.items():
            normalized[fid] = audit.normalize_repairs(rows, evidence["cohort"]["groups"][fid])
            audit.evaluate(root, evidence["initial"], normalized[fid])
    except (ValueError, KeyError, TypeError) as exc:
        return _stop_bad_correction(root, control, str(exc))
    for fid, rows in normalized.items():
        if fid in completed:
            continue  # Identical batch replay after another source finished before interruption.
        output = f"synthesis/audit/corrections/{fid}.json"
        if (root / output).exists() and audit.read_json(root / output) != rows:
            return _stop_bad_correction(root, control, "Changed accepted correction output.")
        atomic_json(root / output, rows)
        control.finish(f"audit-repair:{fid}", [submissions[fid], output])
    state = control.status()
    state["audit_correction"]["incoming_batches"][-1]["complete"] = True
    atomic_json(control.path, state)
    return {"saved": list(normalized), "errors": {}}


def finalize_audit(root, allow_incomplete=False):
    root, control, _, _ = _context(root)
    evidence = audit.managed_evidence(root)
    state, summary = evidence["state"], evidence["summary"]
    if (root / "synthesis/audit/audit-summary.json").exists():
        gate = audit.verify_gate(root)
        if "summary" in gate and state["status"] in ("active", "exhausted"):
            if state["steps"].get("audit", {}).get("status") == "running":
                outputs = ["synthesis/audit/initial-results.json", "synthesis/audit/audit-summary.json",
                           "synthesis/audit/validated_mentions.jsonl"]
                if state.get("audit_correction"):
                    outputs.append("synthesis/audit/repairs.json")
                control.finish("audit", outputs)
            if gate["summary"]["status"] == "incomplete":
                control.close(complete=False)
        return gate
    if state["status"] in ("complete", "incomplete", "paused"):
        raise ValueError("Cannot finalize a new audit on a closed or paused run.")
    allow_incomplete = allow_incomplete or bool(state.get("audit_incomplete_reason"))
    if not allow_incomplete:
        if evidence["pending"]:
            return {**assess_audit(root), "status": "repair_pending"}
        if summary["status"] != "ready" and not state.get("audit_correction"):
            return {**assess_audit(root), "status": "repair_required"}
    exhausted = state["status"] == "exhausted"
    incomplete = allow_incomplete or summary["status"] != "ready" or exhausted
    if exhausted and not allow_incomplete:
        return {"complete": False, "status": "exhausted", "next_action": "audit-finalize --allow-incomplete"}
    if incomplete:
        minimum = audit.min_pass_percent(audit.read_json(root / "synthesis/request.json"))
        state.setdefault("audit_incomplete_reason", "Explicit incomplete close." if allow_incomplete else
                         f"Single correction completed below the fixed {minimum}% threshold.")
        atomic_json(control.path, state)
    # An exhausted budget cannot obtain a fresh terminal reservation. Explicit incomplete sealing
    # is deterministic closure, not permission to launch new model work.
    reserve_terminal = not exhausted and (not incomplete or
        sum(s["status"] == "running" for s in state["steps"].values()) < 4 or "audit" in state["steps"])
    if reserve_terminal:
        step = state["steps"].get("audit")
        if not step:
            control.claim("audit", evidence["inputs"])
        elif step["inputs"] != control._hash_paths(evidence["inputs"]):
            raise ValueError("Audit inputs changed during finalization.")
    audit.finalize(root, allow_incomplete=incomplete, _managed=True)
    if reserve_terminal:
        outputs = ["synthesis/audit/initial-results.json", "synthesis/audit/audit-summary.json",
                   "synthesis/audit/validated_mentions.jsonl"]
        if state.get("audit_correction"):
            outputs.append("synthesis/audit/repairs.json")
        control.finish("audit", outputs)
    if incomplete:
        control.close(complete=False)
    return audit.verify_gate(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["read", "save", "merge", "audit-read", "audit-save", "audit-assess", "audit-repair-read", "audit-repair-save", "audit-finalize"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--input")
    parser.add_argument("--output", help="Also preserve full read output for recovery if tool display truncates.")
    parser.add_argument("--size", type=int, default=4)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--retry", action="store_true", help="Reserve the single repair for failed running items only.")
    args = parser.parse_args()
    try:
        if args.retry and args.action not in ("read", "audit-read"):
            raise ValueError("--retry is only available for extraction and initial audit reads.")
        if args.allow_incomplete and args.action != "audit-finalize":
            raise ValueError("--allow-incomplete is only available for audit-finalize.")
        if args.action in ("read", "audit-read", "audit-repair-read"):
            result = (read_audit_repairs(args.root, args.size) if args.action == "audit-repair-read" else
                      read_batch(args.root, args.size, args.retry, args.action == "audit-read"))
            if args.output:
                atomic_json(Path(args.root) / args.output, result)
        elif args.action == "audit-repair-save":
            if not args.input:
                raise ValueError("--input JSON file is required.")
            result = save_audit_repairs(args.root, input_path=args.input)
        elif args.action == "audit-assess":
            result = assess_audit(args.root)
        elif args.action in ("save", "audit-save"):
            if not args.input:
                raise ValueError("--input JSON file is required.")
            value = audit.read_json(Path(args.root) / args.input)
            result = (save_audits if args.action == "audit-save" else save_extractions)(args.root, value)
        else:
            result = merge_extractions(args.root) if args.action == "merge" else finalize_audit(args.root, args.allow_incomplete)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("errors"):
            raise SystemExit(1)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

"""Fixed-size semantic audit with immutable inputs and one surgical repair pass."""

import argparse
import copy
import json
import re
from collections import defaultdict
from pathlib import Path

from skill.scripts.run_control import atomic_json, digest, file_hash
from skill.scripts.validate_mentions import validate_mentions
from skill.scripts.verify_quotes import normalize_text, verify_quotes

SAMPLE_LIMIT = 20
SOURCE_LIMIT = 6
AGREEMENT_FLOOR = 0.90
SOURCE_DESCRIPTORS = {
    "sales_call": "sales call", "renewal_call": "renewal call",
    "churn_interview": "churn interview", "cs_call": "cs call",
    "nps_survey": "survey", "csat_survey": "survey", "general_survey": "survey",
    "review": "review", "support_ticket": "support ticket",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_records(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def min_pass_percent(request):
    """Read the explicit fixed-sample floor; existing requests remain at 90%."""
    percent = request.get("audit_min_pass_percent", 90)
    if type(percent) is not int or not 85 <= percent <= 100:
        raise ValueError("audit_min_pass_percent must be an integer from 85 to 100.")
    return percent


def _sources(root):
    paths = read_json(root / "synthesis/source-paths.json")
    return {fid: (root / name).read_text(encoding="utf-8") for fid, name in paths.items()}


def quote_source_descriptor(source_type, file_id):
    """Supply neutral source metadata without inventing a speaker's role."""
    return SOURCE_DESCRIPTORS.get(source_type, f"source: {file_id}")


def _attribution_errors(attribution, source):
    """Check literal source support, not whether the speaker owns the quoted turn."""
    # Only source descriptors are non-speaker metadata. Parenthesized titles
    # still need literal support, just like comma-separated titles.
    label = attribution.strip()
    suffix = re.search(r"\s*\(([^)]*)\)\s*$", label)
    if suffix:
        descriptor = suffix.group(1).strip()
        if descriptor.lower() == "customer success call":
            descriptor = "cs call"
        if re.fullmatch(r"(?:sales call|renewal call|churn interview|cs call|survey|review|support ticket|source: [A-Za-z0-9_]+)", descriptor, re.I):
            label = label[:suffix.start()].strip()
        else:
            label = label[:suffix.start()].strip() + ", " + descriptor
    if re.fullmatch(r"(?:unnamed )?customer", label, re.I):
        return []  # honest anonymous label; speaker/context remain semantic checks
    if not label:
        return ["Empty attribution; use an explicitly unnamed customer label."]
    pieces = [p.strip() for p in label.split(",") if p.strip()]
    text = normalize_text(source)
    return [f"Attribution lacks literal source support: {p}" for p in pieces
            if not re.search(r"(?<!\w)" + re.escape(normalize_text(p)) + r"(?!\w)", text)]


def _grounding_errors(row, source):
    """Collect every failed mention's literal checks from a structurally valid row."""
    errors = {}
    for mention, match in zip(row["mentions"], verify_quotes(row, source)):
        reasons = [] if match["found"] else ["quote does not occur in source."]
        reasons.extend(_attribution_errors(mention["quote_attribution"], source))
        if reasons:
            errors[mention["mention_id"]] = reasons
    return errors


def validate_records(records, plan, sources):
    expected = {f["file_id"] for f in plan["files"] if f.get("quality") == "good" and not f.get("excluded")}
    ids = [r["file_id"] for r in records]
    if set(ids) != expected or len(ids) != len(set(ids)):
        raise ValueError("Every active source needs one record, including an empty mentions array for zero signal.")
    seen = set()
    for row in records:
        fid = row["file_id"]
        errors, _ = validate_mentions(row, plan, fid)
        if errors:
            raise ValueError(f"{fid}: {errors}")
        for mention in row["mentions"]:
            mid = mention["mention_id"]
            if mid in seen:
                raise ValueError(f"Duplicate mention ID: {mid}")
            seen.add(mid)
        grounding = _grounding_errors(row, sources[fid])
        if grounding:
            raise ValueError("; ".join(f"{mid}: {'; '.join(reasons)}" for mid, reasons in grounding.items()))


def _snapshot(root):
    plan = read_json(root / "synthesis/plan.json")
    records = read_records(root / "synthesis/pipeline/mentions.jsonl")
    sources = _sources(root)
    source_ids = [f["file_id"] for f in plan["files"]]
    if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(sources):
        raise ValueError("Plan must account for every inventoried source exactly once, including explicit exclusions.")
    request = read_json(root / "synthesis/request.json")
    if "questions" in request and plan["questions"] != request["questions"]:
        raise ValueError("Plan questions differ from the locked request.")
    if "business_context" in request and plan["business_context"] != request["business_context"]:
        raise ValueError("Plan context differs from the locked request.")
    validate_records(records, plan, sources)
    context = digest(plan)
    mentions = {}
    types = {f["file_id"]: f["source_type"] for f in plan["files"]}
    for row in records:
        fid = row["file_id"]
        for item in row["mentions"]:
            fp = digest({"file_id": fid, "mention": item, "source": digest(sources[fid]), "plan": context})
            mentions[item["mention_id"]] = {"file_id": fid, "mention": item, "fingerprint": fp, "source_type": types[fid]}
    # Executive presentation cannot change the source-bound semantic judgment.
    # Exclude only this downstream field; RunControl still locks the full request
    # and package, so changing it within an existing run remains prohibited.
    audit_request = {k: v for k, v in request.items() if k != "executive_mode"}
    identity = digest({"plan": plan, "request": audit_request, "records": records, "sources": {k: digest(v) for k, v in sources.items()}})
    return plan, records, mentions, identity


def _check_limits(limit, source_limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SAMPLE_LIMIT:
        raise ValueError(f"Sample size must be 1..{SAMPLE_LIMIT}.")
    if isinstance(source_limit, bool) or not isinstance(source_limit, int) or not 1 <= source_limit <= SOURCE_LIMIT:
        raise ValueError(f"Source limit must be 1..{SOURCE_LIMIT}.")


def _cell(entry):
    return entry["mention"]["question_id"], entry["source_type"]


def select_fixed_sample(mentions, limit=SAMPLE_LIMIT, source_limit=SOURCE_LIMIT):
    """Choose sources for cell coverage, then mentions for cell/source diversity.

    This deliberately clustered, deterministic sample bounds expensive source
    rereads. It is not a random sample or an estimate of population accuracy.
    """
    _check_limits(limit, source_limit)
    source_cells = defaultdict(set)
    source_mentions = defaultdict(list)
    for mid, entry in mentions.items():
        source_cells[entry["file_id"]].add(_cell(entry))
        source_mentions[entry["file_id"]].append(mid)
    chosen, covered = [], set()
    while len(chosen) < min(source_limit, limit, len(source_cells)):
        # Prefer uncovered cells; stable hashes break ties independently of
        # filesystem/dict order and avoid privileging alphabetically early IDs.
        fid = min((fid for fid in source_cells if fid not in chosen),
                  key=lambda fid: (-len(source_cells[fid] - covered),
                                   digest({"source_sample": "bootcamp-v2", "id": fid})))
        chosen.append(fid)
        covered.update(source_cells[fid])
    remaining = {mid for fid in chosen for mid in source_mentions[fid]}
    cell_counts, source_counts = defaultdict(int), defaultdict(int)
    sample = []
    while remaining and len(sample) < limit:
        mid = min(remaining, key=lambda mid: (
            bool(cell_counts[_cell(mentions[mid])]),
            bool(source_counts[mentions[mid]["file_id"]]),
            cell_counts[_cell(mentions[mid])], source_counts[mentions[mid]["file_id"]],
            digest({"mention_sample": "bootcamp-v2", "id": mid})))
        sample.append(mid)
        remaining.remove(mid)
        cell_counts[_cell(mentions[mid])] += 1
        source_counts[mentions[mid]["file_id"]] += 1
    return sample


def _coverage(plan, mentions, sample):
    cells = {_cell(entry) for entry in mentions.values()}
    covered = {_cell(mentions[mid]) for mid in sample}
    source_ids = sorted({mentions[mid]["file_id"] for mid in sample})
    def describe(items):
        return [{"question_id": qid, "source_type": stype} for qid, stype in sorted(items)]
    return {"sampled_source_ids": source_ids, "sampled_source_count": len(source_ids),
            "source_population": sum(f.get("quality") == "good" and not f.get("excluded") for f in plan["files"]),
            "sources_with_mentions": len({e["file_id"] for e in mentions.values()}),
            "covered_cells": describe(covered), "uncovered_cells": describe(cells - covered)}


def _manifest(plan, mentions, identity, mode, limit, source_limit):
    _check_limits(limit, source_limit)
    sample = select_fixed_sample(mentions, limit, source_limit) if mode == "sampled" else []
    return {"version": 2, "mode": mode, "identity": identity, "sample_limit": limit,
            "source_limit": source_limit, "sample_ids": sample, "population": len(mentions),
            "fingerprints": {mid: entry["fingerprint"] for mid, entry in mentions.items()},
            **_coverage(plan, mentions, sample)}


def prepare(root, mode="sampled", limit=SAMPLE_LIMIT, source_limit=SOURCE_LIMIT):
    root = Path(root).resolve()
    if mode not in ("sampled", "skip"):
        raise ValueError("Audit mode must be sampled or skip. Deep mode is not automatic.")
    plan, records, mentions, identity = _snapshot(root)
    request = read_json(root / "synthesis/request.json")
    min_pass_percent(request)
    if request.get("audit_mode", "sampled") != mode:
        raise ValueError("Mode differs from the recorded user request. Do not silently skip or escalate.")
    if limit != request.get("audit_sample_limit", SAMPLE_LIMIT):
        raise ValueError("Sample limit differs from the locked request.")
    if source_limit != request.get("audit_source_limit", SOURCE_LIMIT):
        raise ValueError("Source limit differs from the locked request.")
    manifest = _manifest(plan, mentions, identity, mode, limit, source_limit)
    path = root / "synthesis/audit/audit-manifest.json"
    if path.exists():
        if read_json(path) != manifest:
            raise ValueError("Audit manifest already locked. New sampling or changed inputs require a new run.")
        return manifest
    tasks = defaultdict(list)
    for mid in manifest["sample_ids"]:
        entry = mentions[mid]
        tasks[entry["file_id"]].append({"mention_id": mid, "fingerprint": entry["fingerprint"], "mention": entry["mention"]})
    paths = read_json(root / "synthesis/source-paths.json")
    for fid, items in tasks.items():
        atomic_json(root / "synthesis/audit/tasks" / f"{fid}.json",
                    {"file_id": fid, "source_path": paths[fid], "questions": plan["questions"],
                     "business_context": plan["business_context"], "mentions": items})
    atomic_json(path, manifest)
    return manifest


def _key_results(results):
    if not isinstance(results, list):
        raise ValueError("Audit results must be an array.")
    keyed = {}
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("mention_id"), str):
            raise ValueError("Malformed audit result.")
        if row["mention_id"] in keyed:
            raise ValueError("Duplicate audit verdict; do not overwrite earlier judgments.")
        if row.get("verdict") not in ("pass", "fail", "uncertain") or not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Each verdict needs pass/fail/uncertain and a nonempty reason.")
        keyed[row["mention_id"]] = row
    return keyed


def evaluate(root, initial=None, repairs=None):
    root = Path(root).resolve()
    plan, records, mentions, identity = _snapshot(root)
    manifest = read_json(root / "synthesis/audit/audit-manifest.json")
    request = read_json(root / "synthesis/request.json")
    minimum = min_pass_percent(request)
    mode = request.get("audit_mode", "sampled")
    if manifest.get("mode") not in ("sampled", "skip") or manifest["mode"] != mode:
        raise ValueError("Audit mode differs from the recorded user choice.")
    if identity != manifest["identity"]:
        raise ValueError("Audit inputs changed; cached verdicts are stale.")
    limit = request.get("audit_sample_limit", SAMPLE_LIMIT)
    if manifest["sample_limit"] != limit:
        raise ValueError("Audit sample limit differs from the locked request.")
    source_limit = request.get("audit_source_limit", SOURCE_LIMIT)
    if manifest.get("source_limit") != source_limit:
        raise ValueError("Audit source limit differs from the locked request.")
    if manifest != _manifest(plan, mentions, identity, mode, limit, source_limit):
        raise ValueError("Audit manifest altered; refusing a new sample or stale verdicts.")
    first = _key_results(initial or [])
    fixes = _key_results(repairs or [])
    selected = set(manifest["sample_ids"])
    if set(first) != selected:
        raise ValueError("Initial results must cover the exact fixed sample. Missing or extra verdicts stop the run.")
    for mid, row in first.items():
        if row.get("fingerprint") != mentions[mid]["fingerprint"]:
            raise ValueError(f"Stale initial verdict: {mid}")
    failures = {mid for mid, row in first.items() if row["verdict"] != "pass"}
    if not set(fixes) <= failures:
        raise ValueError("Repair only originally failed/uncertain sample items, once.")
    corrected = {mid: entry["mention"] for mid, entry in mentions.items()}
    excluded = set(failures)
    passed = len(selected) - len(failures)
    sources = _sources(root)
    for mid, row in fixes.items():
        entry = mentions[mid]
        if row.get("fingerprint") != entry["fingerprint"]:
            raise ValueError(f"Stale repair: {mid}")
        replacement = row.get("replacement")
        if row["verdict"] == "pass":
            if not isinstance(replacement, dict) or replacement.get("mention_id") != mid or replacement.get("question_id") != entry["mention"]["question_id"]:
                raise ValueError("A passing repair needs a full replacement with unchanged mention/question IDs.")
            check = {"file_id": entry["file_id"], "mentions": [replacement]}
            errors, _ = validate_mentions(check, plan, entry["file_id"])
            errors += _attribution_errors(replacement.get("quote_attribution", ""), sources[entry["file_id"]])
            if errors or any(not v["found"] for v in verify_quotes(check, sources[entry["file_id"]])):
                raise ValueError(f"Repair failed mechanical grounding: {mid}: {errors}")
            if row.get("replacement_fingerprint") != digest(replacement):
                raise ValueError("Recheck must identify the exact corrected mention content.")
            corrected[mid] = replacement
            excluded.remove(mid)
            passed += 1
        elif replacement is not None:
            raise ValueError("Failed/uncertain repairs are excluded; replacement must be null.")
    output = copy.deepcopy(records)
    for source in output:
        source["mentions"] = [corrected[m["mention_id"]] for m in source["mentions"] if m["mention_id"] not in excluded]
    validate_records(output, plan, sources)
    n = len(selected)
    rate = passed / n if n else None
    raw_rate = (n - len(failures)) / n if n else None
    ready = bool(mentions) and (manifest["mode"] == "skip" or rate is not None and passed * 100 >= n * minimum)
    coverage = _coverage(plan, mentions, manifest["sample_ids"])
    uncovered = ", ".join(f'{c["question_id"]}/{c["source_type"]}' for c in coverage["uncovered_cells"]) or "none among cells containing mentions"
    limitations = ("Semantic audit skipped by user; mechanical quote/source-label checks retained. " if manifest["mode"] == "skip" else
                   "Fixed sample agreement is not a guarantee of whole-dataset accuracy. Unselected mentions were not semantically audited. Excluded items remain failures in the original sample denominator. ")
    limitations += (f'Semantic audit covered {n} of {len(mentions)} mentions from {coverage["sampled_source_count"]} '
                    f'of {coverage["source_population"]} active source files. '
                    f'Uncovered question/source-type cells: {uncovered}.')
    summary = {"version": 2, "mode": manifest["mode"], "status": "ready" if ready else "incomplete",
               "identity": identity, "sample_size": n, "population": len(mentions),
               "sample_limit": limit, "source_limit": source_limit, **coverage,
               "passed": passed, "raw_pass_rate": raw_rate, "corrected_pass_rate": rate,
               "excluded": len(excluded), "excluded_ids": sorted(excluded),
               "unassessed": len(mentions) - n, "repair_count": len(fixes),
               "initial_hash": digest(initial or []), "repairs_hash": digest(repairs or []),
               "validated_hash": digest(output),
               "limitations": limitations}
    if minimum != 90:
        # Default summaries remain byte-equivalent to historical sealed gates.
        summary["audit_min_pass_percent"] = minimum
        summary["limitations"] += f" Declared fixed-sample agreement threshold: {minimum}%."
    return summary, output


def managed_evidence(root):
    """Rebuild managed evidence from ledger-bound source receipts, never aggregates."""
    from skill.scripts.run_control import RunControl
    root = Path(root).resolve()
    control = RunControl(root)
    state = control.status()
    manifest = read_json(root / "synthesis/audit/audit-manifest.json")
    entries = _snapshot(root)[2]
    source_ids = sorted({entries[mid]["file_id"] for mid in manifest["sample_ids"]})
    initial, inputs = [], ["synthesis/audit/audit-manifest.json", "synthesis/pipeline/mentions.jsonl"]
    for fid in source_ids:
        path = f"synthesis/audit/results/{fid}.json"
        step = state["steps"].get(f"audit-initial:{fid}", {})
        if step.get("status") != "complete" or path not in step.get("outputs", {}):
            raise ValueError("All selected sources need complete initial verdicts.")
        initial.extend(read_json(root / path))
        inputs.append(path)
    summary, _ = evaluate(root, initial)
    first = _key_results(initial)
    groups = {}
    for mid in sorted(summary["excluded_ids"]):
        groups.setdefault(entries[mid]["file_id"], []).append(mid)
    expected = {"initial_hash": digest(initial), "groups": groups}
    anchor = state.get("audit_correction")
    fixes, pending = [], []
    repair_keys = {k.split(":", 1)[1] for k in state["steps"] if k.startswith("audit-repair:")}
    if anchor:
        path = anchor["path"]
        if file_hash(root / path) != anchor["hash"] or read_json(root / path) != expected:
            raise ValueError("Changed correction cohort; the original failed set is immutable.")
        inputs.append(path)
        if not repair_keys <= set(groups):
            raise ValueError("Correction reservation is outside the frozen failed cohort.")
        for fid, submission in anchor.get("submissions", {}).items():
            if fid not in repair_keys or file_hash(root / submission["path"]) != submission["hash"]:
                raise ValueError("Changed/missing correction submission receipt.")
        for fid in groups:
            step = state["steps"].get(f"audit-repair:{fid}")
            if step:
                if step["attempts"] != 1 or step["inputs"] != control._hash_paths(inputs):
                    raise ValueError("Correction permits one source draft with unchanged cohort inputs.")
            if not step or step["status"] != "complete":
                pending.append(fid)
                continue
            submission = anchor.get("submissions", {}).get(fid)
            output = f"synthesis/audit/corrections/{fid}.json"
            if not submission or set(step["outputs"]) != {submission["path"], output}:
                raise ValueError("A correction requires its original submission and accepted source receipt.")
            rows = normalize_repairs(read_json(root / submission["path"])["value"], groups[fid])
            if rows != read_json(root / output):
                raise ValueError("Correction receipt differs from its frozen model draft.")
            fixes.extend(rows)
    elif repair_keys:
        raise ValueError("Correction reservations require an anchored failed cohort.")
    for name, expected_rows in (("initial-results.json", initial), ("repairs.json", fixes)):
        path = root / "synthesis/audit" / name
        if path.exists() and read_json(path) != expected_rows:
            raise ValueError(f"Unbound or changed {name}; use completed source receipts.")
    # Reuse the same validator for both save and finalization, including literal grounding.
    summary, _ = evaluate(root, initial, fixes)
    return {"state": state, "initial": initial, "repairs": fixes, "inputs": inputs,
            "cohort": expected, "pending": pending, "summary": summary}


def normalize_repairs(rows, expected_ids):
    """Only add deterministic metadata after the original payload has been frozen."""
    keyed = _key_results(rows)
    if set(keyed) != set(expected_ids):
        raise ValueError("Corrections must cover every original failed ID for this source exactly.")
    result = copy.deepcopy(rows)
    for row in result:
        if "replacement" not in row:
            raise ValueError("Every correction needs replacement: a full mention or null.")
        if isinstance(row["replacement"], dict) and "replacement_fingerprint" not in row:
            row["replacement_fingerprint"] = digest(row["replacement"])
    return result


def _final_evidence(root):
    folder = root / "synthesis/audit"
    managed = managed_evidence(root) if (root / "synthesis/run-state.json").exists() else None
    initial = managed["initial"] if managed else (read_json(folder / "initial-results.json") if (folder / "initial-results.json").exists() else [])
    repairs = managed["repairs"] if managed else (read_json(folder / "repairs.json") if (folder / "repairs.json").exists() else [])
    summary, output = evaluate(root, initial, repairs)
    if managed and managed["state"].get("audit_incomplete_reason"):
        summary.update(status="incomplete", incomplete_reason=managed["state"]["audit_incomplete_reason"])
    return summary, output, managed


def finalize(root, allow_incomplete=False, *, _managed=False):
    root = Path(root).resolve()
    if not _managed and (root / "synthesis/run-state.json").exists():
        from skill.scripts.batch_extract import finalize_audit
        result = finalize_audit(root, allow_incomplete)
        return result["summary"] if result.get("summary") and "next_action" not in result else result
    folder = root / "synthesis/audit"
    path = folder / "audit-summary.json"
    summary, output, managed = _final_evidence(root)
    if path.exists():
        if read_json(path) != summary:
            raise ValueError("Audit is already finalized. No new audit or repair cycles in this run.")
        if digest(read_records(folder / "validated_mentions.jsonl")) != digest(output):
            raise ValueError("Finalized audit output changed.")
        return summary  # Preserve already-sealed artifacts, including legacy incomplete audits.
    if managed and managed["state"]["status"] in ("incomplete", "complete", "paused"):
        raise ValueError("Cannot seal a new audit on a closed or paused run.")
    if managed and managed["pending"] and not allow_incomplete:
        return {"complete": False, "status": "repair_pending", "pending_sources": managed["pending"]}
    if summary["status"] != "ready" and not allow_incomplete:
        return {"complete": False, "status": "repair_required", "summary": summary}
    if managed:
        if allow_incomplete and (managed["pending"] or summary["status"] != "ready"):
            from skill.scripts.run_control import RunControl
            control = RunControl(root)
            state = control.status()
            minimum = min_pass_percent(read_json(root / "synthesis/request.json"))
            state.setdefault("audit_incomplete_reason", f"Correction declined, unfinished, or below the fixed {minimum}% threshold.")
            atomic_json(control.path, state)
            summary.update(status="incomplete", incomplete_reason=state["audit_incomplete_reason"])
        atomic_json(folder / "initial-results.json", managed["initial"])
        if managed["state"].get("audit_correction"):
            atomic_json(folder / "repairs.json", managed["repairs"])
    destination = folder / "validated_mentions.jsonl"
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in output)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(destination)
    atomic_json(path, summary)
    return summary


def verify_gate(root):
    root = Path(root).resolve()
    try:
        saved = read_json(root / "synthesis/audit/audit-summary.json")
        expected, output, managed = _final_evidence(root)
        if saved != expected or digest(read_records(root / "synthesis/audit/validated_mentions.jsonl")) != digest(output):
            raise ValueError("Audit summary or validated data does not match source-bound evidence.")
        complete = expected["status"] == "ready"
        if managed and (managed["pending"] or managed["state"]["status"] == "incomplete"):
            complete = False
        return {"complete": complete, "reason": "Bounded audit ready" if complete else "Audit below threshold or incomplete; deliver an incomplete handoff.", "summary": expected}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"complete": False, "reason": str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "finalize", "gate"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--mode", choices=["sampled", "skip"], default="sampled")
    parser.add_argument("--limit", type=int, default=SAMPLE_LIMIT)
    parser.add_argument("--source-limit", type=int, default=SOURCE_LIMIT)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.root, args.mode, args.limit, args.source_limit)
        elif args.action == "finalize":
            result = finalize(args.root, args.allow_incomplete)
        else:
            result = verify_gate(args.root)
        print(json.dumps(result, indent=2))
        if result.get("complete") is False or result.get("status") == "incomplete":
            parser.exit(2)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Audit stopped: {exc}\n")


if __name__ == "__main__":
    main()

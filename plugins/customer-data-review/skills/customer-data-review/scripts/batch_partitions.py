"""Parent-only question partitions: immutable worker inputs and one global repair.

No model calls or scheduler. A reservation authorizes exactly one immediate host
invocation; observing it again is recovery, never another dispatch permission.
"""

import argparse
import copy
import json
import uuid
from pathlib import Path

from skill.scripts import batch_report as batch
from skill.scripts.bounded_audit import read_json, read_records
from skill.scripts.run_control import RunControl, atomic_json, file_hash, MAX_IN_FLIGHT, validate_execution_mode

MODE = "question_partitions_v1"
STAGES = {"analysis": "phase-3", "phase-3": "phase-3", "sections": "sections"}
TERMINAL = {"valid", "invalid", "failed"}


def _root_stage(root, stage):
    root = Path(root).resolve()
    if stage not in STAGES:
        raise ValueError("Partition stage must be analysis or sections.")
    request = read_json(root / "synthesis/request.json")
    validate_execution_mode(request)
    if request.get("downstream_execution") != MODE or request.get("writing_mode") != "section_packets_v1":
        raise ValueError("Partition helpers require locked question_partitions_v1 execution and section_packets_v1 writing.")
    return root, STAGES[stage], request


def _folder(stage):
    return f"synthesis/partitions/{stage}"


def _registry_path(stage):
    return f"{_folder(stage)}/registry.json"


def _inside(root, path):
    resolved = (root / path).resolve()
    resolved.relative_to(root)
    return resolved


def _save(root, stage, registry, require_active=False):
    # Single writer. A crash between these atomic writes fails closed against
    # the anchor; do not repair it by editing ledgers or reissuing reservations.
    control = RunControl(root)
    state = control.status()
    if require_active and state["status"] != "active":
        raise ValueError(f"No new fragment dispatch: run is {state['status']}.")
    path = _registry_path(stage)
    atomic_json(root / path, registry)
    state.setdefault("partition_registries", {})[stage] = file_hash(root / path)
    atomic_json(control.path, state)


def _load(root, stage):
    control = RunControl(root)
    state = control.status()
    path = _registry_path(stage)
    if state.get("partition_registries", {}).get(stage) != file_hash(root / path):
        raise ValueError("Partition registry changed or its last write was interrupted; stop incomplete, do not reset it.")
    registry = read_json(root / path)
    if registry["stage"] != stage or registry["run_identity"] != state["identity"]:
        raise ValueError("Partition registry belongs to a different run/stage.")
    for name, expected in registry["tracked"].items():
        if not _inside(root, name).is_file() or file_hash(root / name) != expected:
            raise ValueError(f"Changed/missing immutable partition artifact: {name}")
    actual = state["steps"][stage]["attempts"]
    recovering_claim = (registry.get("repair_intent") and registry["attempt"] == 1 and actual == 2)
    if actual != registry["attempt"] and not recovering_claim:
        raise ValueError("Partition/global attempts diverged; do not reset either allowance.")
    return registry, state


def _write_new(root, registry, path, value=None, raw=None):
    target = _inside(root, path)
    content = raw if raw is not None else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if target.exists() and target.read_bytes() != content:
        raise ValueError(f"Cannot replace immutable partition artifact: {path}")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    registry["tracked"][path] = file_hash(target)


def _project(bundle, stage, qid):
    result = copy.deepcopy(bundle)
    for key in ("action", "attempt", "outputs"):
        result.pop(key, None)
    if stage == "phase-3":
        result["questions"] = [q for q in result["questions"] if q["id"] == qid]
    else:
        inventory = result["inventory"]
        inventory["questions"] = [q for q in inventory["questions"] if q["question_id"] == qid]
        refs = {c["context_ref"] for q in inventory["questions"] for c in q["quote_candidates"]}
        inventory["source_contexts"] = {key: value for key, value in inventory["source_contexts"].items() if key in refs}
    result["worker_scope"] = (
        f"Your only question is {qid}. Return exactly {{questions: [one question object]}} for it. "
        "This overrides common instructions about every locked question/all sections: they mean only the supplied question here. "
        "Read all supplied evidence; do not read the combined bundle, other questions or implementation files. "
        "Produce one draft only. Do not call prepare, claim, save, finish, retry or other ledger commands. "
        "Write only your assigned staging output; parent owns validation and any globally reserved repair."
    )
    return result


def _create_input(root, stage, registry, qid, attempt):
    payload = {"stage": stage, "global_attempt": attempt, "question_id": qid,
               "model_input": _project(read_json(root / registry["bundle"]), stage, qid)}
    if attempt == 2:
        old = registry["fragments"][f"1:{qid}"]
        payload["repair"] = {"errors": old["errors"],
                             "previous_submission": ((root / old["submission"]).read_text(errors="replace")
                                                     if old.get("submission") else None),
                             "instruction": "Fix this failed question once; preserve its already-correct material. No new research or redesign."}
    path = f"{_folder(stage)}/inputs/{attempt}-{qid}.json"
    _write_new(root, registry, path, payload)
    registry["inputs"][f"{attempt}:{qid}"] = path


def _summary(root, registry):
    questions = []
    for qid in registry["questions"]:
        key = f"{registry['attempt']}:{qid}"
        if registry["attempt"] == 2 and qid not in registry["repair_questions"]:
            key = f"1:{qid}"
        path = registry["inputs"][key]
        entry = registry["fragments"].get(key)
        questions.append({"question_id": qid, "attempt": int(key.split(":")[0]),
                          "input_path": path, "input_sha256": registry["tracked"][path],
                          "input_bytes": (root / path).stat().st_size,
                          "state": entry["status"] if entry else "unreserved",
                          "dispatch_id": entry["dispatch_id"] if entry else None,
                          "worker_handle": entry.get("worker_handle") if entry else None,
                          "errors": entry.get("errors", []) if entry else []})
    return {"stage": registry["stage"], "attempt": registry["attempt"],
            "sealed": registry.get("sealed", False), "questions": questions,
            "repair_questions": registry["repair_questions"],
            "dispatch_reservations": len(registry["fragments"]),
            "submitted_fragments": sum(bool(v.get("submission")) for v in registry["fragments"].values()),
            "worker_handles_recorded": sum(bool(v.get("worker_handle")) for v in registry["fragments"].values()),
            "note": "Reservations count attempted dispatches, not verified provider usage. Pending is not permission to relaunch."}


def prepare(root, stage):
    root, stage, request = _root_stage(root, stage)
    if (root / _registry_path(stage)).exists():
        registry, _ = _load(root, stage)
        return _summary(root, registry)
    if stage in RunControl(root).status().get("partition_registries", {}):
        raise ValueError("Missing anchored partition registry; stop incomplete, never reset dispatched attempts.")
    bundle = (batch._prepare_analysis(root) if stage == "phase-3" else batch._prepare_sections(root))
    state = RunControl(root).status()
    if state["steps"][stage]["attempts"] != 1 or bundle["action"] == "reuse":
        raise ValueError("Cannot adopt an existing completed/repaired stage into a new partition registry.")
    # A prepare resumed before registry persistence changes its action receipt,
    # not its evidence. Freeze only stable model inputs so that recovery can
    # reuse already-written canonical bytes without resetting the stage claim.
    bundle = {key: value for key, value in bundle.items() if key not in {"action", "attempt", "outputs"}}
    qids = [q["id"] for q in read_json(root / batch.PLAN)["questions"]]
    registry = {"version": 1, "stage": stage, "run_identity": state["identity"],
                "attempt": 1, "questions": qids, "inputs": {}, "fragments": {},
                "tracked": {}, "repair_questions": [], "repair_intent": False,
                "bundle": f"{_folder(stage)}/full-input.json", "sealed": False}
    _write_new(root, registry, registry["bundle"], bundle)
    for qid in qids:
        _create_input(root, stage, registry, qid, 1)
    _save(root, stage, registry)
    return _summary(root, registry)


def status(root, stage):
    root, stage, _ = _root_stage(root, stage)
    registry, _ = _load(root, stage)
    return _summary(root, registry)


def _capacity(request, estimate, expected_hash):
    required = {"input_sha256", "input_tokens_estimate", "output_tokens_reserve", "overhead_tokens", "estimate_method"}
    if not isinstance(estimate, dict) or set(estimate) != required or estimate["input_sha256"] != expected_hash:
        raise ValueError("Capacity estimate must bind the complete canonical input hash and documented fields.")
    for key in ("input_tokens_estimate", "output_tokens_reserve", "overhead_tokens"):
        if type(estimate[key]) is not int or estimate[key] < (0 if key == "overhead_tokens" else 1):
            raise ValueError("Capacity estimates/reserves must be nonnegative integers; input/output must be positive.")
    if not isinstance(estimate["estimate_method"], str) or not estimate["estimate_method"].strip():
        raise ValueError("Document the capacity estimation method; estimates are not measured usage.")
    limits = request["partition_capacity"]
    if estimate["output_tokens_reserve"] > limits["max_output_tokens"] or sum(estimate[k] for k in
            ("input_tokens_estimate", "output_tokens_reserve", "overhead_tokens")) > limits["context_window_tokens"]:
        raise ValueError("Complete question input plus planned output/overhead exceeds declared capacity; never truncate evidence.")


def reserve(root, stage, question, capacity):
    root, stage, request = _root_stage(root, stage)
    registry, state = _load(root, stage)
    if registry["sealed"]:
        return {"action": "reuse", "stage": stage}
    key = f"{registry['attempt']}:{question}"
    if key in registry["fragments"]:
        return {"action": "pending" if registry["fragments"][key]["status"] == "dispatched" else "reuse",
                "question_id": question, "may_dispatch": False}
    if question not in registry["questions"] or (registry["attempt"] == 2 and question not in registry["repair_questions"]):
        raise ValueError("Question is outside this attempt's frozen cohort.")
    if registry["repair_intent"] and registry["attempt"] == 1:
        raise ValueError("Complete repair registration before dispatch.")
    if state["status"] != "active":
        raise ValueError(f"No new fragment dispatch: run is {state['status']}.")
    if sum(e["status"] == "dispatched" for e in registry["fragments"].values()) >= MAX_IN_FLIGHT:
        raise ValueError("At most four fragment reservations may be in flight.")
    path = registry["inputs"][key]
    _capacity(request, capacity, registry["tracked"][path])
    dispatch_id = uuid.uuid4().hex
    output = f"{_folder(stage)}/staging/{registry['attempt']}-{question}-{dispatch_id}.json"
    registry["fragments"][key] = {"dispatch_id": dispatch_id, "attempt": registry["attempt"],
                                "question_id": question, "input": path, "input_sha256": registry["tracked"][path],
                                "output_path": output, "status": "dispatched", "errors": [],
                                "capacity_estimate": copy.deepcopy(capacity), "reserved_at": RunControl(root).clock()}
    _save(root, stage, registry, require_active=True)
    return {"action": "dispatch", "may_dispatch": True, "dispatch_id": dispatch_id,
            "stage": stage, "attempt": registry["attempt"], "question_id": question,
            "input_path": path, "input_sha256": registry["tracked"][path], "output_path": output,
            "instruction": "Launch exactly once now in a fresh context. Preserve the worker handle; an uncertain launch cannot be silently repeated."}


def _entry(registry, question, dispatch_id):
    matches = [entry for entry in registry["fragments"].values()
               if entry["question_id"] == question and entry["dispatch_id"] == dispatch_id]
    if len(matches) != 1:
        raise ValueError("Unknown question/dispatch identity.")
    return matches[0]


def attach(root, stage, question, dispatch_id, worker_handle):
    root, stage, _ = _root_stage(root, stage)
    registry, _ = _load(root, stage)
    entry = _entry(registry, question, dispatch_id)
    if not isinstance(worker_handle, str) or not worker_handle.strip():
        raise ValueError("Worker handle must be nonempty.")
    if entry.get("worker_handle") == worker_handle:
        return {"action": "reuse"}
    if registry["sealed"] or entry["status"] != "dispatched" or entry.get("worker_handle"):
        raise ValueError("Cannot replace a worker handle or attach to a terminal fragment.")
    entry["worker_handle"] = worker_handle
    _save(root, stage, registry)
    return {"action": "recorded"}


def _validate(root, stage, question, raw):
    try:
        draft = json.loads(raw)
        batch._shape(draft, {"questions"}, label="question fragment")
        rows = batch._questions(draft["questions"], {"questions": [{"id": question}]})
        if stage == "phase-3":
            batch.validate_analysis_question(rows[question], question, read_records(root / batch.RETAINED))
        else:
            from skill.scripts.section_evidence import admit_section_packets, _digest
            inventory = read_json(root / batch.INVENTORY)
            inventory["questions"] = [q for q in inventory["questions"] if q["question_id"] == question]
            inventory["evidence_digest"] = _digest({k: v for k, v in inventory.items() if k != "evidence_digest"})
            admit_section_packets(draft, inventory)
        return []
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc)]


def _section_admission(root, question, raw):
    from skill.scripts.section_evidence import admit_section_packets, _digest
    inventory = read_json(root / batch.INVENTORY)
    inventory["questions"] = [q for q in inventory["questions"] if q["question_id"] == question]
    inventory["evidence_digest"] = _digest({k: v for k, v in inventory.items() if k != "evidence_digest"})
    return admit_section_packets(json.loads(raw), inventory)


def _verify_fragment_admission(root, entry):
    expected = _section_admission(root, entry["question_id"], (root / entry["submission"]).read_bytes())
    if read_json(root / entry["admission"]) != expected or read_json(root / entry["accepted"]) != expected["accepted"]:
        raise ValueError("Fragment admission differs from its immutable raw-to-accepted derivation.")


def submit(root, stage, question, dispatch_id, input_path):
    root, stage, _ = _root_stage(root, stage)
    registry, state = _load(root, stage)
    entry = _entry(registry, question, dispatch_id)
    if _inside(root, input_path) != _inside(root, entry["output_path"]):
        raise ValueError("Submission must use the output path bound to this dispatch.")
    raw = _inside(root, input_path).read_bytes()
    if entry.get("submission"):
        if (root / entry["submission"]).read_bytes() != raw:
            raise ValueError("Changed worker output requires the global repair; frozen fragment bytes cannot be replaced.")
        if entry["status"] != "dispatched":
            if stage == "sections" and entry["status"] == "valid":
                _verify_fragment_admission(root, entry)
            return {"action": "reuse", "status": entry["status"], "errors": entry["errors"]}
    if registry["sealed"] or entry["status"] != "dispatched" or state["status"] not in {"active", "exhausted"}:
        raise ValueError("Only an in-flight fragment may submit on an active/exhausted run.")
    path = f"{_folder(stage)}/submissions/{entry['attempt']}-{question}.json"
    _write_new(root, registry, path, raw=raw)
    entry["submission"] = path
    # Freeze raw bytes before parsing, even if validation itself is interrupted.
    _save(root, stage, registry)
    entry["errors"] = _validate(root, stage, question, raw)
    entry["status"] = "invalid" if entry["errors"] else "valid"
    if stage == "sections" and not entry["errors"]:
        admission = _section_admission(root, question, raw)
        for label, value in (("admission", admission), ("accepted", admission["accepted"])):
            target = f"{_folder(stage)}/{label}/{entry['attempt']}-{question}.json"
            _write_new(root, registry, target, value)
            entry[label] = target
    _save(root, stage, registry)
    return {"status": entry["status"], "errors": entry["errors"], "question_id": question}


def fail(root, stage, question, dispatch_id, reason):
    root, stage, _ = _root_stage(root, stage)
    registry, state = _load(root, stage)
    entry = _entry(registry, question, dispatch_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Record the confirmed failure/reconciliation result, not an observation timeout.")
    if entry["status"] == "failed" and entry["errors"] == [reason]:
        return {"action": "reuse"}
    if registry["sealed"] or entry["status"] != "dispatched" or entry.get("submission") or state["status"] not in {"active", "exhausted"}:
        raise ValueError("Cannot fail a submitted/terminal fragment.")
    entry["status"], entry["errors"] = "failed", [reason]
    _save(root, stage, registry)
    return {"status": "failed", "question_id": question}


def repair(root, stage):
    root, stage, _ = _root_stage(root, stage)
    registry, state = _load(root, stage)
    if registry["sealed"] or registry["attempt"] == 2:
        return _summary(root, registry)
    initial = [registry["fragments"].get(f"1:{qid}") for qid in registry["questions"]]
    if any(not entry or entry["status"] not in TERMINAL for entry in initial):
        raise ValueError("Every initial question must finish or be explicitly failed before the one global repair.")
    failures = [entry["question_id"] for entry in initial if entry["status"] != "valid"]
    if not failures:
        raise ValueError("No failed questions justify a repair.")
    if registry["repair_intent"] and failures != registry["repair_questions"]:
        raise ValueError("The frozen failed-question repair cohort changed.")
    if not registry["repair_intent"]:
        registry["repair_intent"], registry["repair_questions"] = True, failures
        _save(root, stage, registry)
    # Recovery after claim but before registry advancement must not claim again.
    if state["steps"][stage]["attempts"] == 1:
        inputs = list(state["steps"][stage]["inputs"])
        batch._prepare(root, stage, inputs, repair=True)
    registry["attempt"] = 2
    for qid in failures:
        _create_input(root, stage, registry, qid, 2)
    _save(root, stage, registry)
    return _summary(root, registry)


def _effective(registry):
    selected = {}
    for qid in registry["questions"]:
        attempt = 2 if registry["attempt"] == 2 and qid in registry["repair_questions"] else 1
        entry = registry["fragments"].get(f"{attempt}:{qid}")
        if not entry or entry["status"] != "valid":
            raise ValueError(f"Question {qid} is missing, pending or failed; no partial assembly or extra repair is allowed.")
        selected[qid] = entry["submission"]
    return selected


def verify_assembly(root, stage, input_path=None):
    root, stage, _ = _root_stage(root, stage)
    registry, _ = _load(root, stage)
    if not registry["sealed"]:
        raise ValueError("Partition mode requires a sealed complete assembly; combined finish cannot bypass it.")
    selected = _effective(registry)
    if stage == "sections":
        for entry in registry["fragments"].values():
            if entry["status"] == "valid":
                _verify_fragment_admission(root, entry)
    assembled = f"{_folder(stage)}/assembled.json"
    if input_path is not None and _inside(root, input_path) != root / assembled:
        raise ValueError("Only the sealed partition assembly may finish this stage.")
    expected = {"questions": [read_json(root / selected[qid])["questions"][0] for qid in registry["questions"]]}
    if read_json(root / assembled) != expected:
        raise ValueError("Assembly does not exactly preserve selected fragment rows.")
    manifest_path = f"{_folder(stage)}/assembly-manifest.json"
    outputs = {**registry["tracked"], _registry_path(stage): file_hash(root / _registry_path(stage))}
    manifest = {"stage": stage, "attempt": registry["attempt"], "selected": selected,
                "outputs": outputs, "assembled": assembled}
    if read_json(root / manifest_path) != manifest:
        raise ValueError("Sealed assembly manifest changed or lacks exact coverage/hashes.")
    return [*outputs, manifest_path]


def finish(root, stage):
    root, stage, _ = _root_stage(root, stage)
    registry, _ = _load(root, stage)
    selected = _effective(registry)
    if stage == "sections":
        for entry in registry["fragments"].values():
            if entry["status"] == "valid":
                _verify_fragment_admission(root, entry)
    assembled = f"{_folder(stage)}/assembled.json"
    if not registry["sealed"]:
        draft = {"questions": [read_json(root / selected[qid])["questions"][0] for qid in registry["questions"]]}
        _write_new(root, registry, assembled, draft)
        registry["sealed"] = True
        _save(root, stage, registry)
    # The manifest is deterministic and can be recreated after a pre-finish crash.
    manifest_path = f"{_folder(stage)}/assembly-manifest.json"
    manifest = {"stage": stage, "attempt": registry["attempt"], "selected": selected,
                "outputs": {**registry["tracked"], _registry_path(stage): file_hash(root / _registry_path(stage))},
                "assembled": assembled}
    if (root / manifest_path).exists() and read_json(root / manifest_path) != manifest:
        raise ValueError("Assembly manifest changed; do not replace it.")
    if not (root / manifest_path).exists():
        atomic_json(root / manifest_path, manifest)
    verify_assembly(root, stage, assembled)
    return (batch.finish_analysis(root, assembled) if stage == "phase-3" else batch.finish_sections(root, assembled))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "status", "reserve", "attach", "submit", "fail", "repair", "finish"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--stage", choices=["analysis", "sections"], required=True)
    parser.add_argument("--question")
    parser.add_argument("--capacity", help="Host capacity estimate JSON; not token usage")
    parser.add_argument("--dispatch-id")
    parser.add_argument("--worker-handle")
    parser.add_argument("--input")
    parser.add_argument("--reason")
    args = parser.parse_args()
    try:
        if args.action in {"prepare", "status", "repair", "finish"}:
            result = globals()[args.action](args.root, args.stage)
        elif args.action == "reserve":
            if not args.question or not args.capacity:
                raise ValueError("reserve needs --question and --capacity")
            result = reserve(args.root, args.stage, args.question, read_json(_inside(Path(args.root).resolve(), args.capacity)))
        else:
            value = {"attach": args.worker_handle, "submit": args.input, "fail": args.reason}[args.action]
            if not args.question or not args.dispatch_id or not value:
                raise ValueError("Supply question, dispatch identity and the action's value/input.")
            result = globals()[args.action](args.root, args.stage, args.question, args.dispatch_id, value)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Partition work stopped: {exc}\n")


if __name__ == "__main__":
    main()

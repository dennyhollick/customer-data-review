"""Ledger-anchored per-question drafts for one stage (analysis or sections).

Each submission is frozen byte-for-byte and counted in run-state.json before it
is parsed. Accepted parts are hashed in the ledger, so run_control verifies
them on every status check and they cannot be edited or re-counted by deleting
a side file. A stage is either drafted per question or in one combined draft,
never both. No model calls.
"""

import hashlib
import json

from skill.scripts.run_control import RunControl, atomic_json, file_hash

MAX_PART_ATTEMPTS = 3


def _bucket(state, stage):
    return state.setdefault("question_parts", {}).setdefault(stage, {"parts": {}})


def open_stage(root, stage):
    """Reject mixing combined and per-question drafting for one stage."""
    control = RunControl(root)
    state = control.status()
    if state["status"] not in ("active", "exhausted"):
        raise ValueError(f"Cannot change a {state['status']} run.")
    combined = [k for k in state.get("batch_submissions", {}) if k.startswith(f"{stage}:")]
    if combined and stage not in state.get("question_parts", {}):
        raise ValueError(f"{stage} already has a combined draft; continue without --question.")
    if stage not in state.get("question_parts", {}):
        _bucket(state, stage)
        atomic_json(control.path, state)
    return control, state


def per_question(state, stage):
    return stage in state.get("question_parts", {})


def accepted(state, stage, key):
    part = state.get("question_parts", {}).get(stage, {}).get("parts", {}).get(key)
    return part.get("accepted") if part else None


def submit(root, stage, key, input_path):
    """Freeze and count one submission, then return (control, parsed, frozen, part).

    An identical resubmission of an accepted part returns parsed=None.
    Malformed JSON still consumes the attempt: its bytes are frozen first.
    """
    control, state = open_stage(root, stage)
    step = state["steps"].get(stage)
    if not step or step["status"] != "running":
        raise ValueError(f"Run the matching prepare command with --question before finishing {key}.")
    path = (control.root / input_path).resolve()
    path.relative_to(control.root)
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    part = _bucket(state, stage)["parts"].setdefault(key, {"attempts": 0, "accepted": None})
    if part["accepted"]:
        if part["accepted"]["submitted_sha256"] != digest:
            raise ValueError(f"{key} is already accepted and immutable.")
        return control, None, part["accepted"]["path"], part
    last = part.get("last")
    if last and last["sha256"] == digest and (control.root / last["path"]).is_file():
        # Same bytes as the previous attempt (e.g. retry after a crash): re-validate, no new attempt.
        try:
            return control, json.loads(content), last["path"], part
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key}: submitted file is not valid JSON ({exc}); fix it before resubmitting.") from exc
    if part["attempts"] >= MAX_PART_ATTEMPTS:
        raise ValueError(f"{key}: {MAX_PART_ATTEMPTS} attempts already used. Write reports/incomplete.md and stop.")
    part["attempts"] += 1
    frozen = f"synthesis/batch/{stage}-{key.replace(':', '-')}-attempt-{part['attempts']}.json"
    (control.root / frozen).parent.mkdir(parents=True, exist_ok=True)
    (control.root / frozen).write_bytes(content)
    part["last"] = {"path": frozen, "sha256": digest}
    atomic_json(control.path, state)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key}: submitted file is not valid JSON ({exc}); attempt {part['attempts']} used.") from exc
    return control, parsed, frozen, part


def accept(root, stage, key, value, frozen):
    """Record an accepted, validated part and its content hash in the ledger."""
    control = RunControl(root)
    state = control.status()
    part = _bucket(state, stage)["parts"][key]
    output = f"synthesis/batch/{stage}-parts/{key.replace(':', '-')}.json"
    atomic_json(control.root / output, value)
    part["accepted"] = {"path": output, "sha256": file_hash(control.root / output),
                        "submitted_sha256": part["last"]["sha256"], "frozen": frozen}
    atomic_json(control.path, state)
    return output


def load(root, stage, key):
    state = RunControl(root).status()
    record = accepted(state, stage, key)
    if not record:
        return None
    return json.loads((RunControl(root).root / record["path"]).read_text(encoding="utf-8"))


def check(root, state):
    """Called by run_control: accepted parts must still match their hashes."""
    for stage, bucket in state.get("question_parts", {}).items():
        for key, part in bucket.get("parts", {}).items():
            record = part.get("accepted")
            if record:
                path = root / record["path"]
                if not path.is_file() or file_hash(path) != record["sha256"]:
                    raise ValueError(f"Changed/missing accepted part {stage}/{key}; start a new run folder.")

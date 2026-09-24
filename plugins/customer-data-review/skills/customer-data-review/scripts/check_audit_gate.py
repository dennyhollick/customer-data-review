"""Audit enforcement gate — blocks Phase 3 unless audit protocol completed.

Usage: python -m v2.scripts.check_audit_gate deviations.jsonl
Exit code 0 = gate passes, 1 = gate fails.
"""

import argparse
import json
import sys
from pathlib import Path

from skill.scripts.audit_sampling import failure_severity

# Minimum per-mention audit entries required before a user_decided override
# is accepted.  Prevents the orchestrator from skipping audit entirely and
# forging a user override with zero audit work.
MIN_AUDITED_BEFORE_OVERRIDE = 3

# Minimum fraction of audit entries that must have a subagent agent_id.
# Below this threshold the gate fails — nudges the orchestrator to dispatch
# real subagents instead of self-auditing.
SUBAGENT_RATIO_THRESHOLD = 0.80


def _apply_subagent_check(result: dict, per_mention: dict) -> dict:
    """Overlay subagent-ratio check onto an existing audit verdict.

    If fewer than SUBAGENT_RATIO_THRESHOLD of per-mention audit entries
    carry a details.agent_id, override complete→False.  When per_mention
    is empty the check is skipped (existing behaviour handles that case).
    """
    if not per_mention:
        result["subagent_check_passed"] = True
        result.setdefault("subagent_ratio", 0.0)
        return result

    with_agent_id = sum(
        1 for d in per_mention.values() if d.get("agent_id")
    )
    ratio = with_agent_id / len(per_mention)
    result["subagent_ratio"] = round(ratio, 4)

    if ratio < SUBAGENT_RATIO_THRESHOLD:
        result["subagent_check_passed"] = False
        original_reason = result["reason"]
        result["complete"] = False
        result["reason"] = (
            f"Only {ratio:.0%} of audits were subagent-produced "
            f"(need {SUBAGENT_RATIO_THRESHOLD:.0%}). "
            f"Original verdict: {original_reason}"
        )
    else:
        result["subagent_check_passed"] = True

    return result


def load_audit_entries(deviations_path: str) -> list[dict]:
    """Read JSONL and return only audit_result entries.

    Returns [] if file missing/empty. Skips malformed lines.
    """
    path = Path(deviations_path)
    if not path.is_file():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if entry.get("category") == "audit_result":
                entries.append(entry)
    return entries


def check_audit_complete(entries: list[dict]) -> dict:
    """Determine whether the audit protocol was properly completed.

    Returns dict with keys:
        complete (bool), reason (str), rounds_completed (int),
        total_audited (int), total_passed (int), total_failed (int),
        failure_rate (float), severity (str), has_completion_marker (bool)
    """
    result = {
        "complete": False,
        "reason": "",
        "rounds_completed": 0,
        "total_audited": 0,
        "total_passed": 0,
        "total_failed": 0,
        "failure_rate": 0.0,
        "severity": "auto_fix",
        "has_completion_marker": False,
    }

    if not entries:
        result["reason"] = "No audit entries found"
        return _apply_subagent_check(result, {})

    # Separate entry types by step
    per_mention = {}  # mention_id -> latest result
    round_summaries = {}  # round -> summary entry
    completion_marker = None

    for entry in entries:
        details = entry.get("details") or {}
        step = entry.get("step", "")

        if step == "audit_check":
            rid = details.get("mention_id")
            if rid is not None:
                existing = per_mention.get(rid)
                if existing is None or details.get("round", 0) >= existing.get("round", 0):
                    per_mention[rid] = details
        elif step.startswith("audit_round_"):
            rnd = details.get("round")
            if rnd is not None:
                round_summaries[rnd] = details
        elif step == "audit_complete":
            completion_marker = details

    # Completion marker without per-mention entries is suspicious
    if completion_marker and not per_mention:
        result["has_completion_marker"] = True
        result["reason"] = "Completion marker present but no per-mention audit entries"
        return _apply_subagent_check(result, per_mention)

    # Compute stats from per-mention data
    total = len(per_mention)
    passed = sum(1 for d in per_mention.values() if d.get("passed", False))
    failed = total - passed

    result["total_audited"] = total
    result["total_passed"] = passed
    result["total_failed"] = failed
    result["failure_rate"] = round(failed / total, 4) if total > 0 else 0.0
    result["rounds_completed"] = len(round_summaries)
    result["severity"] = failure_severity(result["failure_rate"])

    # Primary path: completion marker exists
    if completion_marker:
        result["has_completion_marker"] = True
        marker_satisfied = completion_marker.get("protocol_satisfied", False)
        marker_severity = completion_marker.get("severity", "")
        marker_rounds = completion_marker.get("total_rounds", 0)
        marker_all_resolved = completion_marker.get("all_failures_resolved", False)

        # Independently verify protocol_satisfied — do not trust the marker blindly.
        has_user_decided = any(
            e.get("resolution") == "user_decided"
            for e in entries
            if e.get("step") not in ("audit_complete",)
            and not (e.get("step") or "").startswith("audit_round_")
        )
        expected_satisfied = (
            (marker_severity == "auto_fix" and marker_rounds >= 1) or
            (marker_severity == "escalate" and marker_rounds >= 4 and marker_all_resolved) or
            (marker_severity == "flag_orchestrator" and has_user_decided)
        )

        if marker_satisfied and not expected_satisfied:
            result["reason"] = (
                f"Completion marker claims protocol_satisfied=True but "
                f"computed value is False (severity={marker_severity}, "
                f"rounds={marker_rounds}, all_resolved={marker_all_resolved})"
            )
            return _apply_subagent_check(result, per_mention)

        if marker_satisfied:
            # Enforce min-audited for flag_orchestrator.
            if marker_severity == "flag_orchestrator" and total < MIN_AUDITED_BEFORE_OVERRIDE:
                result["reason"] = (
                    f"Completion marker claims protocol satisfied with "
                    f"flag_orchestrator severity, but only {total} mentions "
                    f"audited (need {MIN_AUDITED_BEFORE_OVERRIDE})"
                )
                return _apply_subagent_check(result, per_mention)
            result["complete"] = True
            result["reason"] = "Audit protocol satisfied (completion marker, independently verified)"
            return _apply_subagent_check(result, per_mention)
        else:
            result["reason"] = (
                f"Completion marker present but protocol_satisfied=False: "
                f"{marker_severity} severity"
            )
            return _apply_subagent_check(result, per_mention)

    # Fallback: reconstruct from per-response + round data
    result["has_completion_marker"] = False
    severity = result["severity"]

    if severity == "auto_fix":
        if result["rounds_completed"] >= 1 and result["total_audited"] >= 5:
            result["complete"] = True
            result["reason"] = "auto_fix severity: at least 1 round completed, >=5 audited (no marker)"
        elif result["rounds_completed"] >= 1:
            result["reason"] = (
                f"auto_fix severity: 1+ round but only "
                f"{result['total_audited']} audited (minimum 5 required)"
            )
        else:
            result["reason"] = "auto_fix severity but no round summaries found"
    elif severity == "escalate":
        # All 4 rounds required, all failures must be resolved
        if result["rounds_completed"] >= 4:
            if failed == 0:
                result["complete"] = True
                result["reason"] = "escalate severity: 4 rounds completed, all failures resolved (no marker)"
            else:
                result["reason"] = (
                    f"escalate severity: 4 rounds completed but {failed} "
                    f"unresolved failure(s)"
                )
        else:
            result["reason"] = (
                f"escalate severity: only {result['rounds_completed']} of 4 "
                f"rounds completed"
            )
    elif severity == "flag_orchestrator":
        # Need user_decided resolution — only check per-response entries,
        # not round summaries or completion markers.
        # Also require minimum audit dispatches before override is valid.
        has_user_decided = any(
            e.get("resolution") == "user_decided"
            for e in entries
            if e.get("step") not in ("audit_complete",)
            and not (e.get("step") or "").startswith("audit_round_")
        )
        if has_user_decided and total >= MIN_AUDITED_BEFORE_OVERRIDE:
            result["complete"] = True
            result["reason"] = (
                f"flag_orchestrator severity: user_decided resolution found, "
                f"{total} mentions audited (no marker)"
            )
        elif has_user_decided:
            result["reason"] = (
                f"flag_orchestrator severity: user_decided found but only "
                f"{total} mentions audited (need {MIN_AUDITED_BEFORE_OVERRIDE} "
                f"before override is valid)"
            )
        else:
            result["reason"] = "flag_orchestrator severity: no user_decided resolution found"

    return _apply_subagent_check(result, per_mention)


def load_dispatch_entries(deviations_path: str, phase: str = "2",
                          step: str = "summarize") -> list[dict]:
    """Read JSONL and return audit_result entries for a specific phase/step.

    Used to verify that subagents (not the orchestrator) performed
    dispatch-required work like Phase 2 summarization.

    Returns [] if file missing/empty. Skips malformed lines.
    """
    path = Path(deviations_path)
    if not path.is_file():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if (entry.get("category") == "audit_result"
                    and entry.get("phase") == phase
                    and entry.get("step") == step):
                entries.append(entry)
    return entries


def check_dispatch_compliance(entries: list[dict],
                               threshold: float = SUBAGENT_RATIO_THRESHOLD
                               ) -> dict:
    """Check that dispatched work was performed by subagents.

    Examines entries for details.agent_id presence. If fewer than
    `threshold` fraction carry an agent_id, the check fails.

    Returns dict with keys:
        passed (bool), reason (str), total (int),
        with_agent_id (int), ratio (float)
    """
    result = {
        "passed": False,
        "reason": "",
        "total": 0,
        "with_agent_id": 0,
        "ratio": 0.0,
    }

    if not entries:
        result["reason"] = "No dispatch entries found"
        return result

    total = len(entries)
    with_id = sum(
        1 for e in entries
        if (e.get("details") or {}).get("agent_id")
    )
    ratio = with_id / total
    result["total"] = total
    result["with_agent_id"] = with_id
    result["ratio"] = round(ratio, 4)

    if ratio >= threshold:
        result["passed"] = True
        result["reason"] = (
            f"{ratio:.0%} of dispatches were subagent-produced "
            f"(threshold {threshold:.0%})"
        )
    else:
        result["reason"] = (
            f"Only {ratio:.0%} of dispatches were subagent-produced "
            f"(need {threshold:.0%}). {total - with_id} of {total} entries "
            f"missing agent_id."
        )

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Check audit and dispatch gates"
    )
    parser.add_argument("deviations", help="Path to deviations.jsonl")
    parser.add_argument(
        "--check", choices=["audit", "dispatch"], default="audit",
        help="Which gate to check: 'audit' (Phase 2.5 audit completeness) "
             "or 'dispatch' (Phase 2 subagent compliance)"
    )
    parser.add_argument(
        "--phase", default="2",
        help="Phase to filter dispatch entries (default: 2)"
    )
    parser.add_argument(
        "--step", default="summarize",
        help="Step to filter dispatch entries (default: summarize)"
    )
    parser.add_argument("--project-root", help="Use source-bound bounded audit gate")
    args = parser.parse_args()

    root = Path(args.project_root) if args.project_root else Path(args.deviations).resolve().parent.parent.parent
    if args.project_root or (root / "synthesis/run-state.json").exists():
        if args.check == "audit":
            from skill.scripts.bounded_audit import verify_gate
            verdict = verify_gate(root)
        else:
            from skill.scripts.run_control import RunControl
            state = RunControl(root).status()
            verdict = {"complete": state["steps"].get("phase-2-merge", {}).get("status") == "complete",
                       "reason": "Evidence/checkpoint completeness replaces provider-specific agent IDs."}
        print(json.dumps(verdict, indent=2))
        sys.exit(0 if verdict["complete"] else 1)

    if args.check == "audit":
        entries = load_audit_entries(args.deviations)
        verdict = check_audit_complete(entries)
        print(json.dumps(verdict, indent=2))
        sys.exit(0 if verdict["complete"] else 1)
    else:
        entries = load_dispatch_entries(
            args.deviations, phase=args.phase, step=args.step
        )
        verdict = check_dispatch_compliance(entries)
        print(json.dumps(verdict, indent=2))
        sys.exit(0 if verdict["passed"] else 1)


if __name__ == "__main__":
    main()

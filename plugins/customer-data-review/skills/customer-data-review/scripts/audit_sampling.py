"""Phase 2.5 — Spot audit sample selector with escalating tiers."""

import argparse
import json
import math
import random
import re
import sys


# Escalation tiers: fraction of remaining unchecked to sample
_TIER_FRACTIONS = {1: 0.25, 2: 0.50, 3: 0.75, 4: 1.0}

# Failure-rate thresholds
_AUTO_FIX_CEILING = 0.05
_FLAG_FLOOR = 0.20

_MENTION_SUFFIX_RE = re.compile(r"^(.+)_Q\d+_\d+$")


def _extract_file_id(mention_id: str) -> str:
    """Extract file_id from a mention_id like 'sales_01_Q1_0' → 'sales_01'."""
    match = _MENTION_SUFFIX_RE.match(mention_id)
    if not match:
        raise ValueError(f"Cannot extract file_id from '{mention_id}'")
    return match.group(1)


def build_strata(mention_ids: list[str], plan: dict) -> dict[str, str]:
    """Map each mention_id to its source_type using plan metadata.

    Args:
        mention_ids: All mention IDs.
        plan: Parsed plan.json with a "files" list.

    Returns:
        Dict mapping mention_id → source_type.
    """
    file_lookup = {}
    for source in plan.get("files", []):
        file_lookup[source["file_id"]] = source.get("source_type", "other")

    strata = {}
    for mid in mention_ids:
        fid = _extract_file_id(mid)
        strata[mid] = file_lookup.get(fid, "other")
    return strata


def select_sample(
    mention_ids: list[str],
    round_num: int,
    already_audited: set[str],
    strata: dict[str, str] | None = None,
) -> list[str]:
    """Select a sample for audit at the given round tier.

    When strata is provided, allocates proportionally by source type with a
    minimum of 1 per type when the budget allows. If there are more source
    types than the sample budget, some types may receive 0 samples.

    Args:
        mention_ids: All mention IDs eligible for audit.
        round_num: Escalation round (1-4).
        already_audited: IDs already checked in prior rounds.
        strata: Optional mapping of mention_id → source_type. When provided,
            sampling is stratified to ensure coverage across source types.
            Mention IDs not present in the mapping are grouped as "other".

    Returns:
        Sorted list of mention_ids to audit this round.
    """
    if round_num < 1 or round_num > 4:
        raise ValueError(f"round_num must be 1-4, got {round_num}")

    remaining = [rid for rid in mention_ids if rid not in already_audited]
    if not remaining:
        return []

    fraction = _TIER_FRACTIONS[round_num]
    sample_size = math.ceil(len(remaining) * fraction)
    sample_size = min(sample_size, len(remaining))

    if not strata:
        return sorted(random.sample(remaining, sample_size))

    # Group remaining mentions by source type
    by_type: dict[str, list[str]] = {}
    for rid in remaining:
        st = strata.get(rid, "other")
        by_type.setdefault(st, []).append(rid)

    # Allocate proportionally: each type gets floor(proportion * sample_size),
    # with a minimum of 1 to guarantee coverage of small source types.
    total = len(remaining)
    quotas: dict[str, int] = {}
    for st, ids in by_type.items():
        proportional = max(1, math.floor(len(ids) / total * sample_size))
        quotas[st] = min(proportional, len(ids))

    # If total quotas exceed budget, trim largest groups first
    while sum(quotas.values()) > sample_size:
        largest = max(quotas, key=lambda st: quotas[st])
        quotas[largest] -= 1

    # If total quotas are under budget, distribute remainder to largest groups.
    # Cap iterations to prevent infinite loop if invariants are violated.
    max_fill_iters = sample_size
    for _ in range(max_fill_iters):
        if sum(quotas.values()) >= sample_size:
            break
        filled_any = False
        for st in sorted(by_type, key=lambda s: len(by_type[s]), reverse=True):
            if quotas[st] < len(by_type[st]):
                quotas[st] += 1
                filled_any = True
                if sum(quotas.values()) >= sample_size:
                    break
        if not filled_any:
            break

    selected: list[str] = []
    for st, quota in quotas.items():
        selected.extend(random.sample(by_type[st], quota))

    return sorted(selected)


def compute_failure_rate(results: list[dict]) -> float:
    """Compute failure rate from audit results.

    Each result dict must have a "passed" boolean key.
    Returns 0.0 if results is empty.
    """
    if not results:
        return 0.0
    failures = sum(1 for r in results if not r["passed"])
    return failures / len(results)


def should_escalate(results: list[dict]) -> bool:
    """Return True if any failure exists in this round's results."""
    return any(not r["passed"] for r in results)


def failure_severity(failure_rate: float) -> str:
    """Classify failure rate into action severity.

    Returns:
        "auto_fix" if ≤5%, "escalate" if ≤20%, "flag_orchestrator" if >20%.
    """
    if failure_rate <= _AUTO_FIX_CEILING:
        return "auto_fix"
    if failure_rate <= _FLAG_FLOOR:
        return "escalate"
    return "flag_orchestrator"


def main() -> None:
    parser = argparse.ArgumentParser(description="Spot audit sample selector")
    parser.add_argument("mention_ids_json", help="JSON file with list of mention IDs")
    parser.add_argument("--round", type=int, default=1, help="Audit round (1-4)")
    parser.add_argument(
        "--already-audited-json",
        help="JSON file with list of already-audited IDs",
    )
    parser.add_argument(
        "--plan-json",
        help="Plan JSON file — enables stratified sampling by source type",
    )
    parser.add_argument("--seed", type=int, help="Random seed for reproducibility")
    args = parser.parse_args()

    with open(args.mention_ids_json, encoding="utf-8") as f:
        mention_ids = json.load(f)

    already_audited: set[str] = set()
    if args.already_audited_json:
        with open(args.already_audited_json, encoding="utf-8") as f:
            already_audited = set(json.load(f))

    strata = None
    if args.plan_json:
        with open(args.plan_json, encoding="utf-8") as f:
            plan = json.load(f)
        strata = build_strata(mention_ids, plan)

    if args.seed is not None:
        random.seed(args.seed)

    sample = select_sample(mention_ids, args.round, already_audited, strata=strata)
    json.dump(sample, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()

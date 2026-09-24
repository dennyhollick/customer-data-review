"""Deviation logger — structured JSONL entries for pipeline events.

Pure function. No I/O — the caller handles file writes.
All pipeline events (deviations, validation results, audit results)
flow through build_entry() + format_entry().
"""

import json
from datetime import datetime, timezone


DEVIATION_CATEGORIES = frozenset({
    "agent_failure",         # LLM agent failed after 3 attempts
    "validation_override",   # Validation error overridden by orchestrator
    "data_quality",          # Source data quality issue
    "user_override",         # User chose to skip/override
    "script_error",          # Deterministic script produced unexpected result
    "audit_result",          # Audit pass/fail event
    "conversion_failure",    # File conversion failed
    "retry_simplified",      # Agent task simplified after context exceeded
})

RESOLUTION_TYPES = frozenset({
    "fixed_directly",        # Orchestrator fixed the issue
    "redispatched_fresh",    # Fresh agent dispatched
    "skipped_with_deviation",  # Skipped, logged for review
    "user_decided",          # User made the call
    "logged_only",           # Informational, no action needed
})


def build_entry(phase, step, category, description, resolution,
                source_id=None, question_id=None, details=None):
    """Build a structured deviation entry.

    Args:
        phase: Pipeline phase (e.g. "1a", "2", "2.5").
        step: Step within the phase (e.g. "classification", "dispatch").
        category: One of DEVIATION_CATEGORIES.
        description: Human-readable description of the event.
        resolution: One of RESOLUTION_TYPES.
        source_id: Optional source identifier.
        question_id: Optional question identifier.
        details: Optional dict with additional structured data.

    Returns:
        dict with all fields populated.

    Raises:
        ValueError: If category or resolution is not in the allowed set.
    """
    if category not in DEVIATION_CATEGORIES:
        raise ValueError(
            f"Unknown category: {category!r}. "
            f"Must be one of: {sorted(DEVIATION_CATEGORIES)}"
        )
    if resolution not in RESOLUTION_TYPES:
        raise ValueError(
            f"Unknown resolution: {resolution!r}. "
            f"Must be one of: {sorted(RESOLUTION_TYPES)}"
        )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "step": step,
        "category": category,
        "description": description,
        "resolution": resolution,
        "source_id": source_id,
        "question_id": question_id,
        "details": details,
    }


def format_entry(entry):
    """Serialize entry to a single JSON line (no trailing newline).

    Args:
        entry: dict from build_entry().

    Returns:
        Compact JSON string on one line. Unicode preserved (no ascii escaping).
    """
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

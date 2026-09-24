"""Deterministic assembly of the analysis plan from Phase 1 outputs."""

MAX_CONTEXT_LENGTH = 1000


def assemble_plan(
    classification_rows: list[dict],
    segments: list[dict],
    business_context: str,
    scope: str,
    questions: list[dict],
    exclusions: dict[str, str],
) -> dict:
    """Assemble a plan dict from Phase 1 outputs.

    Pure data assembly — no LLM, no validation, no I/O.
    Classification rows are kept immutable; exclusion metadata is merged
    from the separate exclusions dict.

    Args:
        classification_rows: Rows from classification (with reconciled segments).
        segments: Reconciled segment definitions.
        business_context: Business context string.
        scope: Recommended scope (segment id or "all_data").
        questions: Analysis questions.
        exclusions: Mapping of file_id to exclusion_reason.
            Unknown file_ids are silently skipped.

    Returns:
        Plan dict matching plan.schema.json.
    """
    _REQUIRED_FIELDS = ("file_id", "source_type", "company", "lifecycle",
                         "segment", "quality", "summary")
    _OPTIONAL_FIELDS = ("interaction_count", "interaction_type", "interaction_note")

    files = []
    for i, row in enumerate(classification_rows):
        if not isinstance(row, dict):
            raise ValueError(
                f"classification_rows[{i}]: expected dict, got {type(row).__name__}"
            )
        missing = [f for f in _REQUIRED_FIELDS if f not in row]
        if missing:
            raise ValueError(
                f"classification_rows[{i}]: missing required fields: {', '.join(missing)}"
            )
        source = {
            "file_id": row["file_id"],
            "source_type": row["source_type"],
            "company": row["company"],
            "lifecycle": row["lifecycle"],
            "segment": row["segment"],
            "quality": row["quality"],
            "summary": row["summary"],
        }
        for field in _OPTIONAL_FIELDS:
            if field in row:
                source[field] = row[field]
        fid = row["file_id"]
        if fid in exclusions:
            source["excluded"] = True
            source["exclusion_reason"] = exclusions[fid]
        files.append(source)

    # Truncate business_context at word boundary if over limit
    if len(business_context) > MAX_CONTEXT_LENGTH:
        truncated = business_context[:MAX_CONTEXT_LENGTH - 3].rsplit(" ", 1)[0]
        business_context = truncated + "..."

    return {
        "business_context": business_context,
        "scope": scope,
        "questions": questions,
        "segments": segments,
        "files": files,
    }

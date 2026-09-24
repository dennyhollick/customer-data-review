# Phase 1: Scope and classify

Read the request and `references/research_questions.md`. Use the five bootcamp questions unless the user supplies their own. More questions require explicit scope; do not silently add Core 10 or extended questions. For questions about current marketing or internal ownership, distinguish supplied company context from customer evidence and mark missing context as unknown.

For new users, complete `references/guided-intake.md` before freezing the request
or starting analytical work. A request such as "help me analyze these files" needs
a brief guided intake; a request that already provides the goal, sources and
questions can proceed with an informational scope summary. On resume, use the
locked plan rather than repeating onboarding.

1. Inventory the supplied customer files. Convert non-text files once using available tools. Preserve originals. Record unsupported/corrupt files and conversion exclusions visibly; do not install tools or fetch new data indefinitely.
2. Create unique sanitized IDs (letters, digits, underscore). Resolve collisions explicitly, never overwrite a same-ID source. Condense business context to at most 1,000 characters before locking it; preserve that exact text in the plan. Save the source-path map and request described in `references/run-protocol.md`.
3. Set `writing_mode` to `section_packets_v1` and `report_style` to `concise_v1` in the request before initializing the controller. Existing runs keep their frozen settings. Initialize/resume the run controller (`RUN run_control init --root .`). Claim `phase-1` with the request and source map as inputs.
4. Use `prompts/classify_source.md` to classify sources. Start with metadata and the opening exchange; read further when needed to resolve source type, lifecycle, segment or quality. Do not reread complete long transcripts solely to fill the short summary. Unknown metadata stays unknown; do not exclude a source based on an uninformative excerpt. Full-source extraction follows in Phase 2. ICP fit is not evidence quality. Process small batches sequentially or at most four in parallel if available; no model/provider requirement.
5. Normalize with `auto_fix_classification` using the runner's `--write-arg rows` so corrected data is saved, then use `validate_classification`. Use `prompts/reconcile_segments.md` only if labels are inconsistent; a single reconciliation is sufficient. Validate with `validate_reconciliation`. All malformed results share the phase's two repairs.
6. Use `assemble_plan` and `validate_plan` to create `synthesis/plan.json` with the request's business context, questions, included/excluded sources and segments. Do not add fields outside the existing schema. Preserve explicit exclusion reasons and empty/unknown segments.
7. Show a compact scope summary: sources included/excluded, questions, spot-check mode, and the soft time budget. This is informational; no extra approval pause when the request already supplies scope. Finish `phase-1` with plan.json only after validation.

# Phase 4: Question sections, then executive summary and opportunities

Read `prompts/concise_report.md` once.

## Sections, one question at a time

For each locked question in order:

```text
RUN concise_workflow prepare-sections --root . --question Q1 --output synthesis/batches/sections-Q1-input.json
```

The saved file is complete if the displayed output is cut off. It holds the question's themes, counts, retained evidence and the output format. In a very large dataset it shows the strongest examples for each theme and says how many are shown; counts still cover everything.

Write `section-Q1.json`: the research question as `title`, a short `nav_label`, every locked theme with a clear label, 0–3 takeaways with supporting mention IDs, and one or two exact quote excerpts (required when the question has three or more mentions). Before choosing a quote, read it in its source file (`source_paths`) to confirm who is speaking and what they mean. Omit weak or ambiguous quotes.

```text
RUN concise_workflow finish-sections --root . --question Q1 --input section-Q1.json
```

An accepted section is immutable. A rejected draft can be fixed and resubmitted (three attempts per question). In takeaways, sentences that type counts are removed and double quotation marks are stripped, and each change is logged. A theme label that states a count is rejected, so write labels without numbers. After the last question is accepted, the helper checks all sections together and finishes `sections`.

## Executive summary and opportunities

```text
RUN concise_workflow prepare-executive --root . --output synthesis/batches/executive-input.json
```

Write `executive.json` with a title, a plain headline, usually 3–5 findings and 3–5 opportunities.
- For a counted finding, give its `theme_id` and the literal `{calls}` placeholder; code inserts the exact count.
- Each opportunity has a title, the problem in the customers' own terms, a hypothesis, one concrete next step, and the `finding_ids` it builds on (its evidence). They are hypotheses for the perception-gap exercise, not facts. Use fewer only when there are fewer findings.

```text
RUN concise_workflow finish-executive --root . --input executive.json
RUN concise_workflow finish-opportunities --root .
RUN run_control complete --root .
RUN run_control metrics --root .
```

The helpers verify references, counts and exact quote excerpts. They then write `reports/report.html`, `reports/report.md`, `reports/evidence.html` and `reports/opportunities.md`.

A rejected executive draft uses `prepare-executive --repair`: fix the stated error and resubmit (three attempts in total). Style warnings are not a reason for another draft.

Open the rendered report once to check that it reads well. Save the metrics output to `reports/run-summary.json` and report any unavailable measurements honestly. Then stop.

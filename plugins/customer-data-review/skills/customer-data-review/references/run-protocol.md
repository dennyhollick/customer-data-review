# Run state and budgets

Run commands from the user's **project folder**, using the absolute path to the installed package's `scripts/run.py`. The examples abbreviate that entry point as `RUN`; substitute the actual command rather than assuming a shell alias exists.

Before classification, prepare:
- `synthesis/source-paths.json`: JSON object mapping unique sanitized `file_id` strings to UTF-8 text file paths, relative to the project folder (absolute readable paths also work). Keep all source text, including survey rows. Convert files once; do not move originals.
- `synthesis/request.json`: JSON object containing `business_context`, `questions` (list of `{id,text}`), and `audit_mode` (`sampled` by default; `skip` only at the user's request). Set `writing_mode` to `section_packets_v1` and `report_style` to `concise_v1`. These are the locked inputs. Optional `audit_sample_limit` defaults to 20 (maximum 20); `audit_source_limit` defaults to six (maximum six); choose it before initializing, never change it after seeing results. Put unsupported conversion details into the request as limitations. Do not invent business context; explicitly state when unavailable.

```text
RUN run_control init --root .
RUN run_control status --root .
RUN run_control claim --root . --key extract:SOURCE_ID --inputs synthesis/plan.json synthesis/source-paths.json
... perform initial work or one repair; run the phase's validators ...
RUN run_control finish --root . --key extract:SOURCE_ID --outputs synthesis/pipeline/responses/SOURCE_ID.json
```

`claim` reserves an attempt before work starts. `reuse` means its input/output hashes are still valid: use the saved result, do not run the work again. `run` permits the numbered attempt. An interrupted attempt counts. Each key allows an initial attempt plus two repairs, except `extract:` and `audit-initial:` keys, which allow one repair. The semantic `audit-repair:FILE_ID` correction is the exception: one draft only, no second claim. Do not rename it, reset state, or quietly broaden the task. `finish --tokens INTEGER` accepts actual measured tokens for that attempt; omit when unavailable.

Use these durable keys:

| Work | Key | Validated output |
| --- | --- | --- |
| Classification + segment reconciliation | `phase-1` | `synthesis/plan.json` |
| One source extraction | `extract:FILE_ID` | `synthesis/pipeline/responses/FILE_ID.json` |
| Merge and source completeness | `phase-2-merge` | `synthesis/pipeline/mentions.jsonl` |
| Audit task per source | `audit-initial:FILE_ID` | `synthesis/audit/results/FILE_ID.json` |
| Correct/recheck failures per source | `audit-repair:FILE_ID` | `synthesis/audit/corrections/FILE_ID.json` |
| Finalize audit | `audit` | audit manifest, summary, results, and validated mentions |
| Themes, assignments and counts (drafted per question, then assembled) | `phase-3` | `themes_Q*.json`, `merged_Q*.json`, `findings.json` |
| Question sections (drafted per question, then assembled) | `sections` | accepted sections |
| Executive findings and opportunities, verify/render | `report` | report JSON, HTML/Markdown, evidence |
| Opportunities file | `opportunities` | `reports/opportunities.md` |

Pass the actual upstream artifacts through `--inputs` (space separated). Keep upstream artifacts immutable after completion. Declare all durable outputs through `--outputs`; do not include mutable logs or run-state.json itself. The source files, request, and package are fingerprinted globally; changes require a new run folder. This avoids reusing apparently valid outputs after changing questions, source data, or skill code.

Only the parent writes the ledger, even when batches run concurrently. Reserve up to four independent keys serially, start work, then record results serially. For work that allows a retry, use the same key for malformed output, failed grounding, or an incomplete return; they share that allowance. Semantic audit corrections have one draft only. Use the Phase2.5 helpers: audit-assess, audit-repair-read/save if below90%, then audit-finalize. Never claim correction keys manually or assemble repairs.json yourself.

Time is a **soft** budget: 15 minutes plus one minute per source by default. When it passes, `status` shows `budget_warning: true`; tell the user and keep working. It never blocks work. If you must wait for the user, finish current batches, then `RUN run_control pause`; `resume` continues. Only `stop` (incomplete) or `complete` closes a run, and a closed run never reopens. Preserve it and use a new folder for a follow-up.

Warnings are recorded in `synthesis/audit/deviations.jsonl` only when useful for interpreting output. Use `build_entry` categories from `log_deviation.py`: `data_quality`, `script_error`, or `user_override`; there is no `user_decision` category. Do not log every successful tool call.

A command-line usage error (wrong interpreter, unrecognized flag or missing argument) is an invocation mistake: read that command's --help and correct the invocation once, preserving the current ledger and analytical attempt. Do not stop merely because an invented flag is unsupported. This does not permit changing submitted analytical output without its reserved repair.

An exception in a bundled deterministic script is a dependency/data/code problem, not permission to rewrite installed code. Describe it and stop with saved artifacts. A bad quote draft or failed lookup in a helper you wrote is an extraction-output defect: correct the source's draft once, or omit the unsupported mention as Phase 2 directs. Do not treat that as a new audit or restart the corpus. Validation errors in model output get the remaining permitted attempts; semantic audit correction has none. An unusable correction closes incomplete. Warnings do not block.

When all required stages and the three deliverables are ready:
```text
RUN run_control complete --root .
```
If budget or evidence is insufficient, write `reports/incomplete.md`, then `RUN run_control stop`. Report active seconds and total elapsed time separately when available. Token counts are measurements or unavailable, never guesses.

The phase batch commands own claims/finishes and mechanical checks; do not separately claim their keys. They avoid custom orchestration code and repeated function discovery. Legacy per-question keys remain readable for old ledgers; do not mix them with the combined workflow to gain attempts.

## Pure function helpers

Some existing helpers are function libraries, not CLIs. `RUN SCRIPT --help` shows their public function names, signatures and docstrings. Call them through the same portable runner; no ad hoc import setup is needed:

```text
RUN assemble_plan --call assemble_plan --args-json synthesis/plan-inputs.json --output synthesis/plan.json
RUN validate_classification --call validate_classification --args-json synthesis/classification-check-inputs.json
```

The argument file is a JSON object with keyword parameters matching the function signature. For assembly, supply classification_rows, segments, business_context, scope, questions and exclusions. For a validator, read its displayed signature rather than inventing parameter names. Returned error lists (or the first list in an errors/warnings pair) must be empty before finishing a checkpoint; process success alone does not mean validation passed. Warnings remain advisory.

Two normalizers mutate an input and return change messages. Save the mutated argument explicitly, or its corrections will be lost when the helper exits:
```text
RUN validate_classification --call auto_fix_classification --args-json synthesis/classification-fix-inputs.json --write-arg rows --output synthesis/classifications.json
RUN validate_themes --call normalize_themes --args-json synthesis/theme-fix-inputs.json --write-arg themes_obj --output synthesis/pipeline/themes_Q1.json
```
The first argument file contains `{"rows": [...]}`; the second contains `{"themes_obj": {...}}`. Change messages go to stdout. Use the relevant validator afterwards. JSON arrays are converted to sets only for parameters explicitly annotated as sets (for example expected_mention_ids); standard validator CLIs also remain available.

New runs use `batch_report prepare-analysis/finish-analysis --question`, then `concise_workflow prepare-sections/finish-sections --question`, then prepare-executive/finish-executive. Each accepted question is immutable; each question allows three attempts. Known locked count/assignment defects stop delivery; never waive them with prose.


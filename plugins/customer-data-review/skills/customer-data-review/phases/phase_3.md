# Phase 3: Themes and counts, one question at a time

Read `prompts/identify_themes.md` and `prompts/assign_mentions.md` once. Then, for each locked question in order:

```text
RUN batch_report prepare-analysis --root . --question Q1 --output synthesis/batches/analysis-Q1-input.json
```

The first call checks the audit gate and reserves `phase-3`. The saved file is complete if the displayed output is cut off. Its `task` field says what to write next, and its `finish` field gives the exact finish command.

**`task: themes_and_assignments`** (the usual case). Write `analysis-Q1.json` in one pass:

```json
{"question_id":"Q1","themes":[],"assignments":[],"quote_nominations":{}}
```

Create the themes and assign every supplied mention exactly once to one of them. Theme IDs look like `Q1_T1`. Labels must answer this question's own dimension: discovery channels, alternatives and demo objections are not customer pains. A question with no mentions has empty arrays. Optionally nominate one promising quote mention per theme.

```text
RUN batch_report finish-analysis --root . --question Q1 --input analysis-Q1.json
```

**Very large questions.** When a question has too many mentions for one pass, `prepare-analysis` returns `task: themes` with a spread of examples. Write `{"question_id":"Q1","themes":[...]}` and finish it with `--part themes`. Then run `prepare-analysis --question Q1` again for each `task: assign` chunk. Assign every supplied mention to the locked themes and finish it with `--part c0`, `--part c1` and so on. The question is accepted automatically when its last chunk is accepted.

An accepted part is immutable. A rejected draft can be fixed and resubmitted; each part allows three submissions in total, and malformed JSON counts as one. After every question is accepted, the helper assembles them, computes the counts and finishes `phase-3`. These counts are authoritative; never estimate them.

Thin themes, broad "Other" buckets and wording warnings do not justify another pass. An unexpected script error is not permission to edit installed scripts: describe it, save an incomplete handoff and stop.

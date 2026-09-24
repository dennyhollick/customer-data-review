# Guided first-use intake

Help a new user shape a useful analysis before doing it. Use ordinary conversation
or the host's question interface; no specific provider, question tool or subagent
is required. Reuse answers in the current request and supplied company profile.
Read a resume ledger first and do not restart intake for an unchanged run.

## 1. Understand the business and the decision

Ask only for missing essentials, together in one short exchange:

- **Business:** what the company sells and who its customers are. Offer a short
  description (recommended for a quick start), a company website, or skipping
  company context. A supplied profile is enough.
- **Goal:** what the research should help them learn or decide. Offer customer
  understanding, product priorities, positioning/messaging, or retention. Recommend
  the goal best supported by their request; otherwise suggest customer understanding.
- **Sources:** where the customer files are, if they are not already available.

Do not make the user fill a form of paths, model choices or implementation fields.
If they provide a website and browsing is available, read its homepage/about once,
summarize the relevant business context briefly and let them correct it with the
scope review below. If browsing fails, ask for a description or offer to proceed
without company context. Do not start an open-ended company research task.

If they skip context, record that choice and analyze only what the sources support.
Do not infer the company's current marketing, internal ownership or priorities
from customer transcripts.

## 2. Show what is available

Inventory the accessible source files using Phase 1's conversion/source-ID rules.
State the total, source types when clear, and files that cannot be read. List
material exclusions with reasons. Keep unsupported segment/lifecycle labels
unknown; do not create confident classifications from filenames alone.

Recommend analyzing all supplied readable sources unless the request or evident
dataset size calls for a subset. For a subset, identify the exact selection and
why it fits the goal. Offer segment/date/source-type focus when useful. Ask about
unknown metadata only when it would materially change the scope or interpretation.
Never silently sample a dataset the user asked to analyze in full.

## 3. Agree on questions and scope

Show one compact proposed plan: purpose, included source count, any exclusions,
and the full research-question text. Recommend five bootcamp questions by default
from `research_questions.md`; use the user's own questions when supplied. Explain
when marketing/internal context or a different source type is needed to answer a
question. For a generic research request with poor-fitting defaults, offer fewer
or revised questions rather than pretending the data can answer them. In an
explicit bootcamp exercise, retain required questions and state the evidence gaps.

Briefly explain the defaults: a fixed evidence spot-check, limited corrections,
and a soft time budget (15 minutes plus one per source) that warns but never stops
the run. Mention that large datasets use more tokens.

For an otherwise unspecified first run, ask once:
**Proceed with this plan (recommended), adjust the questions, or change the scope?**
Put the recommendation first and explain why it fits their goal and data. Wait for
their answer before locking a plan that depended on this choice. Incorporate their
changes without making them re-answer settled intake questions.

When the user already supplied the scope/questions or explicitly asked to proceed
with the defaults, the summary is informational. Do not add another approval stop.
Optional company context can be skipped when the user chooses that; elapsed time
alone is not consent to an unanswered scope question.

## 4. Lock once and continue

Use the agreed answers in Phase 1's request, source map and plan. Do not initialize
the analytical budget while awaiting intake answers. Follow the existing bounded
classification and validation workflow, then proceed to extraction, the fixed
spot-check, counted themes, sections and executive summary. Report material new
input problems; do not silently change the agreed questions or source cohort.

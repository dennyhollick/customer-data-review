# Customer Data Review

A skill for Claude and ChatGPT/Codex that turns customer call transcripts, surveys and reviews into a report: counted themes, checked quotes, executive findings and 3–5 opportunities to explore. Built for the [PMM Leadership Bootcamp](https://pmmcamp.dennyhollick.com/bootcamp/week-2/).

It needs an AI tool that can read your files and run Python 3.10+. No extra packages are required.

## Install

**Claude Code**

```text
/plugin marketplace add dennyhollick/customer-data-review
/plugin install customer-data-review@pmm-camp
```

**Codex** (CLI, or Codex in the ChatGPT desktop app)

```text
codex plugin marketplace add dennyhollick/customer-data-review
codex plugin add customer-data-review@pmm-camp
```

Restart Codex after installing.

**Claude desktop, claude.ai or ChatGPT without the command line:** download the ZIP for your tool from the [Week 2 page](https://pmmcamp.dennyhollick.com/bootcamp/week-2/) and follow its install steps.

## Use

Put your customer files in a project folder, for example `pmm-bootcamp-week-2/data/`, and ask:

```text
Use customer-data-review on the customer files in my project folder (data/). Use the five bootcamp questions. My business: [two sentences]. The decision this should inform: [one sentence].
```

The report lands in `reports/`: `report.html`, `report.md`, `evidence.html` and `opportunities.md`.

Counts and quotes are checked by code. The written takeaways are the model's interpretation, so check any takeaway you plan to reuse against its linked evidence.

## Layout

- `plugins/customer-data-review/`: the plugin. `plugin.json` is for Codex/ChatGPT, `.claude-plugin/plugin.json` is for Claude, and `skills/customer-data-review/` is the shared skill.
- `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`: the marketplace entries for Claude and Codex.

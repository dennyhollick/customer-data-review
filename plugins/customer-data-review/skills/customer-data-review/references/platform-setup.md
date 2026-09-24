# Set up Customer Data Review in Claude

The skill reads your customer files, runs small Python checks and saves a report. It works anywhere Claude can read your files and run code.

## What you need

- `customer-data-review-claude.zip` from the Week 2 page. Don't unzip it for claude.ai; upload it as is.
- A project folder, such as `pmm-bootcamp-week-2/`, with your transcripts or other customer files in `data/`. Plain text and Markdown work best; export Word and PDF files to text first.
- Two sentences about your business and the decision this research should inform.

Twenty or so representative calls is a good first run; it handles up to about 200. Each call is read in full, so large runs use a lot of your plan's usage.

## Install

**Claude desktop, claude.ai and Cowork**
1. Don't unzip the download.
2. In Claude desktop or claude.ai, open **Customize → Skills**, click **+**, then **Create skill → Upload a skill** and pick the ZIP. Make sure the skill is switched on.
3. Check **Settings → Capabilities → Code execution and file creation** is on. On Team and Enterprise plans, an admin may need to allow Skills first.

The skill is then available in chat and Cowork, and in Claude Code when you sign in with the same account. In a plain claude.ai chat you can attach up to 20 files; in the prompt below, replace `in my project folder (data/)` with `in the attached files`. For more files, use Cowork with your project folder connected.

**Claude Code without the upload**
- Unzip into `~/.claude/skills/`, so the file sits at `~/.claude/skills/customer-data-review/SKILL.md`, then type `/customer-data-review`.
- Or install the plugin: `/plugin marketplace add dennyhollick/customer-data-review`, then `/plugin install customer-data-review@pmm-camp`.

## Start the run

```text
Use customer-data-review on the customer files in my project folder (data/). Use the five bootcamp questions. My business: [two sentences]. The decision this should inform: [one sentence].
```

Claude asks anything it still needs, shows a short plan and then runs. It tells you once the run passes 15 minutes plus one minute per call (35 minutes for 20 calls) and keeps going unless you tell it to stop. If you need to leave partway, ask it to pause; saying "resume" later picks up from the saved checkpoint.

If Claude says it cannot run Python or read your files, use the [chat workflow](chat-workflow.md) instead. It is quicker to set up, but its counts and quotes are not checked by code.

## What you get

- `reports/report.html`: the report to open in a browser. It has an executive summary, opportunities to explore, and one section per question with counted themes, takeaways and quotes.
- `reports/report.md`: the same report in Markdown.
- `reports/evidence.html`: the source evidence behind every count and quote.
- `reports/opportunities.md`: the opportunities on their own, ready for the Week 2 exercise.

Counts are calculated by code: each shows how many calls mention a theme, out of all calls reviewed. Every quote is checked word for word against its source. A spot-check reviews up to 20 extracted observations for meaning. It is a sample, not a guarantee that every line is right, so read the quotes you plan to reuse in context.

---

# Set up Customer Data Review in ChatGPT or Codex

The skill reads your customer files, runs small Python checks and saves a report. It works anywhere the assistant can read your files and run code.

## What you need

- `customer-data-review-chatgpt.zip` from the Week 2 page.
- A project folder, such as `pmm-bootcamp-week-2/`, with your transcripts or other customer files in `data/`. Plain text and Markdown work best; export Word and PDF files to text first.
- Two sentences about your business and the decision this research should inform.

Twenty or so representative calls is a good first run; it handles up to about 200. Each call is read in full, so large runs use a lot of your plan's usage.

## Install

**Codex** (in the ChatGPT desktop app, the CLI or the IDE extension)
1. Unzip the download into `~/.agents/skills/`, so the file sits at `~/.agents/skills/customer-data-review/SKILL.md`.
2. Restart Codex, open your project folder and type `$customer-data-review`.

Or install the plugin from the command line: `codex plugin marketplace add dennyhollick/customer-data-review`, then `codex plugin add customer-data-review@pmm-camp`, and restart Codex.

**ChatGPT Business, Enterprise or Edu**
1. Open **Plugins → Skills → Create → Upload from your computer** and pick the ZIP. ChatGPT scans it before it's available; an admin can turn skill uploads off.
2. Start a chat, type `@customer-data-review` and attach your files. If ChatGPT says it can't run the skill's Python scripts, use Codex.

**ChatGPT Plus or Pro:** Skills aren't available in the browser. Use Codex in the ChatGPT desktop app, or the [chat workflow](chat-workflow.md).

## Start the run

```text
Use $customer-data-review on the customer files in my project folder (data/). Use the five bootcamp questions. My business: [two sentences]. The decision this should inform: [one sentence].
```

In ChatGPT, write `@customer-data-review` instead of `$customer-data-review`. The assistant asks anything it still needs, shows a short plan and then runs. It tells you once the run passes 15 minutes plus one minute per call (35 minutes for 20 calls) and keeps going unless you tell it to stop. If you need to leave partway, ask it to pause; saying "resume" later picks up from the saved checkpoint.

If the assistant cannot run Python or read your files, use the [chat workflow](chat-workflow.md) instead. It is quicker to set up, but its counts and quotes are not checked by code.

## What you get

- `reports/report.html`: the report to open in a browser. It has an executive summary, opportunities to explore, and one section per question with counted themes, takeaways and quotes.
- `reports/report.md`: the same report in Markdown.
- `reports/evidence.html`: the source evidence behind every count and quote.
- `reports/opportunities.md`: the opportunities on their own, ready for the Week 2 exercise.

Counts are calculated by code: each shows how many calls mention a theme, out of all calls reviewed. Every quote is checked word for word against its source. A spot-check reviews up to 20 extracted observations for meaning. It is a sample, not a guarantee that every line is right, so read the quotes you plan to reuse in context.

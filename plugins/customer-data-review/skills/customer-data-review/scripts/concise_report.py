"""Pure, concise reports from locked evidence; editorial meaning is reviewed separately.

Counts always describe distinct source files in the full cohort. They never
count highlighted examples, unique people, or the number of rendered quotes.
No model, filesystem write, ledger operation, or silent content removal occurs.
"""
import copy
import hashlib
import html
from pathlib import Path
import re

from skill.scripts.bounded_audit import _attribution_errors
from skill.scripts.source_spans import HEADER
from skill.scripts.verify_quotes import normalize_text


_NUMBER = r"(?:\d+(?:[.,]\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)"


def _prose(text, *, allow_count_placeholder=False):
    """Keep free prevalence out of prose; source prices/hours remain reviewable.

    This deliberately small boundary does not claim to understand numerical
    meaning. Ratios/percentages belong in code-owned counts or literal quotes.
    """
    if re.search(r'["“”]', text):
        raise ValueError("Authored prose must not contain inline quotations; use a validated quote block.")
    if "{calls}" in text and not allow_count_placeholder:
        raise ValueError("The {calls} placeholder is allowed only in finding text.")
    text = text.replace("{calls}", "")
    ratio = rf"\b{_NUMBER}\s*(?:/|(?:out\s+)?of)\s*(?:the\s+)?{_NUMBER}\b"
    percentage = rf"\b{_NUMBER}\s*(?:%|percent(?:age)?\b|per\s+cent\b)"
    count = rf"\b{_NUMBER}\s*(?:calls?|(?:source\s+)?files?|sources?|mentions?|customers?|accounts?|people|respondents?|interviews?)\b"
    ratios = [m.group() for m in re.finditer(ratio, text, re.I) if re.sub(r"\s", "", m.group()) != "24/7"]
    if ratios or re.search(percentage, text, re.I) or re.search(count, text, re.I):
        raise ValueError("Narrative prevalence must use the code-owned {calls} count, not free ratios, percentages or dataset/person counts.")


_UNITS = re.compile(r"(\n+|(?<=[.!?;])[ \t]+)")


def _prose_ok(text, allow_count_placeholder=False):
    try:
        _prose(text, allow_count_placeholder=allow_count_placeholder)
        return True
    except ValueError:
        return False


def clean_prose(text, *, allow_count_placeholder=False):
    """Repair authored prose instead of failing the whole report.

    Double-quote characters are removed (checked quotes live in quote blocks).
    A sentence, clause (after ;) or line that still states free counts, ratios
    or percentages is dropped: code owns every number. Separators between kept
    units are preserved. Returns (text, dropped_units).
    """
    if not isinstance(text, str):
        return text, []
    text = re.sub(r'["“”]', "", text)
    if _prose_ok(text, allow_count_placeholder):
        return text, []
    pieces = _UNITS.split(text)
    kept, dropped, separator = [], [], ""
    for index, piece in enumerate(pieces):
        if index % 2:
            separator = piece
            continue
        if not piece.strip():
            continue
        if _prose_ok(piece, allow_count_placeholder):
            kept.append((separator if kept else "") + piece)
        else:
            dropped.append(piece.strip())
    result = "".join(kept).strip()
    if dropped and result.endswith(";"):
        result = result[:-1].rstrip() + "."
    return result, dropped


OPPORTUNITY_FIELDS = ("title", "problem", "hypothesis", "next_step")


def _clean_pair(item, label, log, fields=("title", "text"), **context):
    """Clean text fields so their joined form also passes; False if unusable."""
    for field in fields:
        item[field], dropped = clean_prose(item.get(field))
        log += [{**context, "field": f"{label} {field}", "dropped": d} for d in dropped]
    usable = all(isinstance(item.get(f), str) and item[f].strip() for f in fields)
    if usable and not _prose_ok(" ".join(item[f] for f in fields)):
        usable = False
    if not usable:
        log.append({**context, "field": label, "dropped": f"whole {label} (no valid text left, or its title and text together state a count)"})
    return usable


def clean_section(row):
    """Return a cleaned copy of one question section plus a log of changes.

    Theme labels are only stripped of quote characters; a label that states a
    count raises a targeted error because a theme cannot be dropped.
    """
    row, log = copy.deepcopy(row), []
    if not isinstance(row, dict):
        return row, log
    qid = row.get("id")
    if isinstance(row.get("summary"), str):
        row["summary"], dropped = clean_prose(row["summary"])
        log += [{"question_id": qid, "field": "summary", "dropped": d} for d in dropped]
        if not row["summary"].strip():
            row.pop("summary")
    for theme in row.get("themes", []) if isinstance(row.get("themes"), list) else []:
        if isinstance(theme, dict) and isinstance(theme.get("label"), str):
            theme["label"] = re.sub(r'["“”]', "", theme["label"])
            if not _prose_ok(theme["label"]):
                raise ValueError(f"{qid} theme label '{theme['label']}' states a count or percentage; rewrite the label without numbers.")
    if isinstance(row.get("takeaways"), list):
        row["takeaways"] = [t for t in row["takeaways"]
                            if not isinstance(t, dict) or _clean_pair(t, "takeaway", log, question_id=qid)]
    return row, log


def clean_executive(draft):
    """Return a cleaned executive draft plus a log; never invents content.

    Raises a targeted error when a required field would be emptied, so the
    writer knows exactly what to fix instead of retrying blindly.
    """
    draft, log = copy.deepcopy(draft), []
    if not isinstance(draft, dict):
        return draft, log
    if isinstance(draft.get("headline"), str):
        original = draft["headline"]
        draft["headline"], dropped = clean_prose(original)
        log += [{"field": "headline", "dropped": d} for d in dropped]
        if original.strip() and not draft["headline"].strip():
            raise ValueError(f"Headline removed because it states a count or percentage: '{original}'. Rewrite it without numbers.")
    removed = set()
    if isinstance(draft.get("findings"), list):
        kept = []
        for finding in draft["findings"]:
            if not isinstance(finding, dict):
                kept.append(finding)
                continue
            text = finding.get("text")
            if finding.get("theme_id") is None and isinstance(text, str) and "{calls}" in text:
                raise ValueError(f"Finding {finding.get('id')} uses {{calls}} without a theme_id. Add the theme_id it counts, or remove {{calls}}.")
            if isinstance(text, str) and text.count("{calls}") > 1:
                units, seen = _UNITS.split(text), False
                for i in range(0, len(units), 2):
                    if "{calls}" in units[i]:
                        if seen:
                            log.append({"finding": finding.get("id"), "field": "text", "dropped": units[i].strip()})
                            units[i] = ""
                        seen = True
                finding["text"] = re.sub(r"\s{2,}", " ", "".join(units)).strip()
            finding["title"], dropped = clean_prose(finding.get("title"))
            log += [{"finding": finding.get("id"), "field": "title", "dropped": d} for d in dropped]
            finding["text"], dropped = clean_prose(finding.get("text"), allow_count_placeholder=finding.get("theme_id") is not None)
            log += [{"finding": finding.get("id"), "field": "text", "dropped": d} for d in dropped]
            if all(isinstance(finding.get(f), str) and finding[f].strip() for f in ("title", "text")):
                kept.append(finding)
            else:
                removed.add(finding.get("id"))
                log.append({"finding": finding.get("id"), "field": "finding", "dropped": "whole finding (no valid text left)"})
        if draft["findings"] and not kept:
            raise ValueError("Every executive finding was removed because it states counts or percentages "
                             f"({', '.join(str(i) for i in sorted(removed, key=str))}). Rewrite them without numbers; use {{calls}} with a theme_id for counts.")
        draft["findings"] = kept
    if isinstance(draft.get("opportunities"), list):
        kept = []
        for item in draft["opportunities"]:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            if isinstance(item.get("finding_ids"), list) and removed & set(item["finding_ids"]):
                item["finding_ids"] = [i for i in item["finding_ids"] if i not in removed]
                log.append({"opportunity": item.get("title"), "field": "finding_ids", "dropped": sorted(removed)})
                if not item["finding_ids"]:
                    log.append({"opportunity": item.get("title"), "field": "opportunity", "dropped": "whole opportunity (its findings were removed)"})
                    continue
            if _clean_pair(item, "opportunity", log, fields=OPPORTUNITY_FIELDS, opportunity=item.get("title")):
                kept.append(item)
        draft["opportunities"] = kept
    return draft, log


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a nonempty string.")
    return value


def _rows(value, key, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array.")
    result = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError(f"{label} contains a malformed object.")
        identity = _text(row.get(key), f"{label} identity")
        if identity in result:
            raise ValueError(f"{label} contains duplicate identity {identity}.")
        result[identity] = row
    return result


def _ids(value, label, *, empty=False):
    if (not isinstance(value, list) or (not empty and not value) or
            any(not isinstance(v, str) or not v.strip() for v in value) or len(value) != len(set(value))):
        raise ValueError(f"{label} needs distinct nonempty IDs.")
    return set(value)


def _fields(row, required, label, optional=()):
    if not isinstance(row, dict) or not set(required) <= set(row) or set(row) - set(required) - set(optional):
        raise ValueError(f"{label} has missing or unknown fields.")


def _index(inventory, source_texts):
    if not isinstance(inventory, dict) or not isinstance(source_texts, dict):
        raise ValueError("Locked inventory and full source texts must be objects.")
    cohort = _ids(inventory.get("source_file_ids"), "Cohort")
    if set(source_texts) != cohort or any(not isinstance(v, str) for v in source_texts.values()):
        raise ValueError("Full source texts must match the entire cohort exactly.")
    questions = _rows(inventory.get("questions"), "question_id", "Inventory questions")
    if not questions:
        raise ValueError("At least one research question is required.")
    mentions, owners, themes, counts = {}, {}, {}, {}
    normalized = {sid: normalize_text(text) for sid, text in source_texts.items()}
    for qid, question in questions.items():
        _text(question.get("question_text"), "Research question")
        local = _rows(question.get("mentions"), "mention_id", f"{qid} mentions")
        qthemes = _rows(question.get("themes"), "theme_id", f"{qid} themes")
        if set(local) & set(mentions) or set(qthemes) & set(themes):
            raise ValueError("Mention and theme IDs must be globally unique.")
        if set(qthemes) & set(questions) or set(local) & (set(qthemes) | set(themes)) or set(qthemes) & set(mentions):
            raise ValueError("Question, theme and mention identity namespaces must not overlap.")
        for tid, theme in qthemes.items():
            _text(theme.get("label"), "Locked theme label")
            themes[tid] = theme
            owners[tid] = qid
        for mid, mention in local.items():
            if (not isinstance(mention.get("source_id"), str) or not isinstance(mention.get("theme_id"), str) or
                    mention["source_id"] not in cohort or mention["theme_id"] not in qthemes):
                raise ValueError(f"{mid}: unknown source or cross-question theme.")
            if mention.get("question_id", qid) != qid:
                raise ValueError(f"{mid}: cross-question mention.")
            for field in ("mention", "quote", "quote_attribution"):
                _text(mention.get(field), f"{mid} {field}")
            if normalize_text(mention["quote"]) not in normalized[mention["source_id"]]:
                raise ValueError(f"{mid}: retained quotation is not in its original source.")
            if _attribution_errors(mention["quote_attribution"], source_texts[mention["source_id"]]):
                raise ValueError(f"{mid}: retained attribution lacks literal source support.")
            mentions[mid] = mention
            owners[mid] = qid
        stats = _rows(question.get("stats"), "stat_ref", f"{qid} statistics")
        expected_refs = {f"{scope}:{suffix}" for scope in (qid, *qthemes) for suffix in ("mentions", "sources")}
        if set(stats) != expected_refs:
            raise ValueError(f"{qid}: locked statistic coverage differs from question/themes.")
        qsources = {m["source_id"] for m in local.values()}
        for scope in (qid, *qthemes):
            members = {mid: m for mid, m in local.items() if scope == qid or m["theme_id"] == scope}
            sources = {m["source_id"] for m in members.values()}
            for suffix, unit, numerator, denominator in (
                    ("mentions", "mentions", set(members), set(local)),
                    ("sources", "source_files", sources, qsources)):
                stat = stats[f"{scope}:{suffix}"]
                if (stat.get("unit") != unit or stat.get("scope") != qid or
                        _ids(stat.get("numerator_ids"), "Statistic numerator", empty=True) != numerator or
                        _ids(stat.get("denominator_ids"), "Statistic denominator", empty=True) != denominator or
                        type(stat.get("numerator")) is not int or stat["numerator"] != len(numerator) or
                        type(stat.get("denominator")) is not int or stat["denominator"] != len(denominator) or
                        stat.get("percentage") != (round(100 * len(numerator) / len(denominator)) if denominator else 0)):
                    raise ValueError(f"{scope}:{suffix}: locked count or membership sets changed.")
            if scope != qid:
                counts[scope] = {"theme_id": scope, "source_count": len(sources), "cohort_denominator": len(cohort),
                                 "source_ids": sorted(sources), "mention_ids": list(members),
                                 "original_question_denominator": len(qsources)}
    return {"cohort": cohort, "questions": questions, "mentions": mentions, "themes": themes,
            "owners": owners, "counts": counts}


_RANK = re.compile(r"\b(?:second|third)[- ](?:most|biggest|largest|highest|top)\b|\bmost (?:common|frequent|frequently|cited|mentioned|widespread)\b|\bmore (?:sources|calls) than any\b|\bthe most (?:sources|calls|mentions|support)\b|\bthe (?:single )?(?:biggest|largest|dominant|number[- ]one|#1|leading (?:pain|problem|objection|request|reason|concern|theme)|top (?:pain|problem|objection|request|reason|concern|theme|complaint|driver))\b|\b(?:ranked|ranks) (?:first|highest)\b", re.I)
_BREADTH = re.compile(r"\b(?:recurring|common|frequent|widespread|repeated) (?:objection|objections|theme|pain|pains|concern|concerns|request|requests|complaint|complaints|issue|issues|pattern|problem|problems|reason|reasons|question|questions|topic)\b|\b(?:commonly|repeatedly|consistently|frequently|often) (?:cited|raised|mentioned|described|reported|came up|comes up)\b|\bcame up (?:repeatedly|often|again and again)\b|\bwidespread\b|\b(?:many|several|most|multiple) (?:studios|customers|prospects|calls|teams|buyers|owners|managers)\b|\bacross (?:studios|calls|customers|prospects)\b", re.I)


def _claims(text, support_ids, index, theme_id=None, label="Claim"):
    """Deterministic guard on two writing errors code can see.

    Breadth words need evidence from at least two sources. Ranking words need a
    strict lead in the locked counts (ties cannot be ranked).
    """
    mentions = [index["mentions"][mid] for mid in support_ids if mid in index["mentions"]]
    sources = {m["source_id"] for m in mentions}
    breadth = _BREADTH.search(text)
    if breadth and len(sources) < 2 and theme_id is None:
        raise ValueError(f"{label}: '{breadth.group()}' describes breadth, but its evidence comes from one source. Describe that source's situation instead.")
    if breadth and theme_id is not None and index["counts"][theme_id]["source_count"] < 2:
        raise ValueError(f"{label}: '{breadth.group()}' describes breadth, but the theme has one source.")
    rank = _RANK.search(text)
    if not rank:
        return
    themes = [theme_id] if theme_id is not None else sorted({m["theme_id"] for m in mentions})
    if not themes:
        return
    theme = max(themes, key=lambda t: index["counts"][t]["source_count"])
    qid = index["owners"][theme]
    counts = sorted((index["counts"][t]["source_count"] for t in (x["theme_id"] for x in index["questions"][qid]["themes"]) if t in index["counts"]), reverse=True)
    mine = index["counts"][theme]["source_count"]
    word = rank.group().lower()
    position = 2 if word.startswith("second") else 3 if word.startswith("third") else 1
    strict = len(counts) >= position and counts[position - 1] == mine and counts.count(mine) == 1
    if not strict:
        raise ValueError(f"{label}: '{rank.group()}' ranks a theme, but {qid}'s counts do not give it a clear position {position} "
                         f"(its count {mine}; question counts {counts[:5]}). Remove the ranking or describe it without ordering.")


def _support(ids, index, *, qid=None, tid=None):
    values = _ids(ids, "Supporting evidence")
    for mid in values:
        if mid not in index["mentions"]:
            raise ValueError(f"Unknown supporting mention {mid}.")
        if qid is not None and index["owners"][mid] != qid:
            raise ValueError("Takeaway support belongs to another question.")
        if tid is not None and index["mentions"][mid]["theme_id"] != tid:
            raise ValueError("Finding examples must belong to its counted theme.")


def _quote(item, qid, index, source_texts):
    _fields(item, ("mention_id", "text", "attribution"), "Quote")
    mid = _text(item["mention_id"], "Quote mention ID")
    if mid not in index["mentions"] or index["owners"][mid] != qid:
        raise ValueError("Quote must belong to its question.")
    mention = index["mentions"][mid]
    excerpt = _text(item["text"], "Quote excerpt")
    attribution = _text(item["attribution"], "Quote attribution")
    text = source_texts[mention["source_id"]]
    if excerpt not in mention["quote"] or text.count(excerpt) != 1:
        where = "is not inside" if excerpt not in mention["quote"] else f"appears {text.count(excerpt)} times in"
        raise ValueError(f"{mid}: quote excerpt {where} the mention's retained quote/source. Copy a contiguous span character for character "
                         f"from this mention's quote field (it must occur exactly once in the source): {mention['quote'][:160]!r}")
    start, end = text.index(excerpt), text.index(excerpt) + len(excerpt)
    headers = list(HEADER.finditer(text))
    timestamp = None
    if headers:
        preceding = [h for h in headers if h.end() <= start]
        if not preceding:
            raise ValueError("Quote occurs before a grounded speaker turn.")
        header = preceding[-1]
        next_start = next((h.start() for h in headers if h.start() > header.start()), len(text))
        if end > next_start:
            raise ValueError("Quote crosses speaker turns.")
        speaker = header.group("speaker").strip()
        base = re.sub(r"\s*\([^)]*\)\s*$", "", speaker).strip()
        if attribution.casefold() not in {speaker.casefold(), base.casefold()}:
            raise ValueError(f"{mid}: quote attribution must be the source speaker exactly: use {base!r} (or {speaker!r}).")
        timestamp = re.search(r"\d{1,2}:\d{2}(?::\d{2})?", header.group()).group()
    else:
        original = mention["quote_attribution"]
        # Existing grounded extraction attribution is the fallback for surveys,
        # reviews and un-timestamped sources. Never add a speaker's job title.
        base = re.sub(r"\s*\((?:sales call|renewal call|churn interview|cs call|customer success call|survey|review|support ticket|source: [^)]*)\)\s*$", "", original, flags=re.I).strip()
        if attribution.casefold() not in {original.casefold(), base.casefold()} or _attribution_errors(original, text):
            raise ValueError(f"{mid}: quote attribution must be {base!r} (or {original!r}).")
        speaker = original
    return {"mention_id": mid, "source_id": mention["source_id"], "excerpt": excerpt,
            "speaker": speaker, "timestamp": timestamp, "start_character": start, "end_character": end,
            "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "exact_contiguous_source_match": True}


def _sections(questions, index, source_texts):
    rows = _rows(questions, "id", "Report questions")
    if set(rows) != set(index["questions"]):
        raise ValueError("Report must include every research question exactly once.")
    quote_checks = []
    for qid, row in rows.items():
        _fields(row, ("id", "title", "themes", "takeaways", "quotes"), "Report question", ("summary", "nav_label"))
        _text(row["title"], "Question title")
        if "nav_label" in row:
            _text(row["nav_label"], "Navigation label")
        if "summary" in row and row["summary"] is not None:
            _prose(_text(row["summary"], "Optional question summary"))
        themes = _rows(row["themes"], "theme_id", "Displayed themes")
        expected = {t["theme_id"] for t in index["questions"][qid]["themes"]}
        if set(themes) != expected:
            raise ValueError(f"{qid}: report must retain every locked theme, including thin themes.")
        for theme in themes.values():
            _fields(theme, ("theme_id", "label"), "Displayed theme")
            _text(theme["label"], "Displayed theme label")
            _prose(theme["label"])
        if not isinstance(row["takeaways"], list) or len(row["takeaways"]) > 3:
            raise ValueError("A question permits at most three takeaways.")
        for takeaway in row["takeaways"]:
            _fields(takeaway, ("title", "text", "support_ids"), "Takeaway")
            _text(takeaway["title"], "Takeaway title")
            _text(takeaway["text"], "Takeaway text")
            _prose(takeaway["title"] + " " + takeaway["text"])
            _support(takeaway["support_ids"], index, qid=qid)
            _claims(takeaway["title"] + " " + takeaway["text"], takeaway["support_ids"], index, label=f"{qid} takeaway '{takeaway['title']}'")
        quotes = _rows(row["quotes"], "mention_id", "Display quotes")
        if len(quotes) > 2:
            raise ValueError("A question permits at most two display quotes.")
        quote_checks.extend(_quote(item, qid, index, source_texts) for item in quotes.values())
    return quote_checks


def drop_invalid_quotes(row, inventory, source_texts):
    """Final-attempt fallback: keep a section, dropping quotes that fail the exact-match check."""
    index = _index(inventory, source_texts)
    row, dropped = copy.deepcopy(row), []
    kept = []
    for item in row.get("quotes", []) if isinstance(row.get("quotes"), list) else []:
        try:
            _quote(item, row.get("id"), index, source_texts)
            kept.append(item)
        except (ValueError, TypeError, KeyError) as exc:
            dropped.append({"question_id": row.get("id"), "field": "quote", "dropped": f"{item.get('text') if isinstance(item, dict) else item} ({exc})"})
    row["quotes"] = kept
    return row, dropped


def validate_sections(questions, inventory, source_texts):
    """Raise on material structural/provenance errors; never mutate or omit input."""
    _sections(questions, _index(inventory, source_texts), source_texts)


def _executive(content, index, require_executive):
    _fields(content, ("title", "headline", "findings", "questions"), "Report content", ("opportunities",))
    _text(content["title"], "Report title")
    _text(content["headline"], "Executive headline", empty=not require_executive or not index["mentions"])
    _prose(content["headline"])
    findings = _rows(content["findings"], "id", "Executive findings")
    if len(findings) > 5 or (require_executive and index["mentions"] and not findings):
        raise ValueError("Evidence-bearing reports require one to five useful executive findings; three to five is typical.")
    for finding in findings.values():
        _fields(finding, ("id", "title", "theme_id", "text", "support_ids"), "Executive finding")
        _text(finding["title"], "Finding title")
        text = _text(finding["text"], "Finding text")
        _prose(finding["title"])
        _prose(text, allow_count_placeholder=True)
        tid = finding["theme_id"]
        if tid is not None and (not isinstance(tid, str) or tid not in index["themes"]):
            raise ValueError("Finding has an unknown theme.")
        if (tid is None and "{calls}" in text) or text.count("{calls}") > 1:
            raise ValueError("A count placeholder requires one explicit locked theme and may appear once.")
        _support(finding["support_ids"], index, tid=tid)
        _claims(finding["title"] + " " + text, finding["support_ids"], index, theme_id=tid, label=f"Finding {finding['id']}")
    opportunities = content.get("opportunities", [])
    if not isinstance(opportunities, list) or len(opportunities) > 5:
        raise ValueError("Opportunities must be an array of at most five items.")
    for item in opportunities:
        _fields(item, (*OPPORTUNITY_FIELDS, "finding_ids"), "Opportunity")
        _prose(" ".join(_text(item[f], f"Opportunity {f}") for f in OPPORTUNITY_FIELDS))
        if not _ids(item["finding_ids"], "Opportunity finding IDs") <= set(findings):
            raise ValueError("Opportunity finding_ids must reference executive finding IDs.")
        support = [mid for fid in item["finding_ids"] for mid in findings[fid]["support_ids"]]
        _claims(" ".join(item[f] for f in OPPORTUNITY_FIELDS), support, index, label=f"Opportunity '{item['title']}'")


def _anchor(kind, identity):
    return kind + "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _md(text):
    # Treat every supplied field as plain text, including pipe/newline table
    # injection and apparent markdown URLs. Links below are all code-owned.
    text = html.escape(re.sub(r"\s+", " ", text), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|\-])", r"\\\1", text)


def _page(title, body):
    css = (Path(__file__).resolve().parents[1] / "templates/concise-report.css").read_text(encoding="utf-8")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{css}</style></head><body>{body}</body></html>')


def build_document(content, inventory, source_texts, *, metadata=None, require_executive=True):
    """Return HTML/Markdown and self-contained evidence; write nothing to disk."""
    index = _index(inventory, source_texts)
    checks = _sections(content.get("questions") if isinstance(content, dict) else None, index, source_texts)
    _executive(content, index, require_executive)
    metadata = copy.deepcopy({} if metadata is None else metadata)
    if not isinstance(metadata, dict):
        raise ValueError("Report metadata must be an object.")
    unit = metadata.get("cohort_unit", metadata.get("source_unit", "source files"))
    if "source_unit" in metadata and metadata["source_unit"] != unit:
        raise ValueError("Source count unit metadata is inconsistent.")
    if unit not in {"calls", "source files"}:
        raise ValueError("Count units must be calls or source files, never inferred people/accounts.")
    description = _text(metadata.get("cohort_description", unit), "Cohort description")
    review_notes = _text(metadata.get("review_notes", "Mechanical provenance and arithmetic checks do not establish semantic accuracy. Interpretations require editorial review."), "Review notes")
    n = len(index["cohort"])
    e = html.escape
    counts = copy.deepcopy(index["counts"])
    if unit == "calls":
        for count in counts.values():
            count["call_count"] = count["source_count"]
    method = (f'Based on {n} {description}, analyzed across {len(content["questions"])} research questions. '
              f'Counts show distinct {unit} mentioning a theme, out of all {n} reviewed; one source may appear in multiple themes. '
              'They describe this sample, not the wider market. Quoted excerpts match their original sources.')
    if metadata.get("methodology_note") is not None:
        method += " " + _text(metadata["methodology_note"], "Methodology note")
    finding_html, finding_md, evidence_parts = [], [], []
    for finding in content["findings"]:
        tid = finding["theme_id"]
        anchor = _anchor("theme", tid) if tid is not None else _anchor("finding", finding["id"])
        # {calls} already carries its unit; drop a unit word the writer repeated after it.
        written = re.sub(r"\{calls\}(\s+)(?:calls?|source files?|sources?|files?|interviews?)\b", "{calls}", finding["text"])
        text = e(written)
        plain = written
        if tid is not None:
            metric = f'{counts[tid]["source_count"]} of {n} {unit}'
            text = text.replace("{calls}", f'<a class="metric" href="evidence.html#{anchor}">{e(metric)}</a>')
            plain = plain.replace("{calls}", metric)
        if "{calls}" not in finding["text"]:
            text += f' <a class="source-link" href="evidence.html#{anchor}">Evidence</a>'
        finding_html.append(f'<article class="finding"><h3>{e(finding["title"])}</h3><p>{text}</p></article>')
        finding_md.append(f'**{_md(finding["title"])}** {_md(plain)} ([Evidence](evidence.html#{anchor}))')

    def evidence_item(mid, anchor=False):
        mention = index["mentions"][mid]
        identity = f' id="{_anchor("quote", mid)}"' if anchor else ''
        return (f'<article class="evidence-mention"{identity}><p><strong>{e(mention["quote_attribution"])}</strong></p>'
                f'<p>{e(mention["mention"])}</p><blockquote>“{e(mention["quote"])}”</blockquote>'
                f'<p class="evidence-meta">Source: {e(mention["source_id"])} · Evidence: {e(mid)}</p></article>')

    for tid, count in counts.items():
        evidence_parts.append(f'<section class="evidence-section" id="{_anchor("theme", tid)}"><h2>{e(index["themes"][tid]["label"])}</h2>'
                              f'<p>{count["source_count"]} of {n} reviewed {e(unit)}. Repeated observations in the same source count once.</p>'
                              + ''.join(evidence_item(mid) for mid in count["mention_ids"]) + '</section>')
    for finding in content["findings"]:
        if finding["theme_id"] is None:
            evidence_parts.append(f'<section class="evidence-section" id="{_anchor("finding", finding["id"])}"><h2>{e(finding["title"])}</h2><p>{e(finding["text"])}</p>'
                                  + ''.join(evidence_item(mid) for mid in finding["support_ids"]) + '</section>')
    navigation, section_html, section_md, takeaway_evidence = [], [], [], []
    checked_quotes = {q["mention_id"]: q for q in checks}
    for number, question in enumerate(content["questions"], 1):
        qid = question["id"]
        qa = _anchor("question", qid)
        navigation.append(f'<a href="#{qa}">{number:02d} · {e(question.get("nav_label", question["title"]))}</a>')
        rows, md_rows = [], []
        for theme in question["themes"]:
            tid, label = theme["theme_id"], theme["label"]
            count = counts[tid]["source_count"]
            ta = _anchor("theme", tid)
            rows.append(f'<tr><td>{e(label)}</td><td class="count-cell"><a class="count-link" href="evidence.html#{ta}" '
                        f'aria-label="{e(label)}: {count} of {n} {e(unit)}. View evidence."><span class="bar-track" aria-hidden="true">'
                        f'<span class="bar" style="width:{count / n * 100:g}%"></span></span><span class="number">{count}</span></a></td></tr>')
            md_rows.append(f'| {_md(label)} | [{count}](evidence.html#{ta}) |')
        takes, takes_md, quotes, quotes_md = [], [], [], []
        for position, takeaway in enumerate(question["takeaways"], 1):
            ta = _anchor("takeaway", f"{qid}:{position}")
            takes.append(f'<li><strong>{e(takeaway["title"])}</strong> {e(takeaway["text"])} <a class="source-link" href="evidence.html#{ta}">Sources</a></li>')
            takes_md.append(f'- **{_md(takeaway["title"])}** {_md(takeaway["text"])} ([Sources](evidence.html#{ta}))')
            evidence_parts.append(f'<section class="evidence-section" id="{ta}"><h2>{e(takeaway["title"])}</h2><p>{e(takeaway["text"])}</p>'
                                  + ''.join(evidence_item(mid) for mid in takeaway["support_ids"]) + '</section>')
            takeaway_evidence.append({"question_id": qid, **copy.deepcopy(takeaway)})
        for quote in question["quotes"]:
            checked = checked_quotes[quote["mention_id"]]
            qa_quote = _anchor("quote", quote["mention_id"])
            label = checked["timestamp"] or "Source excerpt"
            quotes.append(f'<figure><blockquote>“{e(quote["text"])}”</blockquote><figcaption><strong>{e(checked["speaker"])}</strong>'
                          f'<a href="evidence.html#{qa_quote}">{e(label)}</a></figcaption></figure>')
            quotes_md.append(f'> {_md(quote["text"])}\n>\n> — {_md(checked["speaker"])} · [{_md(label)}](evidence.html#{qa_quote})')
            evidence_parts.append(evidence_item(quote["mention_id"], anchor=True))
        summary = question.get("summary")
        table = (f'<div class="table-wrap"><table><thead><tr><th scope="col">Theme</th><th scope="col">{e(unit.capitalize())} (of {n})</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table><p class="table-note">One source can appear in more than one theme. Select a count to see the supporting evidence.</p></div>') if rows else '<p class="empty-evidence">No retained evidence for this question.</p>'
        section_html.append(f'<section class="question-section" id="{qa}" aria-labelledby="{qa}-title"><p class="eyebrow">{number:02d} · {e(question.get("nav_label", qid))}</p>'
                            f'<h2 id="{qa}-title">{e(question["title"])}</h2>'
                            + (f'<p class="section-summary">{e(summary)}</p>' if summary else '') + table
                            + (f'<div class="takeaways"><h3>Key takeaways</h3><ul>{"".join(takes)}</ul></div>' if takes else '')
                            + (f'<div class="voices"><h3>In their words</h3><div class="quotes">{"".join(quotes)}</div></div>' if quotes else '') + '</section>')
        section_md.append(f'## {_md(qid)}. {_md(question["title"])}\n\n' + (_md(summary) + '\n\n' if summary else '')
                          + (f'| Theme | {_md(unit.capitalize())} (of {n}) |\n| --- | ---: |\n' + '\n'.join(md_rows) + '\n\nOne source can appear in more than one theme.' if rows else 'No retained evidence for this question.')
                          + ('\n\n### Key takeaways\n\n' + '\n\n'.join(takes_md) if takes_md else '')
                          + ('\n\n### In their words\n\n' + '\n\n'.join(quotes_md) if quotes_md else ''))
    opportunities = content.get("opportunities", [])
    finding_titles = {f["id"]: f["title"] for f in content["findings"]}
    opp_html = ''.join(f'<article class="finding opportunity"><h3>{e(o["title"])}</h3>'
                       f'<p><strong>Problem:</strong> {e(o["problem"])}</p><p><strong>Hypothesis:</strong> {e(o["hypothesis"])}</p>'
                       f'<p><strong>Next step:</strong> {e(o["next_step"])}</p>'
                       f'<p class="evidence-meta">Evidence: {e("; ".join(finding_titles[i] for i in o["finding_ids"]))}</p></article>'
                       for o in opportunities)
    opp_md = '\n\n'.join(f'### {_md(o["title"])}\n\n- **Problem:** {_md(o["problem"])}\n- **Hypothesis:** {_md(o["hypothesis"])}\n'
                          f'- **Next step:** {_md(o["next_step"])}\n- **Evidence:** {_md("; ".join(finding_titles[i] for i in o["finding_ids"]))}'
                          for o in opportunities)
    show_exec = bool(content["headline"] or finding_html)
    subtitle = _text(metadata.get("subtitle", f'{n} {description} · {len(content["questions"])} research questions'), "Subtitle")
    footer = _text(metadata.get("footer", content["title"]), "Footer")
    executive = (f'<section class="executive" id="executive-summary" aria-labelledby="exec-title"><p class="eyebrow">Executive summary</p><h1 id="exec-title">{e(content["headline"])}</h1><div class="findings">{"".join(finding_html)}</div>' + (f'<h2 class="opportunities-title">Opportunities to explore</h2><div class="findings">{opp_html}</div>' if opp_html else '') + '</section>' if show_exec else '')
    nav_links = ('<a href="#executive-summary">Executive summary</a>' if show_exec else '') + ''.join(navigation) + '<a href="#methodology">Methodology</a>'
    body = (f'<header class="topbar"><div class="brand">{e(content["title"])}</div><button onclick="window.print()" type="button">Print report</button></header>'
            f'<div class="layout"><aside class="sidebar"><nav class="desktop-contents" aria-label="Report contents"><p class="nav-label">In this report</p>{nav_links}</nav>'
            f'<details class="mobile-contents"><summary>Contents</summary><nav aria-label="Report contents">{nav_links}</nav></details>'
            f'</aside><main><p class="meta">{e(subtitle)}</p>{executive}{"".join(section_html)}'
            + f'<section class="methodology" id="methodology"><details><summary>Methodology &amp; sources</summary><p>{e(method)}</p><p><a href="evidence.html">View supporting evidence and review notes</a></p></details></section><footer>{e(footer)}</footer></main></div>')
    evidence_body = (f'<main class="evidence-main"><a href="report.html">← Back to report</a><h1>Supporting evidence</h1>'
                     f'<p>Counts use all {n} reviewed {e(unit)} as the denominator. Takeaways and findings are interpretations grounded in the source examples below.</p>'
                     f'<details class="methodology"><summary>Review notes</summary><p>{e(review_notes)}</p></details>{"".join(evidence_parts)}'
                     '<footer><a href="report.html">Back to report</a></footer></main>')
    report_md = f'# {_md(content["title"])}\n\n{_md(subtitle)}\n\n'
    if show_exec:
        report_md += f'## Executive summary\n\n### {_md(content["headline"])}\n\n' + '\n\n'.join(finding_md) + '\n\n'
        if opp_md:
            report_md += '### Opportunities to explore\n\n' + opp_md + '\n\n'
    report_md += '\n\n'.join(section_md) + f'\n\n## Methodology\n\n{_md(method)}\n\n[Supporting evidence and review notes](evidence.html)\n'
    evidence = {"cohort_unit": unit, "cohort_denominator": n, "cohort_source_ids": sorted(index["cohort"]),
                "theme_counts": counts, "quote_checks": checks, "findings": copy.deepcopy(content["findings"]),
                "opportunities": copy.deepcopy(opportunities),
                "takeaways": takeaway_evidence, "review_notes": review_notes,
                "source_sha256": {sid: hashlib.sha256(text.encode("utf-8")).hexdigest() for sid, text in source_texts.items()}}
    validation = {"status": "pass", "errors": [], "question_count": len(content["questions"]), "cohort_denominator": n,
                  "displayed_theme_counts_verified": len(counts), "exact_quote_excerpts_verified": len(checks),
                  "referenced_mentions_resolved": True, "scope": "Mechanical provenance/count checks only; editorial meaning is reviewed separately."}
    opportunities_md = ("# Opportunities to explore\n\n" + (opp_md or "No opportunities were identified.")
                        + "\n\nThese are hypotheses from the report's executive findings, not established facts. "
                        "See [the report](report.md) for evidence.\n")
    return {"report_html": _page(content["title"], body), "report_md": report_md, "opportunities_md": opportunities_md,
            "evidence_html": _page(content["title"] + " · Supporting evidence", evidence_body),
            "evidence": evidence, "validation": validation}

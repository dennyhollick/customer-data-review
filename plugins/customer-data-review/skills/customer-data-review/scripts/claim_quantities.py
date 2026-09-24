"""Bind prose quantities to exact claim evidence, without judging its meaning.

Support statistics never absorb counterevidence. A writer may explicitly bind a
contrast clause to cited evidence; that declaration remains a semantic judgment.
This module computes membership and arithmetic, never model-supplied totals.
"""

import hashlib
import json
import re

from skill.scripts.verify_report_evidence import quoted_spans


_WORDS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), range(21)))
_WORDS.update(thirty=30, forty=40, fifty=50, sixty=60, seventy=70, eighty=80, ninety=90)
_WORD_NUMBER = r"(?:" + "|".join(_WORDS) + r")(?:(?:\s+|-)(?:" + "|".join(_WORDS) + r"|hundred|thousand|and|point))*"
_NUMBER = r"(?:-?\d+(?:,\d{3})*(?:\.\d+)?|" + _WORD_NUMBER + r"|another|both)"
_UNIT = r"(?:source[ -]+types?|source[ -]+files?|sources?|mentions?|customers?|accounts?|respondents?|prospects?|users?|people|persons?|participants?)"
# These explicit modifiers leave the counted unit intact. Their semantic truth
# (for example, whether the sources are prospects) is not an arithmetic check.
_MOD = r"(?:(?:the|distinct|unique|retained|cited|supporting|sampled|prospect|prospective|customer|current|existing|former|potential|new|sales|demo|Q\d+)\s+)*"
_START = r"(?<![\w.,-])"
_PERCENT = re.compile(_START + r"(?P<number>" + _NUMBER + r")\s*(?:%|percent\b)"
                      r"(?:\s+(?:of\s+)?" + _MOD + r"(?:(?P<denominator>" + _NUMBER + r")\s+" + _MOD + r")?"
                      r"(?P<unit>" + _UNIT + r")\b)?", re.I)
_RATIO = re.compile(_START + r"(?P<number>" + _NUMBER + r")\s*(?:/|\s+(?:of|out\s+of)\s+)\s*"
                    r"(?:the\s+)?(?P<denominator>" + _NUMBER + r")(?![\w.])"
                    r"(?:\s+" + _MOD + r"(?P<unit>" + _UNIT + r")\b)?", re.I)
_COUNT = re.compile(_START + r"(?P<number>" + _NUMBER + r")\s+" + _MOD + r"(?P<unit>" + _UNIT + r")\b", re.I)


def _number(value):
    text = value.lower()
    if text in {"another", "both"}:
        return 1 if text == "another" else 2
    if re.fullmatch(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text):
        return float(text.replace(",", ""))
    words = re.split(r"[\s-]+", text)

    def small(parts):
        hundreds = 0
        if len(parts) >= 2 and parts[1] == "hundred" and 1 <= _WORDS.get(parts[0], 0) <= 9:
            hundreds, parts = _WORDS[parts[0]] * 100, parts[2:]
            if parts[:1] == ["and"]:
                parts = parts[1:]
        if not parts:
            return hundreds
        if len(parts) == 1 and parts[0] in _WORDS:
            return hundreds + _WORDS[parts[0]]
        if (len(parts) == 2 and _WORDS.get(parts[0], 0) >= 20 and
                _WORDS.get(parts[0], 1) % 10 == 0 and 1 <= _WORDS.get(parts[1], 0) <= 9):
            return hundreds + _WORDS[parts[0]] + _WORDS[parts[1]]
        return float("nan")  # A malformed number cannot silently become a smaller count.

    if "point" in words:
        point = words.index("point")
        fraction = words[point + 1:]
        if not fraction or any(w not in _WORDS or _WORDS[w] > 9 for w in fraction):
            return float("nan")
        return small(words[:point]) + float("0." + "".join(str(_WORDS[w]) for w in fraction))
    if "thousand" in words:
        index = words.index("thousand")
        tail = words[index + 1:]
        if tail[:1] == ["and"]:
            tail = tail[1:]
        return small(words[:index]) * 1000 + small(tail)
    return small(words)


def _unit(value):
    if value is None:
        return None
    word = value.lower()
    if re.fullmatch(r"source[ -]+types?", word):
        return "source_types"
    if word.startswith("mention"):
        return "mentions"
    if word.startswith("source"):
        return "source_files"
    return "unverified_identity"


def _quantities(text):
    """Find whole expressions before group assignment, retaining raw offsets."""
    spans, _ = quoted_spans(text)
    masked = text
    for span in reversed(spans):
        masked = masked[:span["start"]] + " " * (span["end"] - span["start"]) + masked[span["end"]:]
    found = []
    for kind, pattern in (("percentage", _PERCENT), ("ratio", _RATIO), ("count", _COUNT)):
        for match in pattern.finditer(masked):
            start, end = match.span()
            if any(start < row["end"] and row["start"] < end for row in found):
                continue
            if kind == "ratio" and re.sub(r"\s", "", match.group()) == "24/7":
                continue
            found.append({"kind": kind, "start": start, "end": end,
                          "number": _number(match["number"]), "unit": _unit(match["unit"]),
                          "question_scopes": sorted({q.upper() for q in re.findall(r"\bQ\d+\b", match.group(), re.I)}),
                          "denominator": _number(match["denominator"]) if kind in {"ratio", "percentage"} and match["denominator"] else None})
    ordered = sorted(found, key=lambda row: row["start"])
    combined = []
    index = 0
    while index < len(ordered):
        expression = ordered[index]
        if expression["kind"] == "ratio" and index + 1 < len(ordered):
            following = ordered[index + 1]
            # Only an immediately adjacent parenthetical percentage belongs to
            # this ratio. Use the original text so masked quotations cannot
            # create adjacency. Preserve its whole span for count-group checks.
            if (following["kind"] == "percentage" and
                    re.fullmatch(r"\s*\(\s*", text[expression["end"]:following["start"]]) and
                    (closing := re.match(r"\s*\)", text[following["end"]:]))):
                expression["attached_percentage"] = following
                expression["end"] = following["end"] + closing.end()
                index += 1
        combined.append(expression)
        index += 1
    return combined


def _expression_errors(expression, stats):
    unit = expression["unit"]
    if unit == "unverified_identity":
        return ["customer/account/person counts require a verified identity map; use source files."]
    if unit == "source_types":
        return ["source-type counts cannot be derived from source-file membership."]
    available = [s for s in stats if s.get("unit") in {"mentions", "source_files"}]
    scopes = expression["question_scopes"]
    if scopes:
        if len(scopes) != 1 or not any(s.get("scope") == scopes[0] for s in available):
            return ["question qualifier does not match the referenced statistic's scope."]
        available = [s for s in available if s.get("scope") == scopes[0]]
    if unit is None:
        if len({s["unit"] for s in available}) != 1:
            return ["a percentage or ratio needs an explicit unambiguous mention/source-file unit."]
    else:
        available = [s for s in available if s["unit"] == unit]
    kind = expression["kind"]
    if kind == "percentage":
        matches = [s for s in available if s["percentage"] == expression["number"] and
                   (expression["denominator"] is None or s["denominator"] == expression["denominator"])]
    else:
        matches = [s for s in available if s["numerator"] == expression["number"] and
                   (kind != "ratio" or s["denominator"] == expression["denominator"])]
    if matches:
        if "attached_percentage" in expression:
            # A different statistic cannot lend this ratio its percentage,
            # unit, denominator or question scope, even in multi-claim prose.
            return _expression_errors(expression["attached_percentage"], matches)
        return []
    return [{"count": "explicit count does not match its evidence statistic and unit.",
             "ratio": "ratio does not match its evidence numerator/denominator and unit.",
             "percentage": "percentage does not match its evidence statistic and unit."}[kind]]


def _locations(text, substring):
    result, start = [], 0
    while substring and (index := text.find(substring, start)) != -1:
        result.append((index, index + len(substring)))
        start = index + 1
    return result


def _validate(text, defaults, bindings):
    hard, editorial = [], []
    for expression in _quantities(text):
        overlapping = [b for b in bindings if b["start"] < expression["end"] and expression["start"] < b["end"]]
        if overlapping and (len(overlapping) != 1 or overlapping[0]["start"] > expression["start"] or
                            overlapping[0]["end"] < expression["end"]):
            hard.append("count group must contain the complete quantitative expression, including its unit, denominator and attached percentage.")
            continue
        stats = overlapping[0]["stats"] if overlapping else defaults
        editorial.extend(_expression_errors(expression, stats))
    return hard, editorial


def validate_quantities(text, stats):
    """Return quantity errors; bound group statistics never become defaults.

    Legacy unbound statistics remain accepted. Derived bound statistics apply
    only where their complete, unique original clause is reused verbatim. This
    preserves the count basis when accepted claims reach executive prose.
    """
    if not isinstance(text, str):
        return ["quantitative prose must be text."]
    defaults = [s for s in stats if not s.get("binding")]
    grouped = {}
    for stat in stats:
        binding = stat.get("binding")
        if binding:
            key = (stat.get("claim_id"), binding["field"], binding["text"], stat.get("basis"), tuple(stat.get("mention_ids", [])))
            grouped.setdefault(key, []).append(stat)
    bindings = []
    for key, rows in grouped.items():
        locations = _locations(text, key[2])
        if len(locations) == 1:
            start, end = locations[0]
            # The same accepted clause may have separate text/scope bindings.
            prior = next((b for b in bindings if (b["start"], b["end"]) == (start, end)), None)
            if prior:
                def meaning(values):
                    return sorted(json.dumps({k: s[k] for k in ("unit", "basis", "numerator_ids", "denominator_ids", "percentage")}, sort_keys=True) for s in values)
                if meaning(prior["stats"]) != meaning(rows):
                    return ["reused clause has ambiguous count bindings."]
            else:
                bindings.append({"start": start, "end": end, "stats": rows})
    hard, editorial = _validate(text, defaults, bindings)
    return hard + editorial


def _ids(value, *, nonempty=False):
    return (isinstance(value, list) and (bool(value) or not nonempty) and
            all(isinstance(mid, str) and mid for mid in value) and len(value) == len(set(value)))


def _statistics(claim_id, question_id, mentions, selected, basis, binding=None):
    mids = sorted(selected)
    payload = {"binding": binding, "basis": basis, "mention_ids": mids}
    group = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prefix = f"{claim_id}:group:{group}" if binding else f"{claim_id}:support"
    stats = []
    for unit, suffix, key in (("mentions", "mentions", "mention_id"), ("source_files", "sources", "source_id")):
        numerator = sorted({mentions[mid][key] for mid in mids})
        denominator = sorted({m[key] for m in mentions.values()})
        stats.append({"stat_ref": f"{prefix}:{suffix}", "unit": unit,
                      "numerator_ids": numerator, "denominator_ids": denominator,
                      "numerator": len(numerator), "denominator": len(denominator),
                      "percentage": round(len(numerator) / len(denominator) * 100) if denominator else 0,
                      "scope": question_id, "claim_id": claim_id, "basis": basis,
                      "binding": dict(binding) if binding else None, "mention_ids": mids[:]})
    return stats


def source_observation_fields(mention):
    """Code-owned projection of one retained source case, never free prose."""
    required = ("mention_id", "source_id", "theme_id", "mention")
    if not isinstance(mention, dict) or not all(isinstance(mention.get(k), str) and mention[k].strip() for k in required):
        raise ValueError("Source observation needs a complete retained mention.")
    return {"type": "observation", "text": mention["mention"],
            "theme_ids": [mention["theme_id"]], "support_mention_ids": [mention["mention_id"]],
            "counterevidence_ids": [], "stat_refs": [mention["theme_id"] + ":sources"],
            "scope": "Reported in this source; broader prevalence is not established.",
            "source_observation": {"mention_id": mention["mention_id"]}}


def _source_observation_errors(claim, mentions):
    marker = claim["source_observation"]
    if (not isinstance(marker, dict) or set(marker) != {"mention_id"} or
            not isinstance(marker["mention_id"], str) or marker["mention_id"] not in mentions):
        return ["source_observation must identify exactly one retained mention in this question."]
    try:
        expected = source_observation_fields(mentions[marker["mention_id"]])
    except ValueError as exc:
        return [str(exc)]
    errors = [f"source_observation {field} must equal the exact retained source projection."
              for field, value in expected.items() if claim.get(field) != value]
    if "count_groups" in claim:
        errors.append("source_observation cannot add count groups to a source-local case.")
    return errors


def assess_claim_quantities(claim, question):
    """Return hard reference/binding errors, local prose errors, and exact stats."""
    hard, editorial, stats = [], [], []
    result = {"hard_errors": hard, "editorial_errors": editorial, "stats": stats}
    if not isinstance(claim, dict) or not isinstance(question, dict):
        hard.append("claim and question must be objects.")
        return result
    qid, cid = question.get("question_id"), claim.get("claim_id")
    rows, themes, locked = question.get("mentions"), question.get("themes"), question.get("stats")
    if (not isinstance(qid, str) or not isinstance(cid, str) or not re.fullmatch(re.escape(qid) + r"_C[1-9]\d*", cid)
            or not isinstance(rows, list) or not isinstance(themes, list) or not isinstance(locked, list)):
        hard.append("malformed claim identity or locked question inventory.")
        return result
    if any(not isinstance(m, dict) or not all(isinstance(m.get(k), str) and m[k] for k in ("mention_id", "source_id", "theme_id")) for m in rows):
        hard.append("malformed locked mention membership.")
        return result
    mentions = {m["mention_id"]: m for m in rows}
    if len(mentions) != len(rows):
        hard.append("duplicate locked mention IDs.")
    if any(not isinstance(t, dict) or not isinstance(t.get("theme_id"), str) for t in themes):
        hard.append("malformed locked themes.")
        return result
    theme_ids = {t["theme_id"] for t in themes}
    if len(theme_ids) != len(themes) or any(m["theme_id"] not in theme_ids for m in rows):
        hard.append("locked theme membership is inconsistent.")
    for field in ("support_mention_ids", "counterevidence_ids", "theme_ids", "stat_refs"):
        if not _ids(claim.get(field), nonempty=field in {"support_mention_ids", "theme_ids"}):
            hard.append(f"{field} must contain distinct nonempty IDs.")
    for field in ("text", "scope"):
        if not isinstance(claim.get(field), str) or not claim[field].strip():
            hard.append(f"{field} must be nonempty text.")
    if hard:
        return result
    support, counter = set(claim["support_mention_ids"]), set(claim["counterevidence_ids"])
    if not (support | counter) <= set(mentions):
        hard.append("support/counterevidence IDs must belong to this locked question.")
    if support & counter:
        hard.append("support and counterevidence IDs must be disjoint.")
    if not set(claim["theme_ids"]) <= theme_ids:
        hard.append("claim theme IDs must belong to this question.")
    if any(mentions[mid]["theme_id"] not in claim["theme_ids"] for mid in support if mid in mentions):
        hard.append("support must belong to the claim's declared themes.")
    by_ref = {}
    for stat in locked:
        if not isinstance(stat, dict) or not isinstance(stat.get("stat_ref"), str) or stat["stat_ref"] in by_ref:
            hard.append("malformed or duplicate locked statistic references.")
            continue
        ref = stat["stat_ref"]
        by_ref[ref] = stat
        owner, separator, suffix = ref.partition(":")
        unit = {"mentions": "mentions", "sources": "source_files"}.get(suffix)
        selected = set(mentions) if owner == qid else {mid for mid, m in mentions.items() if m["theme_id"] == owner}
        if not separator or owner not in {qid, *theme_ids} or unit is None:
            hard.append("locked statistic reference has an unknown scope or unit.")
            continue
        expected = next(s for s in _statistics(cid, qid, mentions, selected, "support") if s["unit"] == unit)
        if (any(type(stat.get(k)) is not int for k in ("numerator", "denominator", "percentage")) or
                any(stat.get(k) != expected[k] for k in ("unit", "numerator_ids", "denominator_ids", "numerator", "denominator", "percentage", "scope"))):
            hard.append("locked statistic differs from exact question membership.")
    if any(ref not in by_ref or ref.split(":")[0] not in {qid, *claim["theme_ids"]} for ref in claim["stat_refs"]):
        hard.append("unknown, stale, or cross-scope statistic reference.")
    if hard:
        return result
    defaults = _statistics(cid, qid, mentions, support, "support")
    stats.extend(defaults)
    if "source_observation" in claim:
        hard.extend(_source_observation_errors(claim, mentions))
        if hard:
            return result
        # Business quantities describe this exact source's reported situation.
        # Dataset units still require the ordinary exact support arithmetic;
        # copying a case never grants permission to invent source/mention totals.
        for expression in _quantities(claim["text"]):
            if expression["unit"] in {"source_files", "mentions", "source_types"}:
                editorial.extend("text: " + e for e in _expression_errors(expression, defaults))
        errors, prose = _validate(claim["scope"], defaults, [])
        hard.extend("scope: " + e for e in errors)
        editorial.extend("scope: " + e for e in prose)
        return result
    groups = claim.get("count_groups", [])
    if not isinstance(groups, list):
        hard.append("count_groups must be an array.")
        return result
    bindings = {"text": [], "scope": []}
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"field", "text", "basis", "mention_ids"}:
            hard.append("count group fields must be field, text, basis, mention_ids.")
            continue
        field, text, basis = group["field"], group["text"], group["basis"]
        if (not isinstance(field, str) or field not in bindings or not isinstance(text, str) or not text.strip()
                or not isinstance(basis, str) or basis not in {"support", "contrast"} or not _ids(group["mention_ids"], nonempty=True)):
            hard.append("count group needs a text/scope field, support/contrast basis, text and distinct mention IDs.")
            continue
        selected = set(group["mention_ids"])
        if not selected <= (support if basis == "support" else support | counter):
            hard.append("count group IDs are outside its declared claim evidence basis.")
            continue
        locations = _locations(claim[field], text)
        if len(locations) != 1:
            hard.append("count group text must occur exactly once in its declared field.")
            continue
        start, end = locations[0]
        if any(start < b["end"] and b["start"] < end for b in bindings[field]):
            hard.append("count group text spans may not overlap.")
            continue
        derived = _statistics(cid, qid, mentions, selected, basis, {"field": field, "text": text})
        bindings[field].append({"start": start, "end": end, "stats": derived})
        stats.extend(derived)
    for field in bindings:
        errors, prose = _validate(claim[field], defaults, bindings[field])
        hard.extend(f"{field}: {e}" for e in errors)
        editorial.extend(f"{field}: {e}" for e in prose)
    return result


def claim_support_stats(claim, question):
    """Return support-only aggregate stats; never pool in bound contrast totals."""
    assessment = assess_claim_quantities(claim, question)
    if assessment["hard_errors"]:
        raise ValueError("; ".join(assessment["hard_errors"]))
    return [s for s in assessment["stats"] if s["binding"] is None]

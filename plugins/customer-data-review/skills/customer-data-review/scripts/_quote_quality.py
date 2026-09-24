"""Heuristics for detecting low-quality quotes (CSV rows, markdown artifacts)."""

import re

# Ticket-ID prefix: WL-021, TICKET-123, etc.
_TICKET_PREFIX_RE = re.compile(r"^[A-Z]{2,}-\d+,")

# ISO date field: 2025-08-02
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Bare number: 42, 3.14
_BARE_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")

# All-caps code: HIGH, OPEN, ACTIVE
_ALL_CAPS_RE = re.compile(r"^[A-Z_]{2,}$")

# Markdown bold: **text** or __text__
_MARKDOWN_BOLD_RE = re.compile(r"\*\*[^*]+\*\*|__[^_]+__")


def looks_like_csv_row(text: str) -> bool:
    """Heuristic: text looks like a CSV row rather than natural language.

    Two patterns:
    1. Starts with a ticket-ID prefix (e.g., WL-021,2025-08-02,...)
    2. Has 4+ comma-separated fields AND at least 2 fields are structured
       data (dates, bare numbers, or all-caps codes).

    Requires 2+ structured fields to avoid false positives on natural
    language containing industry acronyms like CRM, ERP, GDPR.
    """
    if not text:
        return False

    # Pattern 1: ticket-ID prefix
    if _TICKET_PREFIX_RE.match(text):
        return True

    # Pattern 2: multiple structured fields
    fields = text.split(",")
    if len(fields) < 4:
        return False

    has_date_or_number = False
    caps_count = 0
    for f in fields:
        f = f.strip()
        if _ISO_DATE_RE.match(f):
            has_date_or_number = True
        elif _BARE_NUMBER_RE.match(f):
            has_date_or_number = True
        elif _ALL_CAPS_RE.match(f):
            caps_count += 1

    # Require at least one date or bare-number field. All-caps codes alone
    # are not sufficient — acronym lists (CRM, ERP, GDPR) are common in
    # natural customer quotes.
    return has_date_or_number


# ---------------------------------------------------------------------------
# Low-signal quote detection
# ---------------------------------------------------------------------------

# Role/title words that signal a self-introduction when near "I am" / "I'm"
_ROLE_TITLES = frozenset({
    "manager", "director", "head", "lead", "coordinator", "trainer",
    "coach", "owner", "founder", "president", "officer", "supervisor",
    "specialist", "instructor", "administrator", "analyst", "vp",
    "ceo", "cfo", "coo", "cto",
})

# Self-introduction: "I am the [Role]" / "I'm a [Role]" / "So I am the [Role]"
# Captures up to 15 words after "I am/I'm" to find a role title.
_SELF_INTRO_RE = re.compile(
    r"(?:^|(?<=[.!?;]\s))"      # start of text or sentence boundary
    r"(?:so\s+|and\s+|well\s+)?"  # optional discourse marker
    r"i(?:'m|\s+am)\s+"         # "I am" or "I'm"
    r"(?:the\s+|a\s+|an\s+)?"  # optional article
    r"((?:\w+\s+){0,8})"       # up to 8 words before role title
    , re.IGNORECASE
)

# Meta-commentary about the call/interview itself
_META_PHRASES = [
    "take a call",
    "take this call",
    "on my laptop",
    "on my phone",
    "on my computer",
    "thanks for having",
    "thanks for taking",
    "thanks for calling",
    "glad to be here",
    "glad we could",
    "nice to meet",
    "nice to talk",
    "nice to speak",
    "before we get started",
    "before we begin",
    "before we dive in",
    "good question",
    "great question",
    "that's a great question",
    "appreciate you taking the time",
]

# Embedded signal: if the quote contains numbers, dollar signs, or
# substantive claims (opinion/experience verbs after a conjunction)
# despite introductory framing, it has analytical value.
_HAS_SIGNAL_RE = re.compile(r"\d{2,}|\$\d")

# Business terms that indicate substantive content — broader than
# _CLAIM_INDICATORS (no syntax requirement). Conservative: excludes
# generic words (problem, issue, difficult, team, data) that appear
# in pleasantries and meta-commentary.
_BUSINESS_TERMS_RE = re.compile(
    r"\b(?:crash|broken|downtime|outage|"               # product failures
    r"churn|revenue|budget|pricing|subscription|"        # business ops
    r"schedul|workflow|automat|onboard|migrat|integrat|" # process
    r"evaluat|implement|audit|competitor|alternative|vendor|"  # evaluation
    r"hir|retain|cancel|renew|"                          # people ops
    r"frustrat|nightmare|chaos|inefficien|bottleneck|compliance|deadline"  # pain
    r")\w*\b",
    re.IGNORECASE,
)

# Claim indicators: opinion, experience, or problem language after a
# conjunction + subject. Broader than original: includes so/yet/because
# and they/my/this/that subjects.
_CLAIM_INDICATORS = re.compile(
    r"\b(?:and|but|so|yet|because|since|where|then)\s+"
    r"(?:the|it|we|our|i|they|my|this|that|things)\b.*?"
    r"(?:hate|love|need|want|struggle|frustrat|nightmare|chaos|terrible|"
    r"broken|painful|amazing|difficult|challenge|problem|issue|spend|waste|"
    r"save|cost|switch|evaluat|looking for|trying to|dealing with)",
    re.IGNORECASE,
)

# Words that follow "I am [article]?" but indicate the sentence is NOT
# a self-introduction — the speaker is making a statement, not stating
# their role. Checked against the first word of the capture group.
_NON_INTRO_WORDS = frozenset({
    # Adjectives/adverbs: "I am pretty sure...", "I am not..."
    "pretty", "really", "just", "not", "also", "still", "already",
    "never", "always", "sure", "certain", "confident", "convinced",
    # Present participles: "I am going to...", "I am trying to..."
    "going", "trying", "telling", "hoping", "looking", "thinking",
    "feeling", "working", "dealing",
    # Emotional adjectives: "I am glad the manager..."
    "afraid", "glad", "happy", "sorry",
    # Pronouns after "I am the": "I am the one who..."
    "one", "person", "only", "first", "last",
})


def _has_business_signal(text: str) -> bool:
    """Check whether text contains substantive business content.

    Used to rescue quotes that match a low-signal pattern (meta-phrase or
    self-intro) but also contain genuine analytical content.
    """
    if _HAS_SIGNAL_RE.search(text):
        return True
    if _BUSINESS_TERMS_RE.search(text):
        return True
    if _CLAIM_INDICATORS.search(text):
        return True
    return False


def is_low_signal_quote(text: str) -> bool:
    """Heuristic: quote is a self-introduction or meta-commentary with no
    business insight.

    Returns False (not low-signal) when the quote contains embedded
    analytical signal (numbers, dollar amounts, business terms) even if
    it has introductory framing — e.g., "I manage 70 instructors across
    3 locations" is a self-intro AND a data point.
    """
    if not text or len(text) < 10:
        return False

    lower = text.lower()

    # Check meta-commentary phrases — rescue if quote has business signal
    for phrase in _META_PHRASES:
        if phrase in lower:
            if _has_business_signal(text):
                return False  # meta-phrase is incidental
            return True

    # Check self-introduction pattern
    match = _SELF_INTRO_RE.search(lower)
    if match:
        window = match.group(1) if match.group(1) else ""
        window_words = [w.strip(".,;:()\"'") for w in window.split()]

        # Guard: if the first content word is a non-intro indicator,
        # this is a statement, not a self-introduction.
        if window_words and window_words[0] in _NON_INTRO_WORDS:
            return False

        # Check capture group only for role titles (no after_match scan)
        words = {w for w in window_words}
        if words & _ROLE_TITLES:
            # Found a role title — but does the quote also have signal?
            if _has_business_signal(text):
                return False
            return True

    return False


def has_markdown_formatting(text: str) -> bool:
    """Heuristic: text contains markdown bold formatting artifacts.

    Detects **bold** and __underline__ patterns. Does NOT flag single
    asterisk *italic* — too many false positives in natural text.
    """
    if not text:
        return False
    return bool(_MARKDOWN_BOLD_RE.search(text))

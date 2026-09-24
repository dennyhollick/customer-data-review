"""Single source of truth for all enums and patterns in the v2 pipeline."""

import re

SOURCE_TYPES = frozenset({
    "sales_call",
    "renewal_call",
    "churn_interview",
    "cs_call",
    "internal_meeting",
    "nps_survey",
    "csat_survey",
    "general_survey",
    "review",
    "support_ticket",
    "feature_request",
    "competitive_intel",
    "advisory",
    "community",
    "internal_notes",
    "other",
})

LIFECYCLES = frozenset({"prospect", "active", "churned", "unknown"})

SENTIMENTS = frozenset({"positive", "negative", "mixed", "neutral"})

THEME_TYPES = frozenset({
    "complaint",
    "praise",
    "feature_request",
    "competitive",
    "behavioral",
    "risk",
    "other",
})

QUALITIES = frozenset({"good", "bad"})

INTERACTION_TYPES = frozenset({
    "single",
    "survey_responses",
    "reviews",
    "tickets",
    "deals",
    "requests",
    "other_multi",
})

UNKNOWN_SEGMENT = "unknown"

DISPLAY_CATEGORIES = {
    "sales_call": "Calls",
    "renewal_call": "Calls",
    "churn_interview": "Calls",
    "cs_call": "Calls",
    "internal_meeting": "Calls",
    "nps_survey": "Surveys",
    "csat_survey": "Surveys",
    "general_survey": "Surveys",
    "review": "Reviews",
    "support_ticket": "Support",
    "feature_request": "Feature Requests",
    "competitive_intel": "Competitive Intel",
    "advisory": "Advisory",
    "community": "Community",
    "internal_notes": "Notes",
    "other": "Other",
}

ADJECTIVE_THRESHOLDS = {
    "dominant": (30, 101),
    "primary": (30, 101),
    "leading": (30, 101),
    "top": (30, 101),
    "significant": (15, 30),
    "common": (15, 30),
    "frequent": (15, 30),
    "widespread": (15, 30),
    "notable": (5, 15),
    "moderate": (5, 15),
    "emerging": (5, 15),
    "minor": (0, 5),
    "limited": (0, 5),
    "rare": (0, 5),
}

MENTION_ID_PATTERN = r"^[a-zA-Z0-9_]+_Q\d+_\d+$"
QUESTION_ID_PATTERN = r"^Q\d+$"
THEME_ID_PATTERN = r"^Q\d+_T\d+$"

MENTION_ID_RE = re.compile(MENTION_ID_PATTERN)
QUESTION_ID_RE = re.compile(QUESTION_ID_PATTERN)
THEME_ID_RE = re.compile(THEME_ID_PATTERN)

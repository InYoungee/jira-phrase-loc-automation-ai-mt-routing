import pandas as pd
from dataclasses import dataclass
import math


# ============================================================
# CONTENT ANALYSIS
# ============================================================

@dataclass
class ContentFeatures:
    string_type: str
    priority: str
    has_variables: bool
    notes: str


def analyze_row(row, column_mapping):
    """Extract routing-relevant features from one source row."""
    raw_priority = row.get("Priority", "")
    priority = "" if (isinstance(raw_priority, float) and math.isnan(raw_priority)) else str(raw_priority).strip()

    return ContentFeatures(
        string_type=str(row.get("String_Type", "")).strip(),
        priority=priority,
        has_variables=bool(str(row.get("Variables", "")).strip()),
        notes=str(row.get("Notes", "")).strip().lower(),
    )


# ============================================================
# ROUTING ENGINE
# ============================================================

# Content types considered safe defaults for AI/MT.
# Short, formulaic, low creative-risk categories.
AI_SUITABLE_TYPES = {
    "ui label", "ui",
    "system message", "system",
    "push notification",
    "combat",
    "npc",
    "tutorial",
}

# Notes substrings that force human review regardless of type —
# these represent explicit signals a linguist already flagged.
FORCE_HUMAN_NOTES = [
    "lqa pass required",
    "voice-adjacent",
    "confirm against style guide",
    "naming convention must match",
]


def route_row(features: ContentFeatures):
    """Decide AI-suitable vs human-required for one row.

    Returns (decision, reason) — decision is 'ai_suitable' or 'human_required'.
    """
    string_type_lower = features.string_type.lower()

    # Hard overrides — explicit human-review flags always win,
    # regardless of content type.
    for flag in FORCE_HUMAN_NOTES:
        if flag in features.notes:
            return "human_required", f"flagged in notes: '{flag}'"

    # Dialogue always requires human review — narrative/character-voice
    # content is out of scope for AI/MT regardless of priority.
    if string_type_lower == "dialogue":
        return "human_required", "dialogue always requires human review"

    # Default AI-suitable categories.
    if string_type_lower in AI_SUITABLE_TYPES:
        return "ai_suitable", f"content type '{features.string_type}' is formulaic/low-risk"

    # Item/Achievement names are usually short, but flagged naming-convention
    # rows already got caught above — remaining ones are fine for AI.
    if string_type_lower == "item/achievement name":
        return "ai_suitable", "short label content"

    # Safe default: unknown/uncovered types go to human review rather
    # than being silently assumed safe.
    return "human_required", f"no AI-suitable rule matched for type '{features.string_type}'"


# ============================================================
# FILE-LEVEL ROUTING
# ============================================================

def route_file(file_path, column_mapping):
    """
    Read the source file and produce a routing decision + reason
    for every row.

    Returns a DataFrame with the original columns plus:
        - ai_mt_route: 'ai_suitable' or 'human_required'
        - ai_mt_reason: human-readable justification
    """
    df = pd.read_excel(file_path)

    routes = []
    reasons = []

    for _, row in df.iterrows():
        features = analyze_row(row, column_mapping)
        decision, reason = route_row(features)
        routes.append(decision)
        reasons.append(reason)

    df["ai_mt_route"] = routes
    df["ai_mt_reason"] = reasons

    return df


def print_routing_summary(df):
    """Quick console summary — how many rows went each way, and why."""
    print("\n========== AI/MT ROUTING SUMMARY ==========")
    counts = df["ai_mt_route"].value_counts()
    for route, count in counts.items():
        print(f"{route}: {count} rows ({count / len(df):.0%})")

    print("\n--- Sample reasons ---")
    for route in ["ai_suitable", "human_required"]:
        sample = df[df["ai_mt_route"] == route].head(3)
        for _, row in sample.iterrows():
            print(f"  [{route}] {row.get('String_ID', '?')}: {row['ai_mt_reason']}")


if __name__ == "__main__":
    # Quick standalone test against one of your existing sample files
    test_file = "downloads/NovaFrontier_source_strings.xlsx"
    column_mapping = {}  # not yet needed for rule-based routing

    result_df = route_file(test_file, column_mapping)
    print_routing_summary(result_df)
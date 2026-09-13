"""
AI/MT Layer Orchestrator — runs the full hybrid pipeline
(routing → DeepL draft → Claude refinement → quality gate) and
produces a Phrase-ready merged file plus usage stats for cost logging.
"""

import os

from ai_mt_router import route_file
from mt_draft import draft_translations_with_deepl, find_column_by_prefix
from mt_refine import refine_ai_suitable_rows, enforce_quality_gates, resolve_glossary_path

def estimate_deepl_characters(df, source_column):
    ai_rows = df[df["ai_mt_route"] == "ai_suitable"]
    return int(ai_rows[source_column].fillna("").astype(str).str.len().sum())


def estimate_claude_tokens(df, source_column):
    ai_rows = df[df["ai_mt_route"] == "ai_suitable"]
    input_chars = (
        ai_rows[source_column].fillna("").astype(str).str.len()
        + ai_rows["MT_Draft"].fillna("").astype(str).str.len()
        + 150
    ).sum()
    output_chars = ai_rows["LLM_Refined"].fillna("").astype(str).str.len().sum()
    return {"input_tokens": int(input_chars / 4), "output_tokens": int(output_chars / 4)}

def run_ai_mt_layer(file_path, column_mapping, glossary_path=None):
    """
    Runs the full AI/MT pipeline on a downloaded source file.
    Returns (enriched_df, usage_stats, output_file_path).
    """
    source_lang = column_mapping.get("source_lang")
    target_langs = column_mapping.get("target_langs")

    if not source_lang or not target_langs:
        raise ValueError(f"Could not detect language pair from column_mapping: {column_mapping}")

    target_lang = target_langs[0]  # only process the first target language for now

    if glossary_path is None:
        glossary_path = resolve_glossary_path(file_path)

    df = route_file(file_path, column_mapping)
    df = draft_translations_with_deepl(df, source_lang=source_lang, target_lang=target_lang)
    df = refine_ai_suitable_rows(df, glossary_path=glossary_path)
    df = enforce_quality_gates(df)

    source_column = find_column_by_prefix(df, "source")
    target_column = (
        find_column_by_prefix(df, f"target_{target_lang}")
        or find_column_by_prefix(df, "target")
    )

    if not source_column or not target_column:
        raise ValueError(
            f"Could not resolve source/target columns in {file_path}. "
            f"source_column={source_column}, target_column={target_column}"
        )

    ai_approved = df["ai_mt_route"] == "ai_suitable"

    df[target_column] = df[target_column].astype(object)
    df[target_column] = df[target_column].where(df[target_column].notna(), "")
    df.loc[ai_approved, target_column] = df.loc[ai_approved, "LLM_Refined"]

    usage_stats = {
        "total_rows": len(df),
        "ai_suitable_count": int(ai_approved.sum()),
        "human_required_count": int((~ai_approved).sum()),
        "deepl_characters": estimate_deepl_characters(df, source_column),
        **estimate_claude_tokens(df, source_column),
    }

    base, ext = os.path.splitext(file_path)
    output_file_path = f"{base}_ai_processed{ext}"
    df.to_excel(output_file_path, index=False)

    return df, usage_stats, output_file_path
import os
import anthropic
from dotenv import load_dotenv
import math
import csv
from mt_draft import find_column_by_prefix
import re

load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def resolve_glossary_path(file_path, glossary_dir="glossaries"):
    """Picks a project-specific glossary based on the source filename.
    Returns None if no matching glossary exists — projects without
    their own glossary simply proceed without one."""
    base = os.path.basename(file_path)

    # Strip Jira's auto-appended duplicate suffix (e.g. " (1)", " (2)")
    base = re.sub(r'\s*\(\d+\)(\.xlsx)$', r'\1', base, flags=re.IGNORECASE)

    name, _ext = os.path.splitext(base)
    name = re.sub(r'_ai_processed$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'_source_strings$', '', name, flags=re.IGNORECASE)  # optional — only strips if present
    project_key = name.strip().lower().replace(" ", "_")

    candidate = os.path.join(glossary_dir, f"{project_key}.csv")
    if os.path.exists(candidate):
        print(f"Using project glossary: {candidate}")
        return candidate

    print(f"No project-specific glossary for '{project_key}' — proceeding without one.")
    return None

def load_glossary(glossary_path):
	glossary = []
	with open(glossary_path, newline='', encoding='utf-8') as f:
		reader = csv.DictReader(f)
		for row in reader:
			glossary.append({
				"source_term": row["source_term"].strip(),
				"target_term": row["target_term"].strip(),
				"notes": row.get("notes", "").strip(),
			})
	return glossary


def match_glossary_terms(source_text, glossary):
	"""Finds glossary terms that appear as substrings in the source text."""
	return [g for g in glossary if g["source_term"] and g["source_term"] in source_text]
def extract_text(response):
	"""Finds the text block in a response regardless of what other
	block types (thinking, tool_use, etc.) might precede it."""
	for block in response.content:
		if block.type == "text":
			return block.text.strip()

	raise ValueError(
		f"No text block found. stop_reason={response.stop_reason}, "
		f"block types present: {[b.type for b in response.content]}"
	)

def has_value(x):
	"""True only for a real, non-empty value — treats pandas' NaN
	(from empty Excel cells) as missing, not as a truthy string."""
	if x is None:
		return False
	if isinstance(x, float) and math.isnan(x):
		return False
	return str(x).strip() != ""
def refine_with_claude(source_text, mt_draft, context_screen, char_limit, notes, variables=None, glossary_matches=None):
	limit_instruction = f"Stay within {int(char_limit)} characters." if has_value(char_limit) else "No strict character limit."
	notes_instruction = f"Additional context: {notes}" if has_value(notes) else ""
	variables_instruction = (
		f"\nThis string contains placeholder variable(s): {variables}. "
		f"Preserve them EXACTLY as written, character-for-character — do not translate, "
		f"alter, or add/remove spacing inside them."
		if has_value(variables) else ""
	)

	glossary_instruction = ""
	if glossary_matches:
		terms_list = "\n".join(f"- {m['source_term']} → {m['target_term']}" for m in glossary_matches)
		glossary_instruction = f"\nUse these exact approved terms wherever they apply:\n{terms_list}"

	prompt = f"""You are localizing a mobile game from Korean to English.

Source (Korean): {source_text}
Machine translation draft: {mt_draft}
UI context: {context_screen}
{limit_instruction}
{notes_instruction}
{variables_instruction}
{glossary_instruction}

Adapt the draft into natural, concise English suited to mobile game UI —
prefer short, action-oriented phrasing for buttons/menu items over literal
or formal noun phrases. Return ONLY the final string, nothing else."""

	return call_claude_with_retry(prompt)  # ONLY line making the API call now — no separate client.messages.create() above this

import time

def call_claude_with_retry(prompt, max_retries=3):
	"""Retries on transient overload (529) with exponential backoff.
	Anthropic's own guidance is to retry these, since they're temporary
	capacity signals, not errors caused by the request itself."""
	for attempt in range(1, max_retries + 1):
		try:
			response = client.messages.create(
				model="claude-sonnet-5",
				max_tokens=300,
				thinking={"type": "disabled"},
				messages=[{"role": "user", "content": prompt}],
			)
			return extract_text(response)
		except Exception as e:
			is_overloaded = "overloaded" in str(e).lower() or "529" in str(e)
			if is_overloaded and attempt < max_retries:
				wait = 2 ** attempt  # 2s, 4s, then 8s
				print(f"⚠️ Claude API overloaded (attempt {attempt}/{max_retries}) — retrying in {wait}s...")
				time.sleep(wait)
			else:
				raise  # non-transient error, or retries exhausted — surface it

def refine_ai_suitable_rows(df, route_column="ai_mt_route", glossary_path=None):
    source_column = find_column_by_prefix(df, "source")

    df["LLM_Refined"] = ""
    df["Within_Limit"] = None

    glossary = load_glossary(glossary_path) if glossary_path and os.path.exists(glossary_path) else []

    ai_rows = df[df[route_column] == "ai_suitable"]
    print(f"\n========== CLAUDE REFINEMENT =========={len(ai_rows)} strings")
    if glossary:
       print(f"Loaded {len(glossary)} glossary terms from {glossary_path}")
    elif glossary_path is None:
       print("No glossary applied for this project.")

    for idx, row in ai_rows.iterrows():
       source_text = row.get(source_column, "")
       matches = match_glossary_terms(source_text, glossary)

       refined = refine_with_claude(
          source_text=source_text,
          mt_draft=row.get("MT_Draft", ""),
          context_screen=row.get("Context_Screen", ""),
          char_limit=row.get("Char_Limit"),
          notes=row.get("Notes"),
          variables=row.get("Variables"),
          glossary_matches=matches,
       )
       df.at[idx, "LLM_Refined"] = refined

       char_limit = row.get("Char_Limit")
       if has_value(char_limit):
          df.at[idx, "Within_Limit"] = len(refined) <= int(char_limit)

    return df

def enforce_quality_gates(df):
	"""
	Post-refinement quality gate: any ai_suitable row that still
	violates its character limit after Claude's refinement gets
	escalated back to human_required, rather than shipped as-is.
	"""
	violations = df["Within_Limit"] == False

	if violations.any():
		count = violations.sum()
		print(f"\n⚠️ {count} row(s) still exceed character limit after refinement — escalating to human review")

		df.loc[violations, "ai_mt_route"] = "human_required"
		df.loc[violations, "ai_mt_reason"] = "AI draft exceeded character limit after refinement"

	return df

def print_refinement_sample(df, n=5):
	print("\n--- MT draft vs Claude refinement ---")
	sample = df[df["ai_mt_route"] == "ai_suitable"].head(n)
	for _, row in sample.iterrows():
		limit_flag = "" if row["Within_Limit"] in (None, True) else "  ⚠️ OVER LIMIT"
		print(f"  [{row.get('String_ID', '?')}] DeepL: {row['MT_Draft']}  →  Claude: {row['LLM_Refined']}{limit_flag}")


if __name__ == "__main__":
	from ai_mt_router import route_file
	from mt_draft import draft_translations_with_deepl

	test_file = "downloads/NovaFrontier_source_strings.xlsx"
	df = route_file(test_file, column_mapping={})
	df = draft_translations_with_deepl(df, source_lang="ko", target_lang="en")
	df = refine_ai_suitable_rows(df)
	df = enforce_quality_gates(df)
	print_refinement_sample(df)
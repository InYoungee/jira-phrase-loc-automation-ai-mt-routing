import os
import deepl
from dotenv import load_dotenv

load_dotenv()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")

def get_deepl_translator():
	if not DEEPL_API_KEY:
		raise ValueError("Missing DEEPL_API_KEY in .env")
	return deepl.Translator(DEEPL_API_KEY)


# DeepL requires specific codes — plain "EN" is rejected, needs a regional variant
DEEPL_LANG_MAP = {
	"ko": "KO",
	"en": "EN-US",
	"ja": "JA",
	"zh": "ZH",
    "de": "DE",
    "fr": "FR",
    "es": "ES",
    "it": "IT",
}


def find_column_by_prefix(df, prefix):
	"""Finds the actual pandas column name starting with a given prefix,
	case-insensitive — e.g. 'source' matches 'Source_KO'."""
	prefix = prefix.lower()
	for col in df.columns:
		if str(col).lower().startswith(prefix):
			return col
	return None

def draft_translations_with_deepl(df, source_lang, target_lang, route_column="ai_mt_route"):
	"""
	Translates only rows marked 'ai_suitable' via DeepL.
	Adds a new 'MT_Draft' column to the DataFrame; human_required
	rows are left blank in this column, untouched.
	"""
	translator = get_deepl_translator()

	source_column = find_column_by_prefix(df, "source")
	if not source_column:
		raise ValueError("Could not find a Source_ column in this file.")

	deepl_source = source_lang.upper()
	deepl_target = DEEPL_LANG_MAP.get(target_lang.lower(), target_lang.upper())

	df["MT_Draft"] = ""

	ai_rows = df[df[route_column] == "ai_suitable"]
	texts = ai_rows[source_column].fillna("").astype(str).tolist()

	if not texts:
		print("No AI-suitable rows to translate — skipping DeepL call.")
		return df

	print(f"\n========== DEEPL MT DRAFT ==========")
	print(f"{len(texts)} strings | {deepl_source} → {deepl_target}")

	results = translator.translate_text(texts, source_lang=deepl_source, target_lang=deepl_target)
	translations = [r.text for r in results]

	df.loc[ai_rows.index, "MT_Draft"] = translations

	return df


def print_draft_sample(df, n=5):
	"""Quick sanity check — print a few source/draft pairs side by side."""
	sample = df[df["ai_mt_route"] == "ai_suitable"].head(n)
	print("\n--- Sample MT drafts ---")
	source_column = find_column_by_prefix(df, "source")
	for _, row in sample.iterrows():
		print(f"  [{row.get('String_ID', '?')}] {row[source_column]}  →  {row['MT_Draft']}")


if __name__ == "__main__":
	from ai_mt_router import route_file

	test_file = "downloads/NovaFrontier_source_strings.xlsx"
	df = route_file(test_file, column_mapping={})
	df = draft_translations_with_deepl(df, source_lang="ko", target_lang="en")
	print_draft_sample(df)
import json
from dataclasses import dataclass
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

RATES_FILE = Path(__file__).parent / "default_rates.json"

# ============================================================
# LOAD RATE CONFIGURATION
# ============================================================

def load_rates():
    """
    Load localization pricing configuration from default_rates.json.

    Returns:
        dict: Parsed rate configuration.

    Raises:
        FileNotFoundError: If default_rates.json does not exist.
        json.JSONDecodeError: If the JSON file is invalid.
    """

    if not RATES_FILE.exists():
        raise FileNotFoundError(
            f"Rate configuration file not found: {RATES_FILE}"
        )

    with open(RATES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# COST ESTIMATE DATA MODEL
# ============================================================

@dataclass
class CostEstimate:
    """
    Stores the calculated localization cost.
    """

    source_lang: str
    target_langs: list
    word_count: int
    currency: str
    translation_cost: float
    lqa_cost: float
    total_cost: float


# ============================================================
# COST CALCULATION
# ============================================================

def estimate_cost(
    source_lang,
    target_langs,
    word_count,
):
    """
    Estimate localization cost based on source word count
    and target language rates.

    Rates are defined in default_rates.json.

    Args:
        source_lang (str):
            Source language code, e.g. "KO".

        target_langs (list[str]):
            Target language codes, e.g. ["EN", "JA"].

        word_count (int):
            Number of source words.

    Returns:
        CostEstimate:
            Calculated translation, LQA, and total costs.
    """

    # --------------------------------------------------------
    # Load pricing configuration
    # --------------------------------------------------------

    config = load_rates()

    currency = config["currency"]
    rates = config["rates"]
    default_rate = config["default"]

    # --------------------------------------------------------
    # Validate word count
    # --------------------------------------------------------

    if word_count < 0:
        raise ValueError("word_count cannot be negative.")

    # --------------------------------------------------------
    # Normalize target languages
    # --------------------------------------------------------

    if isinstance(target_langs, str):
        target_langs = [target_langs]

    target_langs = [
        lang.upper()
        for lang in target_langs
    ]

    # --------------------------------------------------------
    # Calculate costs
    # --------------------------------------------------------

    translation_cost = 0.0
    lqa_cost = 0.0

    for target_lang in target_langs:
        rate = rates.get(
            target_lang,
            default_rate
        )

        translation_cost += (
            word_count
            * rate["translation"]
        )

        lqa_cost += (
            word_count
            * rate["lqa"]
        )

    total_cost = (
        translation_cost
        + lqa_cost
    )

    # --------------------------------------------------------
    # Return structured result
    # --------------------------------------------------------

    return CostEstimate(
        source_lang=source_lang.upper(),
        target_langs=target_langs,
        word_count=word_count,
        currency=currency,
        translation_cost=translation_cost,
        lqa_cost=lqa_cost,
        total_cost=total_cost,
    )


# ============================================================
# FORMAT COST ESTIMATE
# ============================================================

def format_estimate(estimate):
    """
    Format a CostEstimate object for console output.

    Args:
        estimate (CostEstimate):
            Cost estimate returned by estimate_cost().

    Returns:
        str:
            Human-readable cost summary.
    """

    target_languages = ", ".join(
        estimate.target_langs
    )

    return (
        "\n"
        "========== COST ESTIMATE ==========\n"
        f"Language pair:     "
        f"{estimate.source_lang} → {target_languages}\n"
        f"Source words:      "
        f"{estimate.word_count:,}\n"
        f"Translation:       "
        f"{estimate.currency} "
        f"{estimate.translation_cost:,.2f}\n"
        f"LQA:               "
        f"{estimate.currency} "
        f"{estimate.lqa_cost:,.2f}\n"
        f"Estimated total:   "
        f"{estimate.currency} "
        f"{estimate.total_cost:,.2f}\n"
    )

# ============================================================
# AI/MT COST ESTIMATION
# ============================================================

def estimate_llm_cost(model, input_tokens, output_tokens):
    """
    Estimate Claude API cost for the LLM refinement step.

    Rates are configured in default_rates.json under 'llm_rates' —
    verify against Anthropic's current pricing page before relying
    on this for budget decisions, as rates can change.
    """
    config = load_rates()
    llm_rates = config.get("llm_rates", {})
    rates = llm_rates.get(model)

    if not rates:
        raise ValueError(
            f"No LLM rate configured for model '{model}'. "
            f"Add it under 'llm_rates' in default_rates.json."
        )

    input_cost = (input_tokens / 1_000_000) * rates["input_per_million"]
    output_cost = (output_tokens / 1_000_000) * rates["output_per_million"]

    return input_cost + output_cost


def estimate_mt_cost(characters, engine="deepl"):
    """
    Estimate MT engine cost for the draft-translation step.

    Rates are configured in default_rates.json under 'mt_rates' —
    verify against your DeepL account's current plan/rate.
    """
    config = load_rates()
    mt_rates = config.get("mt_rates", {})
    rate = mt_rates.get(engine)

    if rate is None:
        raise ValueError(
            f"No MT rate configured for engine '{engine}'. "
            f"Add it under 'mt_rates' in default_rates.json."
        )

    return (characters / 1_000_000) * rate["per_million_chars"]


def estimate_ai_mt_cost(deepl_characters, claude_input_tokens, claude_output_tokens, model="claude-sonnet-5"):
    """Combined AI/MT cost for one job — DeepL draft + Claude refinement."""
    mt_cost = estimate_mt_cost(deepl_characters, engine="deepl")
    llm_cost = estimate_llm_cost(model, claude_input_tokens, claude_output_tokens)

    return {
        "mt_cost": mt_cost,
        "llm_cost": llm_cost,
        "total_ai_mt_cost": mt_cost + llm_cost,
    }
# ============================================================
# TEST / STANDALONE EXECUTION
# ============================================================

if __name__ == "__main__":

    print("Testing cost estimator...")

    test_estimate = estimate_cost(
        source_lang="KO",
        target_langs=["EN"],
        word_count=10_000,
    )

    print(
        format_estimate(test_estimate)
    )
import os
import requests
from dotenv import load_dotenv
from cost_estimator import load_rates

load_dotenv()
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
JIRA_URL = os.getenv("JIRA_URL")


def load_alert_thresholds():
    """Reads alert thresholds from default_rates.json, alongside your
    existing translation/LQA/LLM rates."""
    config = load_rates()
    return config.get("alert_thresholds", {"word_count": 5000, "cost_usd": 50})


def should_alert(word_count, cost_usd):
    """Hybrid rule: alert if EITHER threshold is exceeded. If cost_usd
    is None (cost estimate unavailable), only the word-count check applies."""
    thresholds = load_alert_thresholds()
    word_exceeded = word_count >= thresholds.get("word_count", float("inf"))
    cost_exceeded = cost_usd is not None and cost_usd >= thresholds.get("cost_usd", float("inf"))
    return (word_exceeded or cost_exceeded), word_exceeded, cost_exceeded


def send_slack_alert(issue_key, word_count, cost_usd, currency, source_lang, target_langs, word_exceeded, cost_exceeded):
    if not SLACK_WEBHOOK_URL:
        print("⚠️ SLACK_WEBHOOK_URL not set — skipping Slack alert.")
        return False

    issue_link = f"{JIRA_URL}/browse/{issue_key}" if JIRA_URL else issue_key
    target_str = ", ".join(target_langs) if isinstance(target_langs, list) else str(target_langs)

    reasons = []
    if word_exceeded:
        reasons.append("high word count")
    if cost_exceeded:
        reasons.append("high cost")

    cost_display = f"{currency} {cost_usd:,.2f}" if cost_usd is not None else "N/A"

    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"🚨 High-Cost Localization Job: {issue_key}"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Word Count:*\n{word_count:,}"},
                    {"type": "mrkdwn", "text": f"*Estimated Cost:*\n{cost_display}"},
                    {"type": "mrkdwn", "text": f"*Language Pair:*\n{source_lang.upper()} → {target_str.upper()}"},
                    {"type": "mrkdwn", "text": f"*Trigger:*\n{' & '.join(reasons)}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{issue_link}|View Jira Issue →>"}},
        ]
    }

    response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)

    if response.status_code == 200:
        print(f"✅ Slack alert sent for {issue_key}")
        return True

    print(f"⚠️ Slack alert failed ({response.status_code}): {response.text}")
    return False

def send_pipeline_log(message):
    """Simple text log message to #loc-pipeline-logs, separate from
    the high-cost alert channel."""
    webhook_url = os.getenv("SLACK_LOG_WEBHOOK_URL")
    if not webhook_url:
        print("⚠️ SLACK_LOG_WEBHOOK_URL not set — skipping pipeline log.")
        return False

    response = requests.post(webhook_url, json={"text": message}, timeout=10)
    if response.status_code == 200:
        return True

    print(f"⚠️ Pipeline log Slack message failed ({response.status_code}): {response.text}")
    return False
from flask import Flask, request, jsonify
import threading
import logging
import jira_phrase
import batch_runner
import time
from completion_checker import check_and_close_completed_jobs

COMPLETION_CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes — adjust as needed

logging.basicConfig(filename="webhook.log", level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

def handle_new_issue(issue_key):
    try:
        batch_runner.initialize_state_db()

        issue_info = jira_phrase.get_single_issue_info(issue_key)
        if not issue_info:
            logging.info(f"{issue_key}: not eligible, skipped.")
            return

        result = batch_runner.process_issue(issue_info)
        logging.info(f"{issue_key}: {result}")

    except Exception as e:
        logging.error(f"UNHANDLED ERROR processing {issue_key}: {e}")


@app.route("/webhook/jira-issue-created", methods=["POST"])
def jira_webhook():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "ignored"}), 200

    event = payload.get("webhookEvent")
    issue_key = payload.get("issue", {}).get("key")

    if not issue_key:
        return jsonify({"status": "ignored", "reason": "no issue key in payload"}), 200

    if event == "jira:issue_created":
        print(f"📩 Webhook received: issue created — {issue_key}")
        threading.Thread(target=handle_new_issue, args=(issue_key,)).start()
        return jsonify({"status": "accepted", "issue": issue_key, "event": event}), 200

    elif event == "jira:issue_updated":
        # Issue Updated fires on every field change — only act if this
        # specific update added an attachment.
        changelog_items = payload.get("changelog", {}).get("items", [])
        attachment_added = any(
            item.get("field") == "Attachment" and item.get("toString")
            for item in changelog_items
        )

        if attachment_added:
            print(f"📩 Webhook received: attachment added — {issue_key}")
            threading.Thread(target=handle_new_issue, args=(issue_key,)).start()
            return jsonify({"status": "accepted", "issue": issue_key, "event": event}), 200
        else:
            return jsonify({"status": "ignored", "reason": "no attachment change"}), 200

    return jsonify({"status": "ignored", "event": event}), 200

def run_completion_checker_loop():
    """Runs the completion checker on a repeating schedule for as long
    as the webhook server is running — no manual trigger needed."""
    while True:
        try:
            print("\n🔄 Running scheduled completion check...")
            check_and_close_completed_jobs()
        except Exception as e:
            logging.error(f"Completion checker loop error: {e}")
        time.sleep(COMPLETION_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=run_completion_checker_loop, daemon=True).start()  # NEW
    app.run(port=5000)

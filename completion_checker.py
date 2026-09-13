import sqlite3
from datetime import datetime, timezone
import jira_phrase
import requests
from slack_alert import send_pipeline_log

DB_PATH = "localization_automation.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_open_jobs():
    """Jobs the pipeline has finished uploading, but not yet confirmed
    delivered by a linguist in Phrase."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM localization_jobs WHERE status = 'completed' AND phrase_project_uid IS NOT NULL"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_delivered(attachment_id):
    conn = get_db()
    conn.execute(
        "UPDATE localization_jobs SET status = 'completed_delivered', completed_at = ?, updated_at = ? WHERE attachment_id = ?",
        (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), attachment_id),
    )
    conn.commit()
    conn.close()


def get_all_job_statuses(token, project_uid):
    url = f"{jira_phrase.PHRASE_BASE_URL}/v1/projects/{project_uid}/jobs"
    headers = {"Authorization": f"ApiToken {token}"}
    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        print(f"⚠️ Could not fetch jobs for project {project_uid}: {response.status_code}")
        return []

    data = response.json()
    return data.get("content", data if isinstance(data, list) else [])


def check_and_close_completed_jobs():
    open_jobs = get_open_jobs()
    if not open_jobs:
        print("No open jobs to check.")
        return

    print(f"Checking {len(open_jobs)} open job(s) for delivery status...")
    token = jira_phrase.get_phrase_token()

    for job in open_jobs:
        issue_key = job["issue_key"]
        project_uid = job["phrase_project_uid"]

        phrase_jobs = get_all_job_statuses(token, project_uid)
        if not phrase_jobs:
            continue

        all_delivered = all(
            j.get("status") in ("DELIVERED", "COMPLETED") for j in phrase_jobs)

        if all_delivered:
            print(f"✅ {issue_key}: all jobs COMPLETED — closing.")

            for phrase_job in phrase_jobs:
                job_uid = phrase_job.get("uid")
                filename, file_content = jira_phrase.download_phrase_target_file(token, project_uid, job_uid)
                if file_content:
                    jira_phrase.upload_file_to_jira_issue(issue_key, filename, file_content)
                else:
                    print(f"⚠️ Could not retrieve translated file for job {job_uid} — skipping attachment.")

            jira_phrase.transition_issue(issue_key, ["Done", "Closed", "Complete", "Resolved"])
            jira_phrase.add_jira_delivery_comment(issue_key, project_uid)
            send_pipeline_log(f"✅ *{issue_key}* — translation delivered and issue closed. "
                               f"Word count: {job['word_count']}")
            mark_delivered(job["attachment_id"])
        else:
            statuses = [j.get("status") for j in phrase_jobs]
            print(f"⏳ {issue_key}: not yet fully delivered ({statuses})")


if __name__ == "__main__":
    check_and_close_completed_jobs()
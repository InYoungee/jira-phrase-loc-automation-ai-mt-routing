import argparse
import sqlite3
from datetime import datetime

DB_PATH = "localization_automation.db"


def get_db():
    """Return a SQLite connection with Row objects enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_job_history(status=None, issue_key=None, limit=50):
    """Return historical localization jobs, newest first."""
    conn = get_db()
    query = """
        SELECT attachment_id, issue_key, filename, source_lang, target_langs,
               word_count, phrase_project_uid, phrase_job_uid, status,
               estimated_translation_cost, estimated_lqa_cost,
               estimated_total_cost, currency, started_at, completed_at,
               ai_suitable_count, human_required_count, ai_mt_cost,
               created_at, updated_at
        FROM localization_jobs
        WHERE 1 = 1
    """
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)

    if issue_key:
        query += " AND issue_key = ?"
        params.append(issue_key)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_job(attachment_id):
    """Return one historical localization job."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM localization_jobs WHERE attachment_id = ?",
        (str(attachment_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_job_events(attachment_id=None, issue_key=None):
    """Return lifecycle events, oldest first."""
    conn = get_db()
    query = """
        SELECT id, attachment_id, issue_key, event_type, status,
               details, created_at
        FROM job_events
        WHERE 1 = 1
    """
    params = []

    if attachment_id is not None:
        query += " AND attachment_id = ?"
        params.append(str(attachment_id))

    if issue_key:
        query += " AND issue_key = ?"
        params.append(issue_key)

    query += " ORDER BY id ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_job_summary():
    """Return high-level localization job metrics."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_jobs,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                AS uploaded_jobs,
            SUM(CASE WHEN status = 'completed_delivered' THEN 1 ELSE 0 END)
                AS delivered_jobs,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                AS failed_jobs,
            COALESCE(SUM(word_count), 0) AS total_words,
            COALESCE(SUM(estimated_total_cost), 0) AS total_estimated_cost
        FROM localization_jobs
        """
    ).fetchone()
    conn.close()

    return {
        "total_jobs": int(row["total_jobs"] or 0),
        "uploaded_jobs": int(row["uploaded_jobs"] or 0),
        "delivered_jobs": int(row["delivered_jobs"] or 0),
        "failed_jobs": int(row["failed_jobs"] or 0),
        "total_words": int(row["total_words"] or 0),
        "total_estimated_cost": float(row["total_estimated_cost"] or 0),
    }


def get_historical_volume():
    """Return volume grouped by source/target configuration."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT source_lang, target_langs,
               COUNT(*) AS job_count,
               COALESCE(SUM(word_count), 0) AS total_words
        FROM localization_jobs
        GROUP BY source_lang, target_langs
        ORDER BY total_words DESC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_historical_cost():
    """Return estimated cost grouped by currency."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT currency,
               COUNT(*) AS job_count,
               COALESCE(SUM(estimated_translation_cost), 0)
                   AS translation_cost,
               COALESCE(SUM(estimated_lqa_cost), 0) AS lqa_cost,
               COALESCE(SUM(estimated_total_cost), 0) AS total_cost
        FROM localization_jobs
        WHERE currency IS NOT NULL
        GROUP BY currency
        ORDER BY total_cost DESC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_ai_mt_summary():
    """Return aggregate AI/MT routing stats across all jobs with data."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(ai_suitable_count), 0) AS total_ai_suitable,
            COALESCE(SUM(human_required_count), 0) AS total_human_required,
            COALESCE(SUM(ai_mt_cost), 0) AS total_ai_mt_cost,
            COUNT(CASE WHEN ai_suitable_count IS NOT NULL THEN 1 END) AS jobs_with_ai_mt_data
        FROM localization_jobs
        """
    ).fetchone()
    conn.close()

    return {
        "total_ai_suitable": int(row["total_ai_suitable"]),
        "total_human_required": int(row["total_human_required"]),
        "total_ai_mt_cost": float(row["total_ai_mt_cost"]),
        "jobs_with_ai_mt_data": int(row["jobs_with_ai_mt_data"]),
    }

def _parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def get_processing_time(attachment_id):
    """Return processing duration in seconds for one job."""
    job = get_job(attachment_id)
    if not job:
        return None

    started_at = _parse_timestamp(job.get("started_at"))
    completed_at = _parse_timestamp(job.get("completed_at"))

    if not started_at or not completed_at:
        return None

    return (completed_at - started_at).total_seconds()


def get_average_processing_time():
    """Return average completed-job processing time in seconds."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT started_at, completed_at
        FROM localization_jobs
        WHERE started_at IS NOT NULL
          AND completed_at IS NOT NULL
        """
    ).fetchall()
    conn.close()

    durations = []
    for row in rows:
        started_at = _parse_timestamp(row["started_at"])
        completed_at = _parse_timestamp(row["completed_at"])

        if started_at and completed_at:
            duration = (completed_at - started_at).total_seconds()
            if duration >= 0:
                durations.append(duration)

    return sum(durations) / len(durations) if durations else None


def _format_languages(source_lang, target_langs):
    if not target_langs:
        return "N/A"

    if isinstance(target_langs, str):
        return f"{source_lang or '?'} → {target_langs}"

    return f"{source_lang or '?'} → {', '.join(target_langs)}"


def print_job_history(rows):
    if not rows:
        print("No localization jobs found.")
        return

    print("\n" + "=" * 90)
    print("LOCALIZATION JOB HISTORY")
    print("=" * 90)

    for row in rows:
        print(
            f"{row['issue_key']} | {row['status']} | "
            f"{int(row['word_count'] or 0):,} words | "
            f"{_format_languages(row['source_lang'], row['target_langs'])} | "
            f"{row['currency'] or ''} "
            f"{float(row['estimated_total_cost'] or 0):,.2f}"
        )
        print(
            f"  Attachment: {row['attachment_id']} | "
            f"File: {row['filename'] or 'N/A'}"
        )


def print_job_events(rows):
    if not rows:
        print("No job events found.")
        return

    print("\n" + "=" * 90)
    print("JOB EVENT HISTORY")
    print("=" * 90)

    for row in rows:
        print(
            f"{row['created_at']} | {row['issue_key']} | "
            f"{row['event_type']} | {row['status'] or '-'}"
        )
        if row["details"]:
            print(f"  {row['details']}")


def print_summary():
    summary = get_job_summary()
    average_time = get_average_processing_time()

    print("\n" + "=" * 90)
    print("LOCALIZATION HISTORY SUMMARY")
    print("=" * 90)
    print(f"Total jobs:              {summary['total_jobs']}")
    print(f"Completed jobs:          {summary['completed_jobs']}")
    print(f"Failed jobs:             {summary['failed_jobs']}")
    print(f"Total words:             {summary['total_words']:,}")
    print(
        f"Estimated total cost:    "
        f"{summary['total_estimated_cost']:,.2f}"
    )

    if average_time is not None:
        print(f"Average processing time: {average_time:.2f} seconds")
    else:
        print("Average processing time: N/A")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Query historical Jira → Phrase localization data."
    )
    parser.add_argument("--issue", help="Show history for one Jira issue.")
    parser.add_argument(
        "--attachment",
        help="Show history for one Jira attachment ID.",
    )
    parser.add_argument("--status", help="Filter jobs by status.")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of jobs to display. Default: 50.",
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="Show lifecycle events instead of job history.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show high-level historical metrics.",
    )
    parser.add_argument(
        "--volume",
        action="store_true",
        help="Show localization volume by language configuration.",
    )
    parser.add_argument(
        "--cost",
        action="store_true",
        help="Show estimated cost by currency.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.summary:
        print_summary()
        return

    if args.volume:
        rows = get_historical_volume()
        print("\n" + "=" * 90)
        print("HISTORICAL LOCALIZATION VOLUME")
        print("=" * 90)

        if not rows:
            print("No historical volume found.")
            return

        for row in rows:
            print(
                f"{_format_languages(row['source_lang'], row['target_langs'])} | "
                f"{row['job_count']} jobs | "
                f"{int(row['total_words'] or 0):,} words"
            )
        return

    if args.cost:
        rows = get_historical_cost()
        print("\n" + "=" * 90)
        print("HISTORICAL LOCALIZATION COST")
        print("=" * 90)

        if not rows:
            print("No historical cost data found.")
            return

        for row in rows:
            print(
                f"{row['currency']} | {row['job_count']} jobs | "
                f"Translation: {float(row['translation_cost'] or 0):,.2f} | "
                f"LQA: {float(row['lqa_cost'] or 0):,.2f} | "
                f"Total: {float(row['total_cost'] or 0):,.2f}"
            )
        return

    if args.events:
        rows = get_job_events(
            attachment_id=args.attachment,
            issue_key=args.issue,
        )
        print_job_events(rows)
        return

    if args.attachment:
        job = get_job(args.attachment)
        if not job:
            print(
                f"No historical job found for attachment {args.attachment}."
            )
            return

        print_job_history([job])
        duration = get_processing_time(args.attachment)
        if duration is not None:
            print(f"\nProcessing time: {duration:.2f} seconds")
        return

    rows = get_job_history(
        status=args.status,
        issue_key=args.issue,
        limit=args.limit,
    )
    print_job_history(rows)


if __name__ == "__main__":
    main()

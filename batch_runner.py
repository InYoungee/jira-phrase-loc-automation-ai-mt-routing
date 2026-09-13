import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
import requests
import jira_phrase
import cost_estimator
from slack_alert import should_alert, send_slack_alert

DB_PATH = "localization_automation.db"

# Transient HTTP responses that are normally safe to retry for read-only
# operations and idempotent updates.
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

MAX_RETRIES = 3
BASE_RETRY_SECONDS = 2


# ============================================================
# STATE STORE
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_localization_jobs_table(conn):
    """Adds AI/MT tracking columns to localization_jobs if they don't
    already exist — safe to run on every startup, existing rows just
    get NULL until reprocessed."""
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(localization_jobs)").fetchall()}

    new_columns = {
        "ai_suitable_count": "INTEGER",
        "human_required_count": "INTEGER",
        "ai_mt_cost": "REAL",
    }

    for column_name, column_type in new_columns.items():
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE localization_jobs ADD COLUMN {column_name} {column_type}")
            print(f"✅ Added column '{column_name}' to localization_jobs")

    conn.commit()


def initialize_state_db():
    """Create the persistent state table if it does not exist."""
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_state (
            attachment_id TEXT PRIMARY KEY,
            issue_key TEXT NOT NULL,
            filename TEXT,
            file_hash TEXT,
            status TEXT NOT NULL,
            phrase_project_uid TEXT,
            phrase_job_uid TEXT,
            word_count INTEGER,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Schema migration for cost estimation fields.
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(processing_state)").fetchall()
    }

    if "source_lang" not in columns:
        conn.execute("ALTER TABLE processing_state ADD COLUMN source_lang TEXT")

    if "target_langs" not in columns:
        conn.execute("ALTER TABLE processing_state ADD COLUMN target_langs TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_estimates (
            attachment_id TEXT PRIMARY KEY,
            issue_key TEXT NOT NULL,
            source_lang TEXT NOT NULL,
            target_langs TEXT NOT NULL,
            word_count INTEGER NOT NULL,
            currency TEXT NOT NULL,
            translation_cost REAL NOT NULL,
            lqa_cost REAL NOT NULL,
            total_cost REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS localization_jobs (
            attachment_id TEXT PRIMARY KEY,
            issue_key TEXT NOT NULL,
            filename TEXT,
            file_hash TEXT,
            source_lang TEXT,
            target_langs TEXT,
            word_count INTEGER,
            phrase_project_uid TEXT,
            phrase_job_uid TEXT,
            status TEXT NOT NULL,
            estimated_translation_cost REAL,
            estimated_lqa_cost REAL,
            estimated_total_cost REAL,
            currency TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    migrate_localization_jobs_table(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attachment_id TEXT NOT NULL,
            issue_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_events_attachment
        ON job_events (attachment_id)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_events_issue
        ON job_events (issue_key)
        """
    )

    conn.commit()
    conn.close()


def get_state(attachment_id):
    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM processing_state
        WHERE attachment_id = ?
        """,
        (str(attachment_id),),
    ).fetchone()

    conn.close()
    return dict(row) if row else None


def upsert_state(
    attachment_id,
    issue_key,
    status,
    filename=None,
    file_hash=None,
    phrase_project_uid=None,
    phrase_job_uid=None,
    word_count=None,
    attempts=None,
    last_error=None,
    source_lang=None,
    target_langs=None,
):
    """Insert or update one processing checkpoint."""
    now = utc_now()
    conn = get_db()

    existing = conn.execute(
        """
        SELECT attachment_id
        FROM processing_state
        WHERE attachment_id = ?
        """,
        (str(attachment_id),),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE processing_state
            SET
                issue_key = ?,
                filename = COALESCE(?, filename),
                file_hash = COALESCE(?, file_hash),
                status = ?,
                phrase_project_uid = COALESCE(?, phrase_project_uid),
                phrase_job_uid = COALESCE(?, phrase_job_uid),
                word_count = COALESCE(?, word_count),
                attempts = COALESCE(?, attempts),
                last_error = ?,
                source_lang = COALESCE(?, source_lang),
                target_langs = COALESCE(?, target_langs),
                updated_at = ?
            WHERE attachment_id = ?
            """,
            (
                issue_key,
                filename,
                file_hash,
                status,
                phrase_project_uid,
                phrase_job_uid,
                word_count,
                attempts,
                last_error,
                source_lang,
                target_langs,
                now,
                str(attachment_id),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO processing_state (
                attachment_id,
                issue_key,
                filename,
                file_hash,
                status,
                phrase_project_uid,
                phrase_job_uid,
                word_count,
                attempts,
                last_error,
                source_lang,
                target_langs,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(attachment_id),
                issue_key,
                filename,
                file_hash,
                status,
                phrase_project_uid,
                phrase_job_uid,
                word_count,
                attempts or 0,
                last_error,
                source_lang,
                target_langs,
                now,
                now,
            ),
        )

    conn.commit()
    conn.close()


# ============================================================
# HISTORICAL DATA / AUDIT TRAIL
# ============================================================

def record_job_event(
    attachment_id,
    issue_key,
    event_type,
    status=None,
    details=None,
):
    """Append one event to the historical job audit trail."""
    conn = get_db()
    conn.execute(
        """
        INSERT INTO job_events (
            attachment_id,
            issue_key,
            event_type,
            status,
            details,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(attachment_id),
            issue_key,
            event_type,
            status,
            details,
            utc_now(),
        ),
    )
    conn.commit()
    conn.close()


def upsert_localization_job(
    attachment_id,
    issue_key,
    status,
    filename=None,
    file_hash=None,
    source_lang=None,
    target_langs=None,
    word_count=None,
    phrase_project_uid=None,
    phrase_job_uid=None,
    estimated_translation_cost=None,
    estimated_lqa_cost=None,
    estimated_total_cost=None,
    currency=None,
    started_at=None,
    completed_at=None,
    ai_suitable_count=None,
    human_required_count=None,
    ai_mt_cost=None,
):
    """Create or update the durable historical record for a localization job."""
    now = utc_now()
    conn = get_db()
    existing = conn.execute(
        """
        SELECT attachment_id
        FROM localization_jobs
        WHERE attachment_id = ?
        """,
        (str(attachment_id),),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE localization_jobs
            SET
                issue_key = ?,
                filename = COALESCE(?, filename),
                file_hash = COALESCE(?, file_hash),
                source_lang = COALESCE(?, source_lang),
                target_langs = COALESCE(?, target_langs),
                word_count = COALESCE(?, word_count),
                phrase_project_uid = COALESCE(?, phrase_project_uid),
                phrase_job_uid = COALESCE(?, phrase_job_uid),
                status = ?,
                estimated_translation_cost = COALESCE(
                    ?, estimated_translation_cost
                ),
                estimated_lqa_cost = COALESCE(?, estimated_lqa_cost),
                estimated_total_cost = COALESCE(?, estimated_total_cost),
                currency = COALESCE(?, currency),
                started_at = COALESCE(?, started_at),
                completed_at = COALESCE(?, completed_at),
                ai_suitable_count = COALESCE(?, ai_suitable_count),
                human_required_count = COALESCE(?, human_required_count),
                ai_mt_cost = COALESCE(?, ai_mt_cost),
                updated_at = ?
            WHERE attachment_id = ?
            """,
            (
                issue_key,
                filename,
                file_hash,
                source_lang,
                target_langs,
                word_count,
                phrase_project_uid,
                phrase_job_uid,
                status,
                estimated_translation_cost,
                estimated_lqa_cost,
                estimated_total_cost,
                currency,
                started_at,
                completed_at,
                ai_suitable_count,
                human_required_count,
                ai_mt_cost,
                now,
                str(attachment_id),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO localization_jobs (
                attachment_id,
                issue_key,
                filename,
                file_hash,
                source_lang,
                target_langs,
                word_count,
                phrase_project_uid,
                phrase_job_uid,
                status,
                estimated_translation_cost,
                estimated_lqa_cost,
                estimated_total_cost,
                currency,
                started_at,
                completed_at,
                ai_suitable_count,
                human_required_count,
                ai_mt_cost,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(attachment_id),
                issue_key,
                filename,
                file_hash,
                source_lang,
                target_langs,
                word_count,
                phrase_project_uid,
                phrase_job_uid,
                status,
                estimated_translation_cost,
                estimated_lqa_cost,
                estimated_total_cost,
                currency,
                started_at,
                completed_at,
                ai_suitable_count,
                human_required_count,
                ai_mt_cost,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()


def sync_localization_job_from_state(attachment_id):
    """Refresh the historical job snapshot from current state/cost records."""
    state = get_state(attachment_id)
    if not state:
        return

    saved_cost = get_saved_cost_estimate(attachment_id)

    target_langs = state.get("target_langs")
    if target_langs:
        try:
            target_langs = json.loads(target_langs)
        except (TypeError, json.JSONDecodeError):
            pass

    completed_at = utc_now() if state.get("status") == "completed" else None

    upsert_localization_job(
        attachment_id=attachment_id,
        issue_key=state["issue_key"],
        status=state.get("status") or "unknown",
        filename=state.get("filename"),
        file_hash=state.get("file_hash"),
        source_lang=state.get("source_lang"),
        target_langs=(
            json.dumps(target_langs)
            if isinstance(target_langs, list)
            else target_langs
        ),
        word_count=state.get("word_count"),
        phrase_project_uid=state.get("phrase_project_uid"),
        phrase_job_uid=state.get("phrase_job_uid"),
        estimated_translation_cost=(
            float(saved_cost["translation_cost"]) if saved_cost else None
        ),
        estimated_lqa_cost=(
            float(saved_cost["lqa_cost"]) if saved_cost else None
        ),
        estimated_total_cost=(
            float(saved_cost["total_cost"]) if saved_cost else None
        ),
        currency=saved_cost["currency"] if saved_cost else None,
        completed_at=completed_at,
    )


# ============================================================
# FILE HASH
# ============================================================

def calculate_file_hash(file_path):
    """Return a SHA-256 fingerprint of the downloaded source file."""
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


# ============================================================
# JIRA ATTACHMENT METADATA
# ============================================================

def get_attachment_metadata(issue_key, filename):
    """Retrieve the exact Jira attachment ID for the Excel file."""
    url = f"{jira_phrase.JIRA_URL}/rest/api/3/issue/{issue_key}"

    response = requests.get(
        url,
        auth=(jira_phrase.JIRA_EMAIL, jira_phrase.JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()

    attachments = response.json().get("fields", {}).get("attachment", [])

    for attachment in attachments:
        if attachment.get("filename") == filename:
            return {
                "id": str(attachment["id"]),
                "filename": attachment["filename"],
                "content": attachment["content"],
            }

    raise ValueError(
        f"Could not find attachment '{filename}' on {issue_key}."
    )


# ============================================================
# RETRY HELPER
# ============================================================

def _retry_wait_seconds(attempt, response=None):
    """Use Retry-After when available, otherwise exponential backoff."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass

    return BASE_RETRY_SECONDS * (2 ** (attempt - 1))


def retry_call(function, *args, **kwargs):
    """Retry transient HTTP/network failures safely."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return function(*args, **kwargs)

        except requests.HTTPError as exc:
            response = exc.response
            status_code = response.status_code if response is not None else None

            if status_code not in RETRYABLE_STATUS_CODES:
                raise

            if attempt == MAX_RETRIES:
                raise

            wait_seconds = _retry_wait_seconds(attempt, response)

            print(
                f"⚠️ HTTP {status_code}. "
                f"Retrying in {wait_seconds:.1f}s "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )
            time.sleep(wait_seconds)

        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise

            wait_seconds = _retry_wait_seconds(attempt)

            print(
                f"⚠️ Network error: {exc}. "
                f"Retrying in {wait_seconds:.1f}s "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )
            time.sleep(wait_seconds)


# ============================================================
# COST ESTIMATION
# ============================================================

def save_cost_estimate(
    attachment_id,
    issue_key,
    source_lang,
    target_langs,
    word_count,
):
    """Persist an idempotent cost estimate for one attachment."""
    estimate = cost_estimator.estimate_cost(
        source_lang=source_lang,
        target_langs=target_langs,
        word_count=int(word_count),
    )

    now = utc_now()
    conn = get_db()

    conn.execute(
        """
        INSERT INTO cost_estimates (
            attachment_id,
            issue_key,
            source_lang,
            target_langs,
            word_count,
            currency,
            translation_cost,
            lqa_cost,
            total_cost,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attachment_id) DO UPDATE SET
            issue_key = excluded.issue_key,
            source_lang = excluded.source_lang,
            target_langs = excluded.target_langs,
            word_count = excluded.word_count,
            currency = excluded.currency,
            translation_cost = excluded.translation_cost,
            lqa_cost = excluded.lqa_cost,
            total_cost = excluded.total_cost,
            updated_at = excluded.updated_at
        """,
        (
            str(attachment_id),
            issue_key,
            source_lang,
            json.dumps(target_langs),
            int(word_count),
            estimate.currency,
            estimate.translation_cost,
            estimate.lqa_cost,
            estimate.total_cost,
            now,
            now,
        ),
    )

    conn.commit()
    conn.close()

    return estimate


def get_saved_cost_estimate(attachment_id):
    conn = get_db()
    row = conn.execute(
        """
        SELECT *
        FROM cost_estimates
        WHERE attachment_id = ?
        """,
        (str(attachment_id),),
    ).fetchone()
    conn.close()

    if not row:
        return None

    return dict(row)


def _print_cost_estimate(issue_key, estimate, word_count):
    print("\n========== COST ESTIMATE ==========")
    print(f"Issue:              {issue_key}")
    print(f"Language pair:      {estimate.source_lang} → {', '.join(estimate.target_langs)}")
    print(f"Source words:       {int(word_count):,}")
    print(f"Translation:        {estimate.currency} {estimate.translation_cost:,.2f}")
    print(f"LQA:                {estimate.currency} {estimate.lqa_cost:,.2f}")
    print(f"Estimated total:    {estimate.currency} {estimate.total_cost:,.2f}")


def ensure_cost_estimate(
    issue,
    attachment_id,
    filename,
    word_count,
    state=None,
    verbose=True,
):
    """Ensure a cost estimate exists without touching Phrase."""
    saved = get_saved_cost_estimate(attachment_id)
    if saved:
        return {
            "status": "cost_skipped",
            "estimated_cost": float(saved["total_cost"]),
            "currency": saved["currency"],
        }

    state = state or get_state(attachment_id) or {}
    source_lang = state.get("source_lang")
    target_langs = None

    if state.get("target_langs"):
        try:
            target_langs = json.loads(state["target_langs"])
        except (TypeError, json.JSONDecodeError):
            target_langs = None

    if not source_lang or not target_langs:
        _, _, local_file_path = retry_call(
            jira_phrase.get_jira_issue_and_download_attachment,
            issue["key"],
        )

        if not local_file_path:
            raise RuntimeError(
                f"Could not download {filename} for cost-estimate backfill."
            )

        mapping = jira_phrase.detect_column_mapping(local_file_path)
        source_lang = mapping["source_lang"]
        target_langs = mapping["target_langs"]
        primary_target_lang = target_langs[0]  # NEW — only process the first target language for now
        target_langs = [primary_target_lang]  # CHANGED — pass only one language downstream, not the full list

        upsert_state(
            attachment_id=attachment_id,
            issue_key=issue["key"],
            filename=filename,
            source_lang=source_lang,
            target_langs=json.dumps(target_langs),
            word_count=int(word_count),
            status=state.get("status") or "completed",
        )

    estimate = save_cost_estimate(
        attachment_id=attachment_id,
        issue_key=issue["key"],
        source_lang=source_lang,
        target_langs=target_langs,
        word_count=int(word_count),
    )

    if verbose:
        _print_cost_estimate(issue["key"], estimate, word_count)

    return {
        "status": "cost_backfilled",
        "estimated_cost": estimate.total_cost,
        "currency": estimate.currency,
    }


# ============================================================
# MANUAL RESET / REPROCESS
# ============================================================

def reset_issue_state(issue_key):
    """Clear Phrase checkpoints for an explicitly requested reprocess."""
    conn = get_db()

    rows = conn.execute(
        """
        SELECT attachment_id
        FROM processing_state
        WHERE issue_key = ?
        """,
        (issue_key,),
    ).fetchall()

    if not rows:
        conn.close()
        print(f"⚠️ No stored state found for {issue_key}.")
        return 0

    now = utc_now()

    for row in rows:
        attachment_id = str(row["attachment_id"])

        conn.execute(
            """
            UPDATE processing_state
            SET
                status = 'reset',
                phrase_project_uid = NULL,
                phrase_job_uid = NULL,
                word_count = NULL,
                source_lang = NULL,
                target_langs = NULL,
                last_error = ?,
                updated_at = ?
            WHERE attachment_id = ?
            """,
            (
                f"Manual reprocess requested for {issue_key}",
                now,
                attachment_id,
            ),
        )

        conn.execute(
            "DELETE FROM cost_estimates WHERE attachment_id = ?",
            (attachment_id,),
        )

    conn.commit()
    conn.close()

    for row in rows:
        record_job_event(
            str(row["attachment_id"]),
            issue_key,
            "MANUAL_REPROCESS_REQUESTED",
            status="reset",
            details=(
                "Phrase checkpoints and cost estimate were reset "
                "for manual reprocessing."
            ),
        )

    print(
        f"🔄 Reset {len(rows)} stored checkpoint(s) for {issue_key}. "
        "The next batch run can create a fresh Phrase project/job."
    )
    return len(rows)


# ============================================================
# PIPELINE HELPERS
# ============================================================

def _get_saved_ids(state):
    state = state or {}
    return (
        state.get("phrase_project_uid"),
        state.get("phrase_job_uid"),
    )


def _mark_failed(attachment_id, issue, filename, attempts, error_message):
    """Persist failure while preserving all previously saved checkpoints."""
    current_state = get_state(attachment_id) or {}

    upsert_state(
        attachment_id=attachment_id,
        issue_key=issue["key"],
        filename=filename,
        status="failed",
        file_hash=current_state.get("file_hash"),
        phrase_project_uid=current_state.get("phrase_project_uid"),
        phrase_job_uid=current_state.get("phrase_job_uid"),
        word_count=current_state.get("word_count"),
        attempts=attempts,
        last_error=error_message,
    )


def _process_with_known_attachment(issue, attachment_id, filename):
    """Process one issue after its stable attachment ID is known."""
    issue_key = issue["key"]
    state = get_state(attachment_id) or {}

    if state.get("status") == "completed":
        word_count = state.get("word_count") or 0

        print(f"⏭️ {issue_key}: localization processing already completed.")

        if word_count <= 0:
            return {
                "issue_key": issue_key,
                "status": "skipped_duplicate",
                "word_count": 0,
            }

        saved_cost = get_saved_cost_estimate(attachment_id)

        if saved_cost:
            print(
                f"💰 {issue_key}: cost estimate already exists "
                f"→ {saved_cost['currency']} {float(saved_cost['total_cost']):,.2f}"
            )
            return {
                "issue_key": issue_key,
                "status": "skipped_duplicate",
                "word_count": int(word_count),
                "estimated_cost": float(saved_cost["total_cost"]),
                "currency": saved_cost["currency"],
            }

        print("💰 Cost estimate missing → calculating...")

        try:
            cost_result = ensure_cost_estimate(
                issue=issue,
                attachment_id=attachment_id,
                filename=filename,
                word_count=int(word_count),
                state=state,
            )
            return {
                "issue_key": issue_key,
                "status": "skipped_duplicate",
                "word_count": int(word_count),
                "estimated_cost": cost_result.get("estimated_cost", 0),
                "currency": cost_result.get("currency"),
            }
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            print(f"❌ COST BACKFILL FAILED {issue_key}")
            print(f"   {error_message}")
            return {
                "issue_key": issue_key,
                "status": "failed",
                "word_count": int(word_count),
                "error": error_message,
            }

    attempts = (state.get("attempts") or 0) + 1

    upsert_localization_job(
        attachment_id=attachment_id,
        issue_key=issue_key,
        filename=filename,
        status="processing",
        started_at=utc_now(),
    )
    record_job_event(
        attachment_id,
        issue_key,
        "JOB_RECEIVED",
        status="processing",
        details="Job entered the processing pipeline.",
    )

    upsert_state(
        attachment_id=attachment_id,
        issue_key=issue_key,
        filename=filename,
        status="processing",
        attempts=attempts,
        last_error=None,
    )

    try:
        state = get_state(attachment_id) or {}
        project_uid, job_uid = _get_saved_ids(state)

        local_file_path = None
        column_mapping = None
        source_lang = state.get("source_lang")
        target_langs = None
        ai_mt_stats = None
        ai_mt_cost = None

        if state.get("target_langs"):
            try:
                target_langs = json.loads(state["target_langs"])
            except (TypeError, json.JSONDecodeError):
                target_langs = None

        if job_uid:
            print(
                f"♻️ Existing Phrase job found: {job_uid}. "
                f"Resuming without another upload."
            )
        else:
            _, _, local_file_path = retry_call(
                jira_phrase.get_jira_issue_and_download_attachment,
                issue_key,
            )

            if not local_file_path:
                raise RuntimeError("Jira attachment download failed.")

            file_hash = calculate_file_hash(local_file_path)

            column_mapping = jira_phrase.detect_column_mapping(
                local_file_path
            )
            source_lang = column_mapping["source_lang"]
            target_langs = column_mapping["target_langs"]
            primary_target_lang = target_langs[0]  # only process the first target language for now
            target_langs = [primary_target_lang]  # pass only one language downstream, not the full list

            upsert_state(
                attachment_id=attachment_id,
                issue_key=issue_key,
                filename=filename,
                file_hash=file_hash,
                status="downloaded",
                attempts=attempts,
                source_lang=source_lang,
                target_langs=json.dumps(target_langs),
            )

            record_job_event(
                attachment_id,
                issue_key,
                "FILE_ANALYZED",
                status="analyzed",
                details=(
                    f"Detected source language {source_lang}; "
                    f"target languages {target_langs}."
                ),
            )

            print(
                f"✅ Language configuration: "
                f"{source_lang} → {target_langs}"
            )

        # ----------------------------------------------------
        # 1.5 AI/MT LAYER (NEW)
        # ----------------------------------------------------
        if local_file_path and column_mapping:
            import ai_mt_pipeline
            import cost_estimator as cost_est

            _, ai_mt_stats, ai_processed_path = ai_mt_pipeline.run_ai_mt_layer(
                local_file_path, column_mapping
            )

            local_file_path = ai_processed_path  # upload the merged file, not the raw one

            ai_mt_cost = cost_est.estimate_ai_mt_cost(
                deepl_characters=ai_mt_stats["deepl_characters"],
                claude_input_tokens=ai_mt_stats["input_tokens"],
                claude_output_tokens=ai_mt_stats["output_tokens"],
            )

            record_job_event(
                attachment_id,
                issue_key,
                "AI_MT_PROCESSED",
                status="ai_mt_processed",
                details=(
                    f"{ai_mt_stats['ai_suitable_count']} AI-suitable / "
                    f"{ai_mt_stats['human_required_count']} human-required rows | "
                    f"Est. AI/MT cost: ${ai_mt_cost['total_ai_mt_cost']:.4f} "
                    f"(MT: ${ai_mt_cost['mt_cost']:.4f}, LLM: ${ai_mt_cost['llm_cost']:.4f})"
                ),
            )
            upsert_localization_job(
                attachment_id=attachment_id,
                issue_key=issue_key,
                status="ai_mt_processed",
                ai_suitable_count=ai_mt_stats["ai_suitable_count"],
                human_required_count=ai_mt_stats["human_required_count"],
                ai_mt_cost=ai_mt_cost["total_ai_mt_cost"],
            )

            print(
                f"✅ AI/MT layer: {ai_mt_stats['ai_suitable_count']} AI-suitable, "
                f"{ai_mt_stats['human_required_count']} human-required | "
                f"Est. cost: ${ai_mt_cost['total_ai_mt_cost']:.4f}"
            )
        # ----------------------------------------------------
        # 2. Phrase login
        # ----------------------------------------------------
        phrase_token = retry_call(jira_phrase.get_phrase_token)

        if not phrase_token:
            raise RuntimeError("Phrase authentication failed.")

        # ----------------------------------------------------
        # 3. Create/resume Phrase project
        # ----------------------------------------------------
        state = get_state(attachment_id) or {}
        project_uid = state.get("phrase_project_uid") or project_uid

        if project_uid:
            print(f"♻️ Resuming existing Phrase project: {project_uid}")
        else:
            project_title = f"{issue_key}: {issue['summary']}"

            project_uid = jira_phrase.create_phrase_project(
                phrase_token,
                project_title,
                source_lang=source_lang,
                target_langs=target_langs,
                due_date=issue["due_date"],
            )

            if not project_uid:
                raise RuntimeError("Phrase project creation failed.")

            upsert_state(
                attachment_id=attachment_id,
                issue_key=issue_key,
                filename=filename,
                file_hash=(get_state(attachment_id) or {}).get("file_hash"),
                status="phrase_project_created",
                phrase_project_uid=project_uid,
                attempts=attempts,
                source_lang=source_lang,
                target_langs=json.dumps(target_langs),
            )

            record_job_event(
                attachment_id,
                issue_key,
                "PHRASE_PROJECT_CREATED",
                status="phrase_project_created",
                details=f"Phrase project UID: {project_uid}",
            )

        # ----------------------------------------------------
        # 4. Upload only when there is no saved Phrase job
        # ----------------------------------------------------
        state = get_state(attachment_id) or {}
        job_uid = state.get("phrase_job_uid") or job_uid

        if job_uid:
            print(
                f"♻️ Existing Phrase job found: {job_uid}. "
                f"Skipping duplicate upload."
            )
        else:
            if not local_file_path or not column_mapping:
                raise RuntimeError(
                    "Cannot upload: local file or column mapping is missing."
                )

            upload_result = jira_phrase.upload_excel_to_phrase(
                phrase_token,
                project_uid,
                local_file_path,
                column_mapping,
                target_langs=target_langs,
            )

            if not upload_result:
                raise RuntimeError("Phrase upload failed.")

            jobs = upload_result.get("jobs", [])
            if not jobs:
                raise RuntimeError("Phrase returned no jobs after upload.")

            job_uid = jobs[0].get("uid")
            if not job_uid:
                raise RuntimeError("Phrase job UID missing.")

            current_state = get_state(attachment_id) or {}
            upsert_state(
                attachment_id=attachment_id,
                issue_key=issue_key,
                filename=filename,
                file_hash=current_state.get("file_hash"),
                status="uploaded",
                phrase_project_uid=project_uid,
                phrase_job_uid=job_uid,
                attempts=attempts,
                source_lang=source_lang,
                target_langs=json.dumps(target_langs),
            )

            record_job_event(
                attachment_id,
                issue_key,
                "PHRASE_JOB_CREATED",
                status="uploaded",
                details=f"Phrase job UID: {job_uid}",
            )

        # ----------------------------------------------------
        # 5. Word count check
        # ----------------------------------------------------
        state = get_state(attachment_id) or {}
        word_count = state.get("word_count") or 0

        if word_count > 0 and state.get("status") in {
            "word_count_ready",
            "jira_updated",
            "completed",
        }:
            print(f"♻️ Saved Phrase word count found: {int(word_count):,}")
        else:
            job_result = retry_call(
                jira_phrase.wait_for_job_import,
                phrase_token,
                project_uid,
                job_uid,
            )

            word_count = 0
            if job_result:
                word_count = job_result.get("wordsCount", 0)

            if word_count == 0:
                print(
                    "⚠️ Job word count is 0. "
                    "Checking project-level word count..."
                )
                time.sleep(2)

                word_count = retry_call(
                    jira_phrase.get_phrase_project_word_count,
                    phrase_token,
                    project_uid,
                )

            if word_count <= 0:
                raise RuntimeError("Phrase returned zero words.")

            current_state = get_state(attachment_id) or {}
            upsert_state(
                attachment_id=attachment_id,
                issue_key=issue_key,
                filename=filename,
                file_hash=current_state.get("file_hash"),
                status="word_count_ready",
                phrase_project_uid=project_uid,
                phrase_job_uid=job_uid,
                word_count=int(word_count),
                attempts=attempts,
                source_lang=source_lang,
                target_langs=json.dumps(target_langs) if target_langs else None,
            )

            record_job_event(
                attachment_id,
                issue_key,
                "WORD_COUNT_READY",
                status="word_count_ready",
                details=f"Phrase word count: {int(word_count):,}",
            )

        # ----------------------------------------------------
        # 6. Cost estimate
        # ----------------------------------------------------
        current_state = get_state(attachment_id) or {}

        if not get_saved_cost_estimate(attachment_id):
            cost_result = ensure_cost_estimate(
                issue=issue,
                attachment_id=attachment_id,
                filename=filename,
                word_count=int(word_count),
                state=current_state,
            )
        else:
            saved_cost = get_saved_cost_estimate(attachment_id)
            cost_result = {
                "status": "cost_skipped",
                "estimated_cost": float(saved_cost["total_cost"]),
                "currency": saved_cost["currency"],
            }

        if cost_result.get("status") in {"cost_backfilled", "cost_created"}:
            record_job_event(
                attachment_id,
                issue_key,
                "COST_ESTIMATED",
                status="cost_estimated",
                details=(
                    f"Estimated total: "
                    f"{cost_result.get('currency')} "
                    f"{float(cost_result.get('estimated_cost', 0)):,.2f}"
                ),
            )

        # ----------------------------------------------------
        # 7. Update Jira (Word count, Status, & Description ADF)
        # ----------------------------------------------------
            # ----------------------------------------------------
            # 7. Update Jira (Word count, Status, & Comment)
            # ----------------------------------------------------
            retry_call(
                jira_phrase.update_jira_word_count_and_status,
                issue_key,
                int(word_count),
            )

            saved_cost = get_saved_cost_estimate(attachment_id)
            cost_val = saved_cost["total_cost"] if saved_cost else None
            curr_val = saved_cost["currency"] if saved_cost else "USD"

            # Post pipeline summary as a Comment (Preserves Description)
            jira_phrase.add_jira_comment_with_metadata(
                issue_key=issue_key,
                word_count=int(word_count),
                source_lang=source_lang or "KO",
                target_langs=target_langs or ["EN"],
                estimated_cost=cost_val,
                currency=curr_val,
                ai_suitable_count=ai_mt_stats["ai_suitable_count"] if ai_mt_stats else None,
                human_required_count=ai_mt_stats["human_required_count"] if ai_mt_stats else None,
                ai_mt_cost=ai_mt_cost["total_ai_mt_cost"] if ai_mt_cost else None,
            )

            alert_triggered, word_exceeded, cost_exceeded = should_alert(int(word_count), cost_val)
            if alert_triggered:
                send_slack_alert(
                    issue_key=issue_key,
                    word_count=int(word_count),
                    cost_usd=cost_val,
                    currency=curr_val,
                    source_lang=source_lang or "KO",
                    target_langs=target_langs or ["EN"],
                    word_exceeded=word_exceeded,
                    cost_exceeded=cost_exceeded,
                )

        current_state = get_state(attachment_id) or {}

        record_job_event(
            attachment_id,
            issue_key,
            "JIRA_UPDATED",
            status="jira_updated",
            details=f"Jira word count and Description updated.",
        )

        upsert_state(
            attachment_id=attachment_id,
            issue_key=issue_key,
            filename=filename,
            file_hash=current_state.get("file_hash"),
            status="jira_updated",
            phrase_project_uid=project_uid,
            phrase_job_uid=job_uid,
            word_count=int(word_count),
            attempts=attempts,
        )

        # ----------------------------------------------------
        # 8. Final checkpoint
        # ----------------------------------------------------
        upsert_state(
            attachment_id=attachment_id,
            issue_key=issue_key,
            filename=filename,
            file_hash=current_state.get("file_hash"),
            status="completed",
            phrase_project_uid=project_uid,
            phrase_job_uid=job_uid,
            word_count=int(word_count),
            attempts=attempts,
            last_error=None,
        )

        record_job_event(
            attachment_id,
            issue_key,
            "COMPLETED",
            status="completed",
            details=f"Localization job completed with {int(word_count):,} words.",
        )
        sync_localization_job_from_state(attachment_id)

        print(
            f"✅ COMPLETED {issue_key} | "
            f"{int(word_count):,} words | "
            f"Phrase project {project_uid}"
        )

        return {
            "issue_key": issue_key,
            "status": "completed",
            "word_count": int(word_count),
            "estimated_cost": float(saved_cost["total_cost"]) if saved_cost else 0,
            "currency": saved_cost["currency"] if saved_cost else None,
        }

    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        print(f"❌ FAILED {issue_key}")
        print(f"   {error_message}")

        _mark_failed(
            attachment_id,
            issue,
            filename,
            attempts,
            error_message,
        )

        record_job_event(
            attachment_id,
            issue_key,
            "FAILED",
            status="failed",
            details=error_message,
        )
        sync_localization_job_from_state(attachment_id)

        return {
            "issue_key": issue_key,
            "status": "failed",
            "error": error_message,
        }


def process_issue(issue):
    """Process one issue without allowing it to stop the batch."""
    issue_key = issue["key"]
    filename = issue["attachment"]

    print("\n" + "=" * 70)
    print(f"PROCESSING {issue_key}")
    print("=" * 70)

    try:
        attachment = retry_call(
            get_attachment_metadata,
            issue_key,
            filename,
        )
        attachment_id = attachment["id"]

        return _process_with_known_attachment(
            issue,
            attachment_id,
            filename,
        )

    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        print(f"❌ FAILED {issue_key}")
        print(f"   {error_message}")

        return {
            "issue_key": issue_key,
            "status": "failed",
            "error": error_message,
        }


# ============================================================
# BATCH RUNNER
# ============================================================

def run_batch(reprocess_issues=None):
    initialize_state_db()

    for issue_key in reprocess_issues or []:
        reset_issue_state(issue_key)

    print("\n" + "=" * 70)
    print("JIRA → PHRASE BATCH AUTOMATION")
    print("=" * 70)

    issues = retry_call(jira_phrase.get_eligible_jira_issues)

    if not issues:
        print("No eligible Jira issues found.")
        return

    print(f"\nEligible Jira issues: {len(issues)}")

    results = []

    for issue in issues:
        result = process_issue(issue)
        results.append(result)

    completed = [
        r for r in results if r["status"] == "completed"
    ]
    skipped = [
        r for r in results if r["status"] == "skipped_duplicate"
    ]
    failed = [
        r for r in results if r["status"] == "failed"
    ]

    total_words = sum(
        r.get("word_count", 0)
        for r in results
    )

    total_estimated_cost = sum(
        r.get("estimated_cost", 0) or 0
        for r in results
    )

    currencies = {
        r.get("currency")
        for r in results
        if r.get("currency")
    }

    print("\n" + "=" * 70)
    print("BATCH SUMMARY")
    print("=" * 70)
    print(f"Eligible:          {len(issues)}")
    print(f"Completed:         {len(completed)}")
    print(f"Skipped/duplicate: {len(skipped)}")
    print(f"Failed:            {len(failed)}")
    print(f"Total words:       {total_words:,}")

    if total_estimated_cost:
        currency = next(iter(currencies)) if len(currencies) == 1 else "MIXED"
        print(f"Estimated cost:    {currency} {total_estimated_cost:,.2f}")

    if failed:
        print("\nFailed issues:")
        for result in failed:
            print(
                f"  - {result['issue_key']}: "
                f"{result.get('error', 'Unknown error')}"
            )

    print("=" * 70)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Jira → Phrase localization automation."
    )
    parser.add_argument(
        "--reprocess",
        nargs="+",
        metavar="ISSUE_KEY",
        help=(
            "Explicitly reset stored Phrase checkpoints for these Jira issues "
            "before processing."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_batch(reprocess_issues=args.reprocess)
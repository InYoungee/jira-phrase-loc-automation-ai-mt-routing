import json
import os
import time
import requests
import openpyxl
from openpyxl.utils import get_column_letter
from dotenv import load_dotenv

# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

PHRASE_USERNAME = os.getenv("PHRASE_USERNAME")
PHRASE_PASSWORD = os.getenv("PHRASE_PASSWORD")
PHRASE_BASE_URL = os.getenv("PHRASE_BASE_URL")

JIRA_WORD_COUNT_FIELD_ID = "customfield_10073"


# ============================================================
# BASIC VALIDATION
# ============================================================

required_variables = {
    "JIRA_URL": JIRA_URL,
    "JIRA_EMAIL": JIRA_EMAIL,
    "JIRA_API_TOKEN": JIRA_API_TOKEN,
    "PHRASE_USERNAME": PHRASE_USERNAME,
    "PHRASE_PASSWORD": PHRASE_PASSWORD,
    "PHRASE_BASE_URL": PHRASE_BASE_URL,
}

for name, value in required_variables.items():
    if not value:
        raise ValueError(f"❌ Missing environment variable: {name}")

print("✅ Environment variables loaded")


# ============================================================
# JIRA: SEARCH ELIGIBLE ISSUES + RETRIEVE METADATA
# ============================================================

def get_eligible_jira_issues():
    """Find Jira issues ready for localization processing.

    The search request is a read-only Jira operation, so the batch runner
    may safely retry transient HTTP/network failures around this function.
    """
    url = f"{JIRA_URL}/rest/api/3/search/jql"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    jql = (
        'project = GAME '
        'AND status = "To Do" '
        'AND labels = "Trans" '
        'AND attachments is not EMPTY '
        'ORDER BY created ASC'
    )

    payload = {
        "jql": jql,
        "maxResults": 50,
        "fields": [
            "summary", "status", "labels", "assignee", "duedate", "attachment"
        ],
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        auth=auth,
        timeout=30,
    )

    response.raise_for_status()

    print("\n========== JIRA ISSUE SEARCH ==========")
    print(f"Search status: {response.status_code}")

    data = response.json()
    issues = data.get("issues", [])
    print(f"Found {len(issues)} eligible issues.")

    eligible_issues = []

    for issue in issues:
        fields = issue.get("fields", {})
        issue_key = issue["key"]
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "")
        labels = fields.get("labels", [])

        assignee_data = fields.get("assignee")
        if assignee_data:
            assignee = (
                assignee_data.get("displayName")
                or assignee_data.get("emailAddress")
                or assignee_data.get("accountId")
            )
        else:
            assignee = None

        due_date = fields.get("duedate")
        attachments = fields.get("attachment", [])

        excel_attachments = [
            attachment
            for attachment in attachments
            if attachment["filename"].lower().endswith((".xlsx", ".xls"))
        ]

        if len(excel_attachments) != 1:
            print(
                f"⚠️ {issue_key}: expected 1 Excel attachment, "
                f"found {len(excel_attachments)}. Skipping."
            )
            continue

        attachment = excel_attachments[0]

        issue_info = {
            "key": issue_key,
            "summary": summary,
            "status": status,
            "labels": labels,
            "assignee": assignee,
            "due_date": due_date,
            "attachment": attachment["filename"],
        }
        eligible_issues.append(issue_info)

        print(
            f"\n✅ {issue_key}"
            f"\n   Summary:    {summary}"
            f"\n   Status:     {status}"
            f"\n   Labels:     {labels}"
            f"\n   Assignee:   {assignee}"
            f"\n   Due date:   {due_date}"
            f"\n   Attachment: {attachment['filename']}"
        )

    return eligible_issues

def get_single_issue_info(issue_key):
    """Fetch one Jira issue by key and build the same info dict
    get_eligible_jira_issues() produces, applying the same
    eligibility checks (Excel attachment, 'Trans' label)."""
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}

    response = requests.get(url, headers=headers, auth=auth, timeout=30)
    response.raise_for_status()

    data = response.json()
    fields = data.get("fields", {})
    labels = fields.get("labels", [])

    if "Trans" not in labels:
        print(f"⚠️ {issue_key}: missing 'Trans' label — not eligible.")
        return None

    attachments = fields.get("attachment", [])
    excel_attachments = [
        a for a in attachments
        if a["filename"].lower().endswith((".xlsx", ".xls"))
    ]

    if len(excel_attachments) != 1:
        print(f"⚠️ {issue_key}: expected 1 Excel attachment, found {len(excel_attachments)}.")
        return None

    assignee_data = fields.get("assignee")
    assignee = (
        assignee_data.get("displayName")
        or assignee_data.get("emailAddress")
        or assignee_data.get("accountId")
    ) if assignee_data else None

    return {
        "key": issue_key,
        "summary": fields.get("summary", ""),
        "status": fields.get("status", {}).get("name", ""),
        "labels": labels,
        "assignee": assignee,
        "due_date": fields.get("duedate"),
        "attachment": excel_attachments[0]["filename"],
    }

def detect_column_mapping(file_path):
    """
    Detects source/target columns and language codes from the Excel header.

    Expected examples:
        Source_KO
        Target_EN

    Also supports:
        Source_EN
        Target_KO

    And multiple target languages:
        Source_KO
        Target_EN
        Target_JA

    Returns:
        {
            "source_column": "D",
            "source_lang": "ko",
            "target_columns": {
                "en": "J",
                "ja": "K"
            }
        }
    """

    wb = openpyxl.load_workbook(file_path, read_only=True)
    ws = wb[wb.sheetnames[0]]

    headers = [
        cell.value
        for cell in next(ws.iter_rows(min_row=1, max_row=1))
    ]

    mapping = {
        "target_columns": {}
    }

    for idx, header in enumerate(headers, start=1):

        if not header:
            continue

        header_text = str(header).strip()
        header_lower = header_text.lower()

        letter = get_column_letter(idx)

        # ----------------------------------------------------
        # SOURCE LANGUAGE
        # Example: Source_KO
        # ----------------------------------------------------

        if header_lower.startswith("source_"):

            language_code = header_lower.replace(
                "source_", "", 1
            ).strip()

            if language_code:
                mapping["source_column"] = letter
                mapping["source_lang"] = language_code

        # ----------------------------------------------------
        # TARGET LANGUAGE
        # Example: Target_EN
        # Example: Target_JA
        # ----------------------------------------------------

        elif header_lower.startswith("target_"):

            language_code = header_lower.replace(
                "target_", "", 1
            ).strip()

            if language_code:
                mapping["target_columns"][language_code] = letter

        # ----------------------------------------------------
        # CONTEXT / METADATA COLUMNS
        # ----------------------------------------------------

        elif header_lower == "id" or header_lower.endswith("_id"):

            mapping["context_key_column"] = letter

        elif "note" in header_lower or "comment" in header_lower:

            mapping["context_note_column"] = letter

    wb.close()

    # --------------------------------------------------------
    # PRINT DETECTED METADATA
    # --------------------------------------------------------

    print("\n========== COLUMN DETECTION ==========")
    print(json.dumps(mapping, indent=2))

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if "source_column" not in mapping:
        raise ValueError(
            "❌ Could not detect a Source column. "
            "Expected a header such as 'Source_KO'."
        )

    if "source_lang" not in mapping:
        raise ValueError(
            "❌ Could not determine the source language."
        )

    if not mapping["target_columns"]:
        raise ValueError(
            "❌ Could not detect any Target columns. "
            "Expected headers such as 'Target_EN' or 'Target_JA'."
        )

    # --------------------------------------------------------
    # CONVENIENCE VALUES
    # --------------------------------------------------------

    mapping["target_langs"] = list(
        mapping["target_columns"].keys()
    )

    if len(mapping["target_columns"]) == 1:

        mapping["target_column"] = next(
            iter(mapping["target_columns"].values())
        )

    # --------------------------------------------------------
    # PRINT SUMMARY
    # --------------------------------------------------------

    print(f"✅ Source column: {mapping['source_column']}")
    print(f"✅ Source language: {mapping['source_lang']}")
    print(f"✅ Target languages: {mapping['target_langs']}")

    for language, column in mapping["target_columns"].items():
        print(
            f"✅ Target column ({language}): {column}"
        )

    return mapping

# ============================================================
# PHRASE IMPORT SETTINGS
# ============================================================

def create_import_settings(token, name, column_mapping, target_lang):
    """Create Phrase import settings for the multilingual XLS upload.
    target_lang must be explicitly provided — no default, since silently
    assuming a language is exactly the bug this replaces."""
    url = f"{PHRASE_BASE_URL}/v1/importSettings"
    headers = {
        "Authorization": f"ApiToken {token}",
        "Content-Type": "application/json",
    }

    source_column = column_mapping.get("source_column")
    target_column = column_mapping.get("target_columns", {}).get(target_lang)

    if not source_column or not target_column:
        raise ValueError(
            f"Could not resolve column letters for source or target_lang='{target_lang}'. "
            f"column_mapping={column_mapping}"
        )

    multilingual_config = {
        "sourceColumn": source_column,
        "targetColumns": {target_lang: target_column},
        "segmentation": True,
        "htmlToText": True,
    }

    if column_mapping.get("context_key_column"):
        multilingual_config["contextKeyColumn"] = column_mapping["context_key_column"]
    if column_mapping.get("context_note_column"):
        multilingual_config["contextNoteColumn"] = column_mapping["context_note_column"]

    payload = {
        "name": name,
        "fileImportSettings": {
            "fileFormat": "multiling_xls",
            "multilingualXls": multilingual_config,
        },
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()

    settings_uid = response.json().get("uid")
    if not settings_uid:
        raise RuntimeError("Phrase import settings response did not contain a UID.")

    print(f"✅ Import settings created: {settings_uid}")
    return settings_uid

def get_jira_issue_and_download_attachment(issue_key):
    """Fetch a Jira issue and download its first Excel attachment.

    HTTP/network failures are raised so the batch runner can apply its
    safe retry policy. Missing attachments remain normal validation failures.
    """
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}

    print("\n========== GETTING JIRA ISSUE ==========")
    print(f"Issue: {issue_key}")

    response = requests.get(
        url,
        headers=headers,
        auth=auth,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    summary = data["fields"]["summary"]
    attachments = data["fields"].get("attachment", [])

    if not attachments:
        print(f"⚠️ No attachments found on {issue_key}.")
        return summary, None, None

    excel_attachments = [
        attachment
        for attachment in attachments
        if attachment["filename"].lower().endswith((".xlsx", ".xls"))
    ]

    if not excel_attachments:
        print(f"❌ No Excel attachment found on {issue_key}.")
        return summary, None, None

    attachment = excel_attachments[0]
    file_name = attachment["filename"]
    download_url = attachment["content"]

    print(f"✅ Found attachment: {file_name}")

    file_res = requests.get(
        download_url,
        auth=auth,
        timeout=60,
    )
    file_res.raise_for_status()

    os.makedirs("downloads", exist_ok=True)
    local_path = os.path.join("downloads", file_name)

    with open(local_path, "wb") as f:
        f.write(file_res.content)

    print(f"✅ Downloaded to: {local_path}")
    return summary, file_name, local_path

def update_jira_word_count_and_status(issue_key, word_count):
    """Update Jira's word-count field and ensure the issue is In Progress.

    The word-count PUT is idempotent. The status transition is made
    application-idempotent by checking the current status before issuing
    the transition POST. This means the whole function can be retried
    safely if a network failure occurs after a successful Jira update.
    """
    if word_count <= 0:
        raise ValueError("Word count must be greater than zero.")

    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    url_update = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    payload = {
        "fields": {
            JIRA_WORD_COUNT_FIELD_ID: int(word_count)
        }
    }

    response = requests.put(
        url_update,
        json=payload,
        headers=headers,
        auth=auth,
        timeout=30,
    )
    response.raise_for_status()

    print(f"✅ Jira Word Count updated to {word_count:,}")

    # --------------------------------------------------------
    # Ensure current Jira status is In Progress.
    # --------------------------------------------------------
    transition_url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/transitions"

    transition_response = requests.get(
        transition_url,
        headers=headers,
        auth=auth,
        timeout=30,
    )
    transition_response.raise_for_status()

    transitions = transition_response.json().get("transitions", [])

    issue_response = requests.get(
        url_update,
        headers=headers,
        auth=auth,
        timeout=30,
    )
    issue_response.raise_for_status()

    current_status = (
        issue_response.json()
        .get("fields", {})
        .get("status", {})
        .get("name", "")
    )

    if current_status.strip().lower() == "in progress":
        print(f"✅ {issue_key} is already 'In Progress'.")
        return True

    in_progress_id = None
    for transition in transitions:
        if transition.get("name", "").strip().lower() == "in progress":
            in_progress_id = transition.get("id")
            break

    if not in_progress_id:
        raise RuntimeError("'In Progress' transition is not available.")

    payload_trans = {
        "transition": {
            "id": in_progress_id
        }
    }

    res_trans = requests.post(
        transition_url,
        json=payload_trans,
        headers=headers,
        auth=auth,
        timeout=30,
    )
    res_trans.raise_for_status()

    print(f"✅ {issue_key} moved to 'In Progress'.")
    return True


def add_jira_comment_with_metadata(
    issue_key, word_count, source_lang, target_langs, estimated_cost=None, currency="USD",
    ai_suitable_count=None, human_required_count=None, ai_mt_cost=None
):
    """
    Posts a structured pipeline metadata summary as a Comment on the Jira issue using ADF JSON.
    """
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/comment"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    target_str = ", ".join(target_langs) if isinstance(target_langs, list) else str(target_langs)
    cost_text = f"{currency} {estimated_cost:,.2f}" if estimated_cost else "N/A"

    bullet_items = [
        {
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Language Pair: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"{source_lang.upper()} → {target_str.upper()}"}
                ]
            }]
        },
        {
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Total Word Count: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"{word_count:,} words"}
                ]
            }]
        },
        {
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Estimated Cost: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": cost_text}
                ]
            }]
        },
    ]

    # NEW — only added when the AI/MT layer actually ran for this job
    if ai_suitable_count is not None and human_required_count is not None:
        ai_mt_cost_text = f"${ai_mt_cost:.4f}" if ai_mt_cost is not None else "N/A"
        bullet_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "AI/MT Routing: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": (
                        f"{ai_suitable_count} AI-suitable, {human_required_count} human-required "
                        f"(est. AI/MT cost: {ai_mt_cost_text})"
                    )}
                ]
            }]
        })

    adf_comment = {
        "body": {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": "🤖 Localization Pipeline Report"}]
                },
                {"type": "bulletList", "content": bullet_items},
            ]
        }
    }

    response = requests.post(url, json=adf_comment, headers=headers, auth=auth, timeout=30)

    if response.status_code in [200, 201]:
        print(f"✅ Posted metadata comment on {issue_key}")
        return True

    print(f"⚠️ Failed to post Jira comment ({response.status_code}): {response.text}")
    return False
def get_phrase_token():
    """Authenticate with Phrase.

    Raises HTTP/network errors so the batch runner can retry transient
    failures while allowing authentication/validation errors to fail fast.
    """
    login_url = f"{PHRASE_BASE_URL}/v3/auth/login"
    payload = {
        "userName": PHRASE_USERNAME,
        "password": PHRASE_PASSWORD,
    }
    headers = {"Content-Type": "application/json"}

    print("\n========== PHRASE LOGIN ==========")

    response = requests.post(
        login_url,
        json=payload,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    token = response.json().get("token")
    if not token:
        raise RuntimeError("Phrase authentication returned no token.")

    print("✅ Phrase authentication successful.")
    return token

def create_phrase_project(token, project_name, source_lang, target_langs, due_date=None):
    """Create a Phrase project. target_langs must be explicitly provided —
    no silent default, since the caller is responsible for deciding which
    language(s) this project actually processes."""
    if not target_langs:
        raise ValueError("target_langs is required and cannot be empty.")

    url = f"{PHRASE_BASE_URL}/v1/projects"
    headers = {"Authorization": f"ApiToken {token}", "Content-Type": "application/json"}
    payload = {"name": project_name, "sourceLang": source_lang, "targetLangs": target_langs}

    if due_date:
        payload["dateDue"] = f"{due_date}T23:59:59Z"

    print("\n========== CREATING PHRASE PROJECT ==========")
    print(f"Project name: {project_name}")
    print(f"Source language: {source_lang}")
    print(f"Target languages: {target_langs}")
    print(f"Jira due date: {due_date}")

    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()

    project_uid = response.json().get("uid")
    if not project_uid:
        raise RuntimeError("Phrase project response did not contain a UID.")

    print(f"✅ Phrase Project Created: {project_uid}")
    return project_uid

def upload_excel_to_phrase(token, project_uid, file_path, column_mapping, target_langs=None):
    """Upload Excel to an existing Phrase project.

    This operation is intentionally not retried automatically because the
    POST creates a Phrase job. If the response is lost after the server has
    created the job, blindly retrying can create a duplicate job.
    """
    if target_langs is None:
        target_langs = column_mapping["target_langs"]

    print("\n========== UPLOADING TO PHRASE ==========")

    settings_uid = create_import_settings(
        token,
        f"settings_{os.path.basename(file_path)}",
        column_mapping,
        target_lang=target_langs[0],
    )

    url = f"{PHRASE_BASE_URL}/v1/projects/{project_uid}/jobs"
    file_name = os.path.basename(file_path)
    encoded_name = requests.utils.quote(file_name)

    memsource_config = {
        "targetLangs": target_langs,
        "importSettings": {"uid": settings_uid},
    }

    headers = {
        "Authorization": f"ApiToken {token}",
        "Memsource": json.dumps(memsource_config),
        "Content-Disposition": f"filename*=UTF-8''{encoded_name}",
        "Content-Type": "application/octet-stream",
    }

    with open(file_path, "rb") as f:
        file_content = f.read()

    response = requests.post(
        url,
        headers=headers,
        data=file_content,
        timeout=120,
    )
    response.raise_for_status()

    print(f"✅ Successfully uploaded '{file_name}' to Phrase.")
    return response.json()

def wait_for_job_import(token, project_uid, job_uid, max_attempts=10, wait_seconds=3):
    """Poll a Phrase job until its word count is available.

    A transient HTTP/network failure is raised so the outer retry layer can
    safely restart the polling operation. Polling itself is read-only.
    """
    url = f"{PHRASE_BASE_URL}/v1/projects/{project_uid}/jobs/{job_uid}"
    headers = {
        "Authorization": f"ApiToken {token}",
        "Accept": "application/json",
    }

    print("\n========== CHECKING PHRASE JOB IMPORT ==========")

    for attempt in range(1, max_attempts + 1):
        response = requests.get(
            url,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        words = data.get("wordsCount", 0)
        status = data.get("status", "")

        print(
            f"[Check {attempt}/{max_attempts}] "
            f"Job Status: {status} | Words: {words}"
        )

        if words > 0:
            print("✅ Phrase job import completed!")
            return data

        if attempt < max_attempts:
            time.sleep(wait_seconds)

    print("⚠️ Job-level check timed out.")
    return None

def get_phrase_project_word_count(token, project_uid):
    """Read project-level word count from Phrase."""
    url = f"{PHRASE_BASE_URL}/v1/projects/{project_uid}"
    headers = {
        "Authorization": f"ApiToken {token}",
        "Accept": "application/json",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    word_count = response.json().get("wordCount", 0)
    print(f"[project_check] Project-level Word Count: {word_count:,}")
    return word_count

def run_pipeline_for_issue(issue):

    issue_key = issue["key"]
    summary = issue["summary"]
    due_date = issue["due_date"]

    print("\n==================================================")
    print(f"STARTING AUTOMATION FOR {issue_key}")
    print("==================================================")
    print(f"Summary:    {summary}")
    print(f"Labels:     {issue['labels']}")
    print(f"Assignee:   {issue['assignee']}")
    print(f"Due date:   {due_date}")
    print(f"Attachment: {issue['attachment']}")

    # ========================================================
    # 1. DOWNLOAD JIRA ATTACHMENT
    # ========================================================
    summary_from_jira, file_name, local_file_path = get_jira_issue_and_download_attachment(issue_key)
    if not local_file_path:
        print("❌ Stopping pipeline due to missing attachment.")
        return False

    # ========================================================
    # 1.5 DETECT LANGUAGE PAIR FROM FILE (NEW)
    # ========================================================
    column_mapping = detect_column_mapping(local_file_path)
    source_lang = column_mapping["source_lang"]
    target_langs = column_mapping["target_langs"]

    print(f"\n✅ Detected language pair: {source_lang} → {target_langs}")

    # ========================================================
    # 2. PHRASE LOGIN
    # ========================================================
    phrase_token = get_phrase_token()
    if not phrase_token:
        return False

    # ========================================================
    # 3. CREATE PHRASE PROJECT
    # ========================================================
    project_title = f"{issue_key}: {summary}"
    project_uid = create_phrase_project(
        phrase_token,
        project_title,
        source_lang=source_lang,
        target_langs=target_langs,
        due_date=due_date
    )
    if not project_uid:
        print("❌ Phrase project creation failed.")
        return False

    # ========================================================
    # 4. UPLOAD EXCEL TO PHRASE
    # ========================================================
    upload_result = upload_excel_to_phrase(
        phrase_token,
        project_uid,
        local_file_path,
        column_mapping,
        target_langs=target_langs
    )
    if not upload_result:
        print("❌ Phrase upload failed.")
        return False

    # ========================================================
    # 5. GET PHRASE JOB
    # ========================================================
    jobs = upload_result.get("jobs", [])
    if not jobs:
        print("❌ Phrase returned no jobs.")
        return False

    job = jobs[0]
    job_uid = job.get("uid")
    if not job_uid:
        print("❌ Phrase job UID missing.")
        return False

    print(f"\nPhrase Job UID: {job_uid}")

    # ========================================================
    # 6. WAIT FOR WORD COUNT
    # ========================================================
    job_result = wait_for_job_import(phrase_token, project_uid, job_uid)

    word_count = 0
    if job_result:
        word_count = job_result.get("wordsCount", 0)

    # ========================================================
    # 7. PROJECT-LEVEL FALLBACK
    # ========================================================
    if word_count == 0:
        print("\n⚠️ Job word count is 0.")
        print("Fetching Project-level word count...")
        time.sleep(2)
        word_count = get_phrase_project_word_count(phrase_token, project_uid)

    # ========================================================
    # 8. FINAL WORD COUNT
    # ========================================================
    print("\n========== PHRASE WORD COUNT ==========")
    print(f"Final Word Count: {word_count:,}")

    if word_count <= 0:
        print("❌ Phrase returned zero words.")
        print("⚠️ Jira will NOT be updated.")
        return False

    # ========================================================
    # 9. UPDATE JIRA WORD COUNT
    # ========================================================
    update_jira_word_count_and_status(issue_key, word_count)

    # ========================================================
    # 10. SUCCESS
    # ========================================================
    print("\n==================================================")
    print(f"PIPELINE COMPLETED FOR {issue_key}")
    print("==================================================")
    print(f"Phrase Project UID: {project_uid}")
    print(f"Phrase Word Count: {word_count:,}")
    print(f"Language pair: {source_lang} → {target_langs}")
    print(f"Jira Due Date: {due_date}")
    print("Jira Status: In Progress")

    return True


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "\n=================================================="
    )

    print(
        "JIRA → PHRASE LOCALIZATION AUTOMATION"
    )

    print(
        "=================================================="
    )

    # --------------------------------------------------------
    # 1. Find eligible Jira issues automatically
    # --------------------------------------------------------

    issues = get_eligible_jira_issues()

    if not issues:

        print(
            "\nNo eligible Jira issues found."
        )

        print(
            "Pipeline finished."
        )

        exit()

    # --------------------------------------------------------
    # 2. Process every eligible issue
    # --------------------------------------------------------

    print(
        "\n========== PROCESSING ISSUES =========="
    )

    successful = 0
    failed = 0

    for issue in issues:

        try:

            success = run_pipeline_for_issue(
                issue
            )

            if success:

                successful += 1

            else:

                failed += 1

        except Exception as e:

            failed += 1

            print(
                f"\n❌ Unexpected error "
                f"while processing "
                f"{issue['key']}:"
            )

            print(
                str(e)
            )

    # --------------------------------------------------------
    # 3. Final summary
    # --------------------------------------------------------

    print(
        "\n=================================================="
    )

    print(
        "AUTOMATION SUMMARY"
    )

    print(
        "=================================================="
    )

    print(
        f"Eligible issues: "
        f"{len(issues)}"
    )

    print(
        f"Successful: "
        f"{successful}"
    )

    print(
        f"Failed: "
        f"{failed}"
    )

    print(
        "=================================================="
    )

def transition_issue(issue_key, target_status_names):
    """Transitions an issue to the first matching status name found
    in its available transitions. target_status_names is a priority
    list, since workflow status names vary by Jira instance."""
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/transitions"
    response = requests.get(url, headers=headers, auth=auth, timeout=30)
    if response.status_code != 200:
        print(f"⚠️ Could not retrieve transitions for {issue_key}.")
        return False

    transitions = response.json().get("transitions", [])
    available = {t["name"].lower(): t["id"] for t in transitions}

    for target in target_status_names:
        if target.lower() in available:
            payload = {"transition": {"id": available[target.lower()]}}
            res = requests.post(url, json=payload, headers=headers, auth=auth, timeout=30)
            if res.status_code in [200, 204]:
                print(f"✅ {issue_key} moved to '{target}'.")
                return True

    print(f"⚠️ None of {target_status_names} found as a valid transition for {issue_key}. "
          f"Available: {[t['name'] for t in transitions]}")
    return False


def add_jira_delivery_comment(issue_key, project_uid, phrase_url_base="https://cloud.memsource.com/web"):
    """Posts a translation-delivered notice as a Jira comment."""
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/comment"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    adf_comment = {
        "body": {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": "✅ Translation Delivered"}]
                },
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "All jobs for this project have been marked "},
                        {"type": "text", "text": "DELIVERED", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": " in Phrase. This issue has been closed automatically."}
                    ]
                },
            ]
        }
    }

    response = requests.post(url, json=adf_comment, headers=headers, auth=auth, timeout=30)
    if response.status_code in [200, 201]:
        print(f"✅ Posted delivery comment on {issue_key}")
        return True

    print(f"⚠️ Failed to post delivery comment ({response.status_code}): {response.text}")
    return False

def download_phrase_target_file(token, project_uid, job_uid):
    """Downloads the completed translation file from Phrase.
    Uses Phrase's simple synchronous v1 endpoint — Phrase has deprecated
    it in favor of an async v2 flow, but v1 remains functional and is
    appropriate for this project's scale. A production system handling
    very large files would use the async v2 version instead."""
    url = f"{PHRASE_BASE_URL}/v1/projects/{project_uid}/jobs/{job_uid}/targetFile"
    headers = {"Authorization": f"ApiToken {token}"}

    response = requests.get(url, headers=headers, timeout=60)
    if response.status_code != 200:
        print(f"⚠️ Failed to download target file: {response.status_code} - {response.text}")
        return None, None

    content_disposition = response.headers.get("Content-Disposition", "")
    filename = "translated_file.xlsx"
    if "filename=" in content_disposition:
        filename = content_disposition.split("filename=")[-1].strip('"; ')

    return filename, response.content

def upload_file_to_jira_issue(issue_key, filename, file_content):
    """Attaches a file (e.g., a completed translation from Phrase) to a Jira issue."""
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/attachments"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {
        "X-Atlassian-Token": "no-check",  # required by Jira for attachment uploads, or the request is rejected
        "Accept": "application/json",
    }
    files = {"file": (filename, file_content)}

    response = requests.post(url, headers=headers, auth=auth, files=files, timeout=60)

    if response.status_code in [200, 201]:
        print(f"✅ Attached '{filename}' to {issue_key}")
        return True

    print(f"⚠️ Failed to attach file to {issue_key}: {response.status_code} - {response.text}")
    return False
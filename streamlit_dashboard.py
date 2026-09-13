"""
Localization Automation Dashboard
Reads from the same localization_automation.db your pipeline writes to,
via the existing query functions in historical_reports.py.

Run with: streamlit run streamlit_dashboard.py
"""

import json
import streamlit as st
import pandas as pd
import plotly.express as px

import historical_reports as hr

st.set_page_config(
    page_title="Localization Automation Dashboard",
    page_icon="🌐",
    layout="wide",
)
st.markdown("""
<style>
    .block-container {
        max-width: 95% !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        padding-top: 2rem !important;
    }
    [data-testid="stMetricValue"] { font-size: 2.1rem !important; }
    [data-testid="stMetricLabel"] { font-size: 1.05rem !important; }
    .stMarkdown, .stCaption, p, li { font-size: 1.05rem !important; }
    div[data-testid="stDataFrame"] * { font-size: 1.0rem !important; }
    h1 { font-size: 2.4rem !important; }
    h2 { font-size: 1.6rem !important; }
    h3 { font-size: 1.3rem !important; }
</style>
""", unsafe_allow_html=True)

st.title("🌐 Localization Automation Dashboard")
st.caption("Jira → Phrase → AI/MT hybrid pipeline — live view of processing history and cost")

if st.button("🔄 Refresh data"):
    st.cache_data.clear()

overview_tab, history_tab = st.tabs(["📊 Overview", "📋 Job History"])

# ============================================================
# Cached data loaders — thin wrappers around historical_reports.py
# ============================================================

@st.cache_data(ttl=30)
def load_summary():
    return hr.get_job_summary()

@st.cache_data(ttl=30)
def load_ai_mt_summary():
    return hr.get_ai_mt_summary()

@st.cache_data(ttl=30)
def load_volume():
    return hr.get_historical_volume()

@st.cache_data(ttl=30)
def load_cost():
    return hr.get_historical_cost()

@st.cache_data(ttl=30)
def load_job_history(status=None, issue_key=None, limit=200):
    return hr.get_job_history(status=status, issue_key=issue_key, limit=limit)

@st.cache_data(ttl=30)
def load_job_events(issue_key=None, attachment_id=None):
    return hr.get_job_events(issue_key=issue_key, attachment_id=attachment_id)


def format_target_langs(value):
    """target_langs is stored as a JSON string or plain string — normalize for display."""
    if not value:
        return "N/A"
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return ", ".join(parsed).upper()
        return str(parsed).upper()
    except (TypeError, json.JSONDecodeError):
        return str(value).upper()

STATUS_LABELS = {
    "completed": "📤 Uploaded to Phrase (Awaiting Translation)",
    "ai_mt_processed": "🤖 AI/MT Processed",
    "jira_updated": "📝 Jira Updated",
    "completed_delivered": "✅ Delivered",
    "failed": "❌ Failed",
}

def display_status(raw_status):
    """Maps internal pipeline status codes to human-readable labels
    for display — the underlying DB values stay untouched, since
    completion_checker.py and other logic depend on the raw strings."""
    if not raw_status:
        return "Unknown"
    return STATUS_LABELS.get(raw_status, raw_status.replace("_", " ").title())
# ============================================================
# OVERVIEW TAB
# ============================================================

with overview_tab:
    summary = load_summary()
    ai_mt = load_ai_mt_summary()

    st.subheader("Key Metrics")
    kpi_cols = st.columns(6)
    kpi_cols[0].metric("Total Jobs", summary["total_jobs"])
    kpi_cols[1].metric("📤 Uploaded to Phrase", summary["uploaded_jobs"])
    kpi_cols[2].metric("✅ Delivered", summary["delivered_jobs"])
    kpi_cols[3].metric("Failed", summary["failed_jobs"])
    kpi_cols[4].metric("Total Words", f"{summary['total_words']:,}")
    kpi_cols[5].metric("Est. Total Cost", f"${summary['total_estimated_cost']:,.2f}")

    st.divider()

    left_col, right_col = st.columns(2)

    # --- AI/MT routing split ---
    with left_col:
        st.subheader("AI/MT Routing Split")
        total_ai = ai_mt["total_ai_suitable"]
        total_human = ai_mt["total_human_required"]

        if total_ai + total_human == 0:
            st.info("No AI/MT routing data available yet — this metric only populates for jobs "
                    "processed after the AI/MT layer's tracking columns were added.")
        else:
            routing_df = pd.DataFrame({
                "Route": ["AI-suitable", "Human-required"],
                "Strings": [total_ai, total_human],
            })
            fig = px.pie(routing_df, names="Route", values="Strings", hole=0.45,
                         color="Route",
                         color_discrete_map={"AI-suitable": "#2E86AB", "Human-required": "#E76F51"})
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

            st.metric("Est. AI/MT Cost (DeepL + Claude)", f"${ai_mt['total_ai_mt_cost']:.4f}")
            st.caption(f"AI/MT tracking data available for {ai_mt['jobs_with_ai_mt_data']} "
                       f"of {summary['total_jobs']} jobs — earlier jobs predate this metric.")

    # --- Cost by currency ---
    with right_col:
        st.subheader("Cost Breakdown")
        cost_rows = load_cost()

        if not cost_rows:
            st.info("No cost data available yet.")
        else:
            cost_df = pd.DataFrame(cost_rows)
            fig = px.bar(
                cost_df, x="currency",
                y=["translation_cost", "lqa_cost"],
                barmode="stack",
                labels={"value": "Cost", "currency": "Currency", "variable": "Cost Type"},
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # --- Volume by language pair ---
    st.subheader("Volume by Language Pair")
    volume_rows = load_volume()

    if not volume_rows:
        st.info("No volume data available yet.")
    else:
        volume_df = pd.DataFrame(volume_rows)
        volume_df["language_pair"] = volume_df.apply(
            lambda r: f"{(r['source_lang'] or '?').upper()} → {format_target_langs(r['target_langs'])}",
            axis=1,
        )
        fig = px.bar(
            volume_df.sort_values("total_words", ascending=True),
            x="total_words", y="language_pair", orientation="h",
            labels={"total_words": "Total Words", "language_pair": "Language Pair"},
            text="job_count",
        )
        fig.update_traces(texttemplate="%{text} job(s)", textposition="outside")
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

    # --- Status breakdown ---
    st.subheader("Job Status Breakdown")
    all_jobs = load_job_history(limit=500)

    if not all_jobs:
        st.info("No jobs processed yet.")
    else:
        status_df = pd.DataFrame(all_jobs)
        status_df["status_label"] = status_df["status"].apply(display_status)  # NEW
        status_counts = status_df["status_label"].value_counts().reset_index()  # CHANGED — was: status_df["status"]
        status_counts.columns = ["status", "count"]
        fig = px.bar(status_counts, x="status", y="count", labels={"count": "Jobs", "status": "Status"})
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)


# ============================================================
# JOB HISTORY TAB
# ============================================================

with history_tab:
    st.subheader("Job History")

    filter_cols = st.columns([2, 2, 1])
    with filter_cols[0]:
        status_display_options = ["All"] + list(STATUS_LABELS.values())
        status_label_selected = st.selectbox("Filter by status", status_display_options)  # CHANGED — was: st.text_input
        reverse_status_map = {v: k for k, v in STATUS_LABELS.items()}
        status_filter = reverse_status_map.get(status_label_selected)  # None if "All" selected
    with filter_cols[1]:
        issue_filter = st.text_input("Filter by issue key (optional)", "")
    with filter_cols[2]:
        limit = st.number_input("Max rows", min_value=10, max_value=1000, value=200, step=10)

    jobs = load_job_history(
        status=status_filter,  # CHANGED — was: status_filter.strip() or None
        issue_key=issue_filter.strip() or None,
        limit=int(limit),
    )

    if not jobs:
        st.warning("No jobs match these filters.")
    else:
        df = pd.DataFrame(jobs)
        df["target_langs"] = df["target_langs"].apply(format_target_langs)
        df["source_lang"] = df["source_lang"].fillna("?").str.upper()
        df["status"] = df["status"].apply(display_status)

        display_cols = [
            "issue_key", "status", "filename", "source_lang", "target_langs",
            "word_count", "estimated_total_cost", "currency",
            "ai_suitable_count", "human_required_count", "ai_mt_cost",
            "created_at", "completed_at",
        ]
        display_cols = [c for c in display_cols if c in df.columns]

        st.dataframe(
            df[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "estimated_total_cost": st.column_config.NumberColumn("Est. Cost", format="$%.2f"),
                "ai_mt_cost": st.column_config.NumberColumn("AI/MT Cost", format="$%.4f"),
                "word_count": st.column_config.NumberColumn("Words", format="%d"),
            },
        )

        st.divider()
        st.subheader("Job Event Timeline")
        st.caption("Select an issue above to inspect its full processing history.")

        issue_options = sorted(df["issue_key"].unique().tolist())
        selected_issue = st.selectbox("Issue key", options=["—"] + issue_options)

        if selected_issue != "—":
            events = load_job_events(issue_key=selected_issue)
            if not events:
                st.info(f"No event log found for {selected_issue}.")
            else:
                for event in events:
                    st.markdown(
                        f"**{event['created_at']}** — `{event['event_type']}`"
                        + (f" ({event['status']})" if event["status"] else "")
                    )
                    if event["details"]:
                        st.caption(event["details"])
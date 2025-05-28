import sys

try:
    import pysqlite3
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    # Fall back to built-in sqlite3 on Windows or if pysqlite3 is not installed
    import sqlite3
else:
    # Now sqlite3 refers to pysqlite3
    import sqlite3

import streamlit as st
import threading
import re
import os
from streamlit_crew import (
    generate_individual_case_summary,
    generate_report_for_forms,
    generate_summary_of_all_issues,
    generate_report_from_prompt,
    generate_and_execute_sql,
    generate_and_execute_sql_prompt,
    execute_case_closing,
    execute_case_escalation,
    generate_case_summary_for_email,
    send_escalation_email
)
from Sales.telegram_app import run_telegram_bot


TELEGRAM_BOT_TOKEN = os.getenv("SALES_TELE_BOT")

schema = """
Tables:
- flock_farm_information(id, case_id, type_of_chicken, age_of_chicken, housing_type, number_of_affected_flocks_houses, timestamp)
- symptoms_performance_data(id, case_id, main_symptoms, daily_production_performance, pattern_of_spread_or_drop, timestamp)
- medical_diagnostic_records(id, case_id, vaccination_history, lab_data, pathology_findings_necropsy, current_treatment, timestamp)
- issues(id, title, description, farm_name, status, close_reason, assigned_team, case_id, created_at, updated_at)
- farmer_problem(id, case_id, problem_description, timestamp)
- issue_attachments(id, case_id, file_name, file_path, uploaded_at)
"""

known_filters = {
    "case_id": ["case", "case id", "case_id", "case number"],
    "farm_name": ["farm", "farm name", "farm_name"],
    "status": ["status", "case status", "case_status"],
    "type_of_chicken": ["type of chicken", "chicken type", "chicken_type"],
    "age_of_chicken": ["age of chicken", "chicken age", "chicken_age"],
    "housing_type": ["housing type", "housing_type"],
    "number_of_affected_flocks_houses": ["number of affected flocks", "affected flocks", "affected houses", "number_of_affected_flocks_houses"],
    "vaccination_history": ["vaccination history", "vaccination", "vaccination_history"],
    "lab_data": ["lab data", "lab results", "lab_data"],
    "pathology_findings_necropsy": ["pathology findings", "necropsy findings", "pathology_findings_necropsy"],
    "current_treatment": ["current treatment", "treatment", "current_treatment"],
    "main_symptoms": ["main symptoms", "symptoms", "main_symptoms"],
    "daily_production_performance": ["daily production performance", "production performance", "daily_production_performance"],
    "pattern_of_spread_or_drop": ["pattern of spread", "spread pattern", "pattern_of_spread_or_drop"],
}

def extract_filters(prompt: str) -> dict:
    filters = {}
    prompt = prompt.strip().lower()  # Normalize casing for better matching

    # ✅ Detect 8-character case_id (hex)
    case_id_match = re.search(r"\b[a-f0-9]{8}\b", prompt)
    if case_id_match:
        filters["case_id"] = case_id_match.group(0)

    # ✅ NULL intent detection
    null_phrases = [
        r"(no|without|not have|has no|lacks)\s+([a-z_ ]+)"
    ]
    for pattern in null_phrases:
        null_matches = re.findall(pattern, prompt)
        for _, field_phrase in null_matches:
            for key, aliases in known_filters.items():
                if any(alias in field_phrase for alias in aliases):
                    filters[key] = "__NULL__"

    # ✅ Other filters by alias
    for key, aliases in known_filters.items():
        if key in filters:
            continue  # Already matched

        for alias in aliases:
            if alias.strip() == "case":  # Too generic, skip
                continue

            # Match: field is value / field: value / field = value
            pattern = fr"\b{re.escape(alias)}\b\s*(?:is|=|:)?\s*(['\w \-]+)"
            match = re.search(pattern, prompt)
            if match:
                value = match.group(1).strip()
                filters[key] = "__NULL__" if value.lower() in ["null", "none"] else value
                break

    return filters

st.set_page_config(page_title="Poultry Case Reporting", layout="wide")

st.title("🐔 Poultry Case Reporting Dashboard")

# Sidebar navigation
st.sidebar.header("Navigation")
main_action = st.sidebar.radio(
    "Select Action:",
    ["Generate Report", "Close Case", "Escalate Case"]
)

if main_action == "Generate Report":
    report_type = st.sidebar.selectbox("Choose report type:", ["Dynamic Report", "Standard Report"])

    # Dynamic Report
    if report_type == "Dynamic Report":
        st.subheader("Dynamic Report")
        prompt = st.text_area("Enter your prompt here:")
        if st.button("Generate Report"):
            if prompt:
                filters = extract_filters(prompt)
                case_id = filters.get("case_id", None)

                if case_id and len(case_id) != 8:
                    st.warning("Detected case ID is not exactly 8 characters. Please provide the first 8 characters of the case UUID.")

                with st.spinner("Generating report..."):
                    execution_result = generate_and_execute_sql_prompt(
                        schema=schema,
                        user_input=prompt,
                        filters=filters
                    )
                    result = generate_report_from_prompt(execution_result, filters)
                    st.success("Report Generated.")
                    st.markdown(result)
            else:
                st.warning("Please enter a prompt.")

    # Standard Report
    elif report_type == "Standard Report":
        st.subheader("Standard Report")
        standard_option = st.selectbox("Select standard report type:", [
            "Generate Individual Case Summary",
            "Generate Full Case Report",
            "Summarize All Issues"
        ])

        case_id_input = None
        if standard_option in ["Generate Individual Case Summary", "Generate Full Case Report"]:
            case_id_input = st.text_input("Enter Case ID")

        if standard_option == "Generate Individual Case Summary":
            if st.button("Generate Summary"):
                if case_id_input:
                    with st.spinner("Generating case summary..."):
                        result = generate_individual_case_summary(case_id_input)
                        st.success("Summary Generated.")
                        st.text(result)
                else:
                    st.warning("Please enter a valid case ID.")

        elif standard_option == "Generate Full Case Report":
            if st.button("Generate Full Report"):
                if case_id_input:
                    with st.spinner("Generating full report..."):
                        result = generate_report_for_forms(case_id_input)
                        st.success("Full Report Generated.")
                        st.text(result)
                else:
                    st.warning("Please enter a valid case ID.")

        elif standard_option == "Summarize All Issues":
            if st.button("Generate Summary of All Issues"):
                with st.spinner("Generating issue summary..."):
                    result = generate_summary_of_all_issues()
                    st.success("Issues Summary Generated.")
                    st.markdown(result)

elif main_action == "Close Case":
    st.subheader("Close a Case")
    case_id_to_close = st.text_input("Enter Case ID")
    close_reason = st.text_input("Enter Reason for Closing")

    if st.button("Close Case"):
        if case_id_to_close and close_reason:
            result = execute_case_closing(case_id_to_close, close_reason)
            with st.spinner("Closing case..."):
                st.success(f"Case {case_id_to_close} successfully closed.")
        else:
            st.warning("Please enter both a valid Case ID and a close reason.")

elif main_action == "Escalate Case":
    st.subheader("Escalate a Case")
    case_id_to_escalate = st.text_input("Enter Case ID to Escalate")
    escalation_reason = st.text_input("Enter Reason for Escalation")

    if st.button("Escalate Case"):
        if case_id_to_escalate and escalation_reason:
            case_info = generate_case_summary_for_email(case_id_to_escalate)
            success = send_escalation_email(case_id_to_escalate, escalation_reason, case_info)
            result = execute_case_escalation(case_id_to_escalate)
            with st.spinner("Escalating case..."):
                st.success(f"Case {case_id_to_escalate} successfully escalated.")
        else:
            st.warning("Please enter both a valid Case ID and an escalation reason.")

st.sidebar.markdown("---")

if "bot_started" not in st.session_state:
    st.session_state.bot_started = False

if st.sidebar.button("🤖 Activate Telegram Bot"):
    if not st.session_state.bot_started:
        st.session_state.bot_started = True
        threading.Thread(target=run_telegram_bot, daemon=True).start()
        st.sidebar.success("✅ Telegram Bot started.")
    else:
        st.sidebar.info("ℹ️ Bot is already running.")
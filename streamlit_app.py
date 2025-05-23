__import__('pysqlite3')
import sys
sys.modules['sqlite3'] = sys.modules['pysqlite3']
import streamlit as st
import re
from streamlit_crew import (
    generate_individual_case_summary,
    generate_report_for_forms,
    generate_summary_of_all_issues,
    generate_report_from_prompt,
    generate_and_execute_sql,
    execute_case_closing
)

schema = """
Tables:
- flock_farm_information(id, case_id, type_of_chicken, age_of_chicken, housing_type, number_of_affected_flocks_houses, timestamp)
- symptoms_performance_data(id, case_id, main_symptoms, daily_production_performance, pattern_of_spread_or_drop, timestamp)
- medical_diagnostic_records(id, case_id, vaccination_history, lab_data, pathology_findings_necropsy, current_treatment, timestamp)
- issues(id, title, description, farm_name, status, close_reason, assigned_team, case_id, created_at, updated_at)
- farmer_problem(id, case_id, problem_description, timestamp)
- notifications(id, recipient_team, message, sent_at)
- issue_attachments(id, case_id, file_name, file_path, uploaded_at)
"""

st.set_page_config(page_title="Poultry Case Reporting", layout="wide")

st.title("🐔 Poultry Case Reporting Dashboard")

# Sidebar navigation
st.sidebar.header("Navigation")
main_action = st.sidebar.radio(
    "Select Action:",
    ["Generate Report", "Close Case"]
)

if main_action == "Generate Report":
    report_type = st.sidebar.selectbox("Choose report type:", ["Dynamic Report", "Standard Report"])

    # Dynamic Report
    if report_type == "Dynamic Report":
        st.subheader("Dynamic Report")
        prompt = st.text_area("Enter your prompt here:")
        if st.button("Generate Report"):
            if prompt:
                case_match = re.search(r"\bcase(?:[\s_]*id)?[:\s#]*?([0-9a-fA-F]{8})\b", prompt, re.IGNORECASE)
                case_id = case_match.group(1) if case_match else None

                if case_id and len(case_id) != 8:
                    st.warning("Detected case ID is not exactly 8 characters. Please provide the first 8 characters of the case UUID.")

                with st.spinner("Generating report..."):
                    execution_result = generate_and_execute_sql(
                        schema=schema,
                        user_input=prompt,
                        case_id=case_id
                    )
                    result = generate_report_from_prompt(execution_result, case_id=case_id)
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
    case_id_to_close = st.number_input("Enter Case ID to close", min_value=1, step=1)
    close_reason = st.text_input("Enter Reason for Closing")

    if st.button("Close Case"):
        if case_id_to_close and close_reason:
            result = execute_case_closing(case_id_to_close, close_reason)
            with st.spinner("Closing case..."):
                st.success(f"Case {case_id_to_close} successfully closed.")
        else:
            st.warning("Please enter both a valid Case ID and a close reason.")
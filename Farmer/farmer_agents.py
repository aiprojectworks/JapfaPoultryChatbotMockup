# crew_sql_agent.py
from crewai import Agent, Task, Crew
from crewai.telemetry import Telemetry
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
from IPython.display import Markdown
from typing import Dict, Any
import sqlite3
import os
import re
import ast
import textwrap
import inspect
from datetime import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Load API key from .env
load_dotenv()
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
# st.environ["OPENAI_API_KEY"] = st.getenv("OPENAI_API_KEY")
# os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"

EMAIL_PASSKEY = os.getenv("EMAIL_PASSKEY")

# telemetry connection fix
def noop(*args, **kwargs):
    # with open("./logfile.txt", "a") as f:
    #     f.write("Telemetry method called and noop'd\n")
    pass
for attr in dir(Telemetry):
    if callable(getattr(Telemetry, attr)) and not attr.startswith("__"):
        setattr(Telemetry, attr, noop)
        
# =================================================================================================================
        
intent_dict = {
    "get_fully_completed_forms_by_case_id": "Select all fields from each form table to determine if the form linked to the given case_id is fully completed.",
    
    "handle_resume_decision": "Delete an incomplete form entry from its respective table using the provided case_id.",
    
    "confirm_save_biosecurity": "Insert or update a biosecurity_form entry for the given user and case_id with the latest field values from the session.",
    "confirm_save_mortality": "Insert or update a mortality_form entry for the given user and case_id with the latest field values from the session.",
    "confirm_save_health_status": "Insert or update a health_status_form entry for the given user and case_id, including the optional image path if provided.",
    
    "get_forms_by_case_id": "Check whether each form table contains any row with the given case_id to determine form completion status.",
    
    "cancel_entry": "Select fields from each form table to verify if a form is fully filled for the given case_id.",
    
    "cancel_confirmed": "Delete all entries from biosecurity_form, mortality_form, and health_status_form where case_id matches the session's active case.",
    
    "load_incomplete_data": "Select the latest case_id submitted by the given user_id across all form tables, regardless of whether the form is fully completed, by combining and ordering all timestamps.",
    
    "is_form_incomplete": "Select all relevant fields (including image_path if present) from the latest entry in all the form tables for the given user and case_id. Do not exclude rows based on null or empty values. Order results by timestamp descending and limit to 1 row.",
    
    "fallback_resume_prompt_check": "Check if each form table has at least one row for the given case_id to decide whether to show a resume prompt."
}


# =================================================================================================================

# --- 1. Form Definition ---
form_definitions = {
    "flock_farm_information": {
        "Type of Chicken": "What type of chicken is this? (e.g., Layer, Broiler, Breeder)",
        "Age of Chicken": "What is the age of the chicken? (years, round up to nearest whole number)",
        "Housing Type": "What housing type is used? (e.g., Closed House, Opened-Side)",
        "Number of Affected Flocks/Houses": "How many flocks or houses are affected?",
        "Feed Type": "What type of feed is used? (e.g., Complete Feed, Self Mix)",
        "Environment Information": "Describe the environmental conditions (e.g., climate, weather, cage atmosphere, nearby poultry farms)"
    },
    "symptoms_performance_data": {
        "Main Symptoms": "What are the main symptoms or clinical signs observed?",
        "Daily Production Performance": "Provide daily chicken production data (e.g., mortality, %HD, feed intake, egg weight)",
        "Pattern of Spread or Drop": "Describe if there's mortality, production drop, or spreading pattern"
    },
    "medical_diagnostic_records": {
        "Vaccination History": "What is the vaccination history or program followed?",
        "Lab Data": "Provide any lab results or data if available",
        "Pathology Findings (Necropsy)": "List any pathology anatomy changes found during necropsy",
        "Current Treatment": "What treatment is currently being administered?",
        "Management Questions": "List any management-related concerns or questions"
    }
}

form_definitions_types = {
    "flock_farm_information": {
        "Type of Chicken": "TEXT",
        "Age of Chicken": "INTEGER",
        "Housing Type": "TEXT",
        "Number of Affected Flocks/Houses": "INTEGER",
        "Feed Type": "TEXT",
        "Environment Information": "TEXT"
    },
    "symptoms_performance_data": {
        "Main Symptoms": "TEXT",
        "Daily Production Performance": "TEXT",  # Can be JSON or text-encoded data
        "Pattern of Spread or Drop": "TEXT"
    },
    "medical_diagnostic_records": {
        "Vaccination History": "TEXT",
        "Lab Data": "TEXT",
        "Pathology Findings (Necropsy)": "TEXT",
        "Current Treatment": "TEXT",
        "Management Questions": "TEXT"
    }
}

form_validation = {
    "flock_farm_information": {
        "Type of Chicken": lambda x: x.lower() in ["layer", "broiler", "breeder"],
        "Age of Chicken": lambda x: x.isdigit() and 0 < int(x) < 200,
        "Housing Type": lambda x: x.lower() in ["closed house", "opened-side", "open-sided", "open house"],
        "Number of Affected Flocks/Houses": lambda x: x.isdigit() and int(x) >= 0,
        "Feed Type": lambda x: x.lower() in ["complete feed", "self mix"],
        "Environment Information": lambda x: len(x.strip()) > 10
    },
    "symptoms_performance_data": {
        "Main Symptoms": lambda x: len(x.strip()) > 5,
        "Daily Production Performance": lambda x: len(x.strip()) > 5,
        "Pattern of Spread or Drop": lambda x: len(x.strip()) > 5
    },
    "medical_diagnostic_records": {
        "Vaccination History": lambda x: len(x.strip()) > 5,
        "Lab Data": lambda x: len(x.strip()) > 5,
        "Pathology Findings (Necropsy)": lambda x: len(x.strip()) > 5,
        "Current Treatment": lambda x: len(x.strip()) > 5,
        "Management Questions": lambda x: len(x.strip()) > 5
    }
}

forms = []
for key in form_definitions:
  forms.append(key)
  
  
# ======================DB INITIALIZER AGENTS======================

def db_init_agent(form_def_type):
    # Format form def into schema text
    def to_sql_field_name(label):
        # Lowercase and replace non-alphanumeric characters with underscores
        return re.sub(r'\W+', '_', label.strip().lower())

    def format_form_schema_with_types(form_defs_types):
        shared_fields = [
            "- id INTEGER PRIMARY KEY AUTOINCREMENT",
            "- case_id TEXT",
            "- user TEXT",
            "- timestamp DATETIME DEFAULT CURRENT_TIMESTAMP"
        ]

        result = []
        for form_name, fields in form_defs_types.items():
            field_lines = [
                f"- {to_sql_field_name(field)} {sql_type}"
                for field, sql_type in fields.items()
            ]
            form_description = f"Table `{form_name}` has the following fields:\n" + \
                            "\n".join(shared_fields + field_lines)
            result.append(form_description)

        return "\n\n".join(result)

    # --- 3. Define the Agent ---
    sql_create_agent = Agent(
        role="SQL Table Builder",
        goal="Generate SQLite table creation scripts based on form fields",
        backstory="You assist backend engineers by translating structured form definitions into clean SQL schema statements.",
        verbose=True,
        allow_delegation=False,
        llm=ChatOpenAI(model_name="gpt-4o")
    )

    # --- 4. Define the Task ---
    sql_create_task = Task(
        description=f"""You are a backend developer assistant. Generate SQL `CREATE TABLE IF NOT EXISTS` statements for each form below.
        Each table must include:
        - id INTEGER PRIMARY KEY AUTOINCREMENT
        - case_id TEXT
        - user TEXT
        - timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        At the end of each table definition, include this constraint:
        - UNIQUE(case_id, user)
        
        Use `snake_case` for all field names.
        {format_form_schema_with_types(form_def_type)}
        Return only the SQL statements in one markdown code block""",
        expected_output="SQL `CREATE TABLE` statements for each form in a single markdown code block like ```sql ...```",
        agent=sql_create_agent
    )

    # --- 5. Run the Crew ---
    schema_builder_crew = Crew(
        agents=[sql_create_agent],
        tasks=[sql_create_task],
        verbose=False,
        memory=False
    )

    # Execute
    result = schema_builder_crew.kickoff()
    sqlbuilder_output = str(result)

    def extract_sql_block(result_text: str) -> str:
        if result_text.startswith("```sql"):
            return result_text.strip().removeprefix("```sql").removesuffix("```").strip()
        return result_text.strip()

    # Usage:
    clean_sql = extract_sql_block(sqlbuilder_output)
    print(clean_sql)
    return clean_sql

# ================================CREW DYNAMIC SQL GENERATOR=======================================
def dynamic_sql_agent(intent, form_def_type):
    def to_sql_field_name(label):
        # Lowercase and replace non-alphanumeric characters with underscores
        return re.sub(r'\W+', '_', label.strip().lower()).strip('_')

    def format_form_schema_with_types(form_defs_types):
        shared_fields = [
            "- id INTEGER PRIMARY KEY AUTOINCREMENT",
            "- case_id TEXT",
            "- user TEXT",
            "- timestamp DATETIME DEFAULT CURRENT_TIMESTAMP"
        ]

        result = []
        for form_name, fields in form_defs_types.items():
            field_lines = [
                f"- {to_sql_field_name(field)} {sql_type}"
                for field, sql_type in fields.items()
            ]
            form_description = f"Table `{form_name}` has the following fields:\n" + \
                            "\n".join(shared_fields + field_lines)
            result.append(form_description)

        return "\n\n".join(result)

    form_def_text = format_form_schema_with_types(form_def_type)
    print(form_def_text)

    # 4. Create SQL Agent
    sql_agent = Agent(
        name="DynamicSQLAgent",
        role="Database SQL Generator and Validator",
        goal="Generate syntactically correct and secure SQL statements based on natural language instructions and known form schemas.",
        backstory="An AI assistant trained in SQL generation from flexible form structures. Skilled in crafting secure and valid queries dynamically.",
        llm=ChatOpenAI(model_name="gpt-4o")
    )

    # 5. Generate task dynamically
    task_description = f"""
    You are an SQL generation agent. Your job is to generate **parameterized SQL** statements to fulfill the following task:

    --- USER INPUT ---
    "{intent}"
    ------------------

    Instructions:
    - Use the known form schemas listed below.
    - Check all form tables listed.
    - Do not include WHERE clauses that prevent updating when fields are null or empty.
    - Use a parameter placeholder `?` for the `case_id`, not a hardcoded value.
    - The final output should be in json format with all the forms as the key and the sql statements as the value
    - Respond with a single unified input only if specified in the user input
    - Do NOT return explanations, only the SQL queries.
    - Return the output in **JSON format** with keys as table names and values as SQL strings.
    - Use lowercase snake_case field names exactly as defined in the schema (not display labels).
    - CONFLICT(case_id, user) can be used. Do not add any WHERE clause to the ON CONFLICT part. Always update the fields regardless of NULL or empty.

    --- FORM SCHEMA ---
    {form_def_text}
    -------------------

    Final Output Format (example):

    ```json
    {{
    "biosecurity_form": "SELECT * FROM biosecurity_form WHERE case_id = ? AND field1 IS NOT NULL AND field1 != '' ...",
    "mortality_form": "...",
    "health_status_form": "..."
    }}
    ```
    OR
    ```json
    {{
    "unified_output": "SELECT case_id, timestamp FROM biosecurity_form WHERE user = ? UNION ALL SELECT case_id, timestamp FROM mortality_form WHERE user = ? ORDER BY timestamp DESC LIMIT 1"
    }}
    """

    # 6. Create and run Crew
    generateDynamicSql = Task(
        description=task_description,
        agent=sql_agent,
        expected_output="A JSON dictionary mapping table names to SQL SELECT queries, parameterized with `?`, filtering by case_id and excluding null/empty fields."
    )

    crew = Crew(
        agents=[sql_agent],
        tasks=[generateDynamicSql],
        verbose=True
    )

    result = crew.kickoff()
    
    def extract_sql_block(result_text: str) -> str:
        if result_text.startswith("```json"):
            return result_text.strip().removeprefix("```json").removesuffix("```").strip()
        return result_text.strip()
    
    return(extract_sql_block(str(result)))

# agent_result = ast.literal_eval(dynamic_sql_agent(intent_dict["confirm_save_biosecurity"]))
# print(agent_result)
# completed = {}
# print("\n========================\n")
# for form in forms:
#     completed[form] = False
#     print("\n========================\n")
#     if not agent_result.get("unified_output"):
#         print(agent_result[form])
#     else:
#         print(agent_result['unified_output'])

  
# print("\n========================\n")
# print(completed)

# if not agent_result.get("unified_output"):
#     print(agent_result[form])
# else:
#     raise Exception


# ================================CREW SQL VALIDATOR=======================================

def describe_validation_for_question(question, form_validation):
    for form_fields in form_validation.values():
        if question in form_fields:
            validator = form_fields[question]
            try:
                src = textwrap.dedent(inspect.getsource(validator)).strip()
                # Numeric rules
                if "x.isdigit()" in src and "int(x)" in src:
                    rules = re.findall(r"int\(x\)\s*([<>]=?)\s*(\d+)", src)
                    if rules:
                        parts = []
                        for op, val in rules:
                            op_map = {
                                ">": f"more than {val}",
                                ">=": f"at least {val}",
                                "<": f"less than {val}",
                                "<=": f"at most {val}"
                            }
                            parts.append(op_map.get(op, f"{op} {val}"))
                        return f"Must be a number {' and '.join(parts)}."
                    return "Must be a valid number."
                # Choice-based rules
                elif "x.lower()" in src and "in" in src:
                    options = re.findall(r"\[([^\]]+)\]", src)
                    if options:
                        opts = [o.strip().strip("'\"") for o in options[0].split(",")]
                        return f"Must be one of the following: {', '.join(opts)}."
                # Text length rules
                elif "len(x.strip())" in src:
                    rules = re.findall(r"len\(x\.strip\(\)\)\s*([<>]=?)\s*(\d+)", src)
                    if rules:
                        parts = []
                        for op, val in rules:
                            op_map = {
                                ">": f"more than {val} characters",
                                ">=": f"at least {val} characters",
                                "<": f"less than {val} characters",
                                "<=": f"at most {val} characters"
                            }
                            parts.append(op_map.get(op, f"{op} {val} characters"))
                        return f"Must be {' and '.join(parts)}."
                return "Unrecognized validation rule."
            except Exception as e:
                return f"Could not extract rule due to error: {e}"
    return "No validation rule found for this question."


def validation_agent(question: str, answer: str, form_def: dict, form_val: dict):
    prompt_question = form_def.get(question, question)
    validation_description = describe_validation_for_question(question, form_val)

    validator_agent = Agent(
        role="Answer Validator",
        goal="Ensure user answers are valid, complete, and relevant to the question and rules.",
        backstory="You are a quality control assistant helping validate data entry in a form-based system.",
        verbose=True,
        allow_delegation=False,
        llm=ChatOpenAI(model_name="gpt-4o")
    )
    
    # This used to be part of validator_task to include the context of the base validation, removed due to not detecting right amt of characters
        # The base validation rule for this question is:
        # - {validation_description}

    validator_task = Task(
        description=f"""
        You are a form data validation assistant.

        The user answered the following question:
        - **Question**: {prompt_question}
        - **Answer**: {answer}

        Please check if the answer is:
        1. Related to the question
        2. Not suspicious, empty, or illogical
        
        It is okay to let the answer be valid if there are mispellings and typographical errors whilst adhering to the above criteria
        **Mispellings and typographical errors should not dictate if a answer is invalid**

        Return exactly one of:
        - ✅ Valid
        - ⚠️ Invalid: followed by reason
        - ❌ Suspicious: followed by reason
        """,
        expected_output="A single-line output in the format: status + reason.",
        agent=validator_agent
    )

    result = Crew(agents=[validator_agent], tasks=[validator_task], verbose=False, memory=False).kickoff()
    result = str(result).strip()

    if result.startswith("✅"):
        return "✅", ""
    elif result.startswith("⚠️") or result.startswith("❌"):
        parts = result.split(":", 1)
        return parts[0].strip(), parts[1].strip() if len(parts) > 1 else "Unclear reason"
    else:
        return "⚠️", "Invalid format returned from validator"

def error_message_agent(question: str, answer: str, form_def: dict, form_val: dict, validator_reason: str):
    prompt_question = form_def.get(question, question)
    validation_description = describe_validation_for_question(question, form_val)

    error_response_agent = Agent(
        role="Error Message Composer",
        goal="Generate helpful error messages for invalid user input.",
        backstory="You help users correct their answers with clear, friendly guidance.",
        verbose=True,
        allow_delegation=False,
        llm=ChatOpenAI(model_name="gpt-4o")
    )

#        - **Validation Rule**: {validation_description}

    error_message_task = Task(
        description=f"""
        The user answered a form question, but their input was rejected.

        - **Question**: {prompt_question}
        - **Answer**: {answer}
        - **Validator's Reason**: {validator_reason}

        Your job:
        - Write a friendly and helpful error message.
        - Rephrase the validator's reason for clarity if needed.
        - Suggest how to fix the answer, with examples if useful.
        - If the answer was fine, return 'valid'

        Example output: "Your answer is too short. Please enter more information like: 'fever, nasal discharge'"
        """,
        expected_output="A friendly error message or 'valid'.",
        agent=error_response_agent
    )

    result = Crew(agents=[error_response_agent], tasks=[error_message_task], verbose=False, memory=False).kickoff()
    return str(result).strip()

def spelling_correction_agent(text: str, question: str = "") -> str:
    spell_checker = Agent(
        role="Spelling Correction Assistant",
        goal="Check and optionally correct spelling errors in user inputs, only when confident.",
        backstory="You help users by checking their answers for spelling mistakes and correcting them only if you are confident. If unsure, you return 'valid'.",
        verbose=False,
        allow_delegation=False,
        llm=ChatOpenAI(model_name="gpt-4o")
    )

    check_task = Task(
        description=f"""
        Check if the following user input contains obvious spelling mistakes.
        
        - Question context: "{question}"
        - User's input: "{text}"
        
        Instructions:
        1. If the text contains **clear and confident** spelling errors, return the corrected version.
           - Format: Corrected text only (e.g., "broiler" instead of "broiller")
        2. If you are not confident, return exactly: **valid**
        3. Do not explain your answer or ask the user. Just fix if sure, else ignore.
        
        Examples:
        - Input: "broiller" → Output: "broiler"
        - Input: "closed house" → Output: "closed house"
        - Input: "no data/" → Output: "no data"
        - Input: "typo maybe?" (unsure) → Output: valid
        """,
        expected_output="Either 'valid' or the corrected string.",
        agent=spell_checker
    )

    crew = Crew(
        agents=[spell_checker],
        tasks=[check_task],
        verbose=False,
        memory=False
    )

    result = crew.kickoff()
    return str(result).strip()

# Orchestrator function
def data_validator_agent(question: str, answer: str, form_def: dict, form_val: dict):
    # Step 1: Validate answer and capture reasoning
    status, reason = validation_agent(question, answer, form_def, form_val)

    if status == "✅":
        # Step 2: Spell-check
        corrected = spelling_correction_agent(answer, question=question)
        if corrected != "valid" and corrected != answer:
            return "✅ Valid (autocorrected)", corrected
        else:
            return "✅ Valid", answer
    else:
        # Step 3: Generate error message using validator's reason
        error_message = error_message_agent(question, answer, form_def, form_val, validator_reason=reason)
        return f"{status} {reason}", error_message
    
    
def uncompleted_message_maker_agent():
    """
    Help dynamically make a message for user to let them know what else is missing when they click on submit
    OR
    Help dynamically make code to show buttons for the remaining fields that are not done 
    [OR the code for showing buttons of remaining fields to be inputted (if below 5-7 fields then show, if above then normal choose form to input will be shown)
    can be hardcoded and only the output from is_all_form_data_complete is used to generate the missing message]
    """
    return True

def case_summary_agent(form_responses: dict) -> str:
    """
    Takes in all completed form data (structured by form name → question → answer)
    and returns a structured summary of the case:
    - Human-readable summary of submitted data
    - Inferred issues (cautiously worded)
    - Suggested next steps
    """
    # Flatten answers into readable string
    context_lines = []
    for form_name, fields in form_responses.items():
        context_lines.append(f"\n=== {form_name.replace('_', ' ').title()} ===")
        for question, answer in fields.items():
            context_lines.append(f"- {question}: {answer}")
    context_block = "\n".join(context_lines)

    summary_agent = Agent(
        role="Veterinary Case Summarizer",
        goal="Summarize poultry health forms and suggest potential concerns for review",
        backstory=(
            "You're an AI vet assistant helping summarize chicken health data from structured forms. "
            "You highlight potential issues based on symptoms and conditions, but avoid making hard diagnoses."
        ),
        verbose=False,
        allow_delegation=False,
        llm=ChatOpenAI(model_name="gpt-4o")
    )

    task_description = f"""
    You will analyze structured form data submitted by a farmer regarding a chicken health issue.
    
    Your output should be formatted clearly and professionally in **3 sections**:

    **AI-Inferred Preliminary Summary (For Review Only)**
    This summary is automatically generated by AI and is meant for sales team reference only.
    It should NOT be taken as a final diagnosis and should be confirmed by a qualified technical team if needed.

    1. **Summary of Data Provided**:
       Summarize the environment, flock profile, symptoms, lab/treatment history, etc.

    2. **Possible Issue(s)**:
       Suggest potential issues based on the data (e.g., IBV, feed-related problems).
       Use cautious language like "may suggest", "could be related to", "consistent with", "warrants investigation".

    3. **Recommended Actions**:
       Recommend 1–3 next steps (e.g., lab test, vaccine review, change in feed).
       Be practical. Avoid speculative advice.

    --- FORM DATA ---
    {context_block}
    ------------------

    Keep the full response under 300 words.
    Do NOT use emojis or icons anywhere.
    Return only the 3 sections clearly labeled.
    """

    summary_task = Task(
        description=task_description,
        expected_output="A structured AI-generated report with Summary, Issues, and Recommendations.",
        agent=summary_agent
    )

    crew = Crew(agents=[summary_agent], tasks=[summary_task], verbose=True, memory=False)
    result = crew.kickoff()
    return str(result).strip()

def email_generator(summary_text: str, form_responses: dict, user_name: str = "Unknown User") -> str:
    """
    Constructs a full HTML email body for sending case reports to the sales team.
    - summary_text: output from `case_summary_agent()`
    - form_responses: from Telegram user_session_data[user_id]["forms"]
    - user_name: string to personalize who submitted the case
    """
    # Format form responses into an HTML table
    form_tables_html = ""
    for form_name, fields in form_responses.items():
        form_tables_html += f"""
        <div style="margin-top: 15px;">
            <h4 style="color:#333; background-color:#f2f2f2; padding: 10px; border-left: 5px solid #e67e22;">
                {form_name.replace('_', ' ').title()}
            </h4>
            <table style="width:100%; border-collapse: collapse; margin-bottom: 20px;" border="1">
                <thead>
                    <tr style="background-color:#f9f9f9;">
                        <th style="padding: 8px; text-align: left;">Field</th>
                        <th style="padding: 8px; text-align: left;">Answer</th>
                    </tr>
                </thead>
                <tbody>
        """
        for question, answer in fields.items():
            form_tables_html += f"""
                <tr>
                    <td style="padding: 8px;">{question}</td>
                    <td style="padding: 8px;">{answer}</td>
                </tr>
            """
        form_tables_html += """
            </tbody>
            </table>
        </div>
        """

    # Compose the full HTML email
    timestamp = datetime.now().strftime("%d %b %Y, %I:%M %p")
    email_html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; padding: 20px;">
        <h1 style="color:#e67e22; text-align:center;">New Poultry Case Submitted</h1>
        <p><strong>Submitted by:</strong> {user_name}</p>
        <p><strong>Timestamp:</strong> {timestamp}</p>
        
        <hr style="margin: 30px 0;">

        <h3 style="color:#e67e22;">AI-Inferred Summary</h3>
        <div style="background-color:#f9f9f9; padding: 15px; border-left: 5px solid #e67e22; margin-bottom: 30px;">
            <pre style="white-space: pre-wrap; font-family: inherit;">{summary_text}</pre>
        </div>
        
        <hr style="margin: 30px 0;">

        <h3 style="color:#e67e22;">Farmer's Submitted Data</h3>
        {form_tables_html}

        <hr style="margin: 40px 0;">
        <p style="font-size: 0.9em; color: #777;">
            This email was auto-generated by the AI Vet Assistant system. Please verify findings and consult with the technical team if necessary.<br>
            <em>Do not reply to this email. For urgent cases, escalate through the technical escalation channel.</em>
        </p>
    </body>
    </html>
    """
    return email_html
    
def send_email(to_email: str, subject: str, html_content: str):
    """
    Sends an HTML email using Gmail's SMTP server.

    Args:
        to_email (str): Recipient's email address
        subject (str): Email subject line
        html_content (str): Full HTML email content (from email_generator)
    """
    from_email = "japfanotifier@gmail.com"
    app_password = EMAIL_PASSKEY

    # Create MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    # Attach HTML content
    html_part = MIMEText(html_content, "html")
    msg.attach(html_part)

    # Send via Gmail SMTP
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_email, app_password)
            server.sendmail(from_email, to_email, msg.as_string())
            print("✅ Email sent successfully!")
    except Exception as e:
        print("❌ Failed to send email:", e)

# user_session_data = {
#     123456789: {  # Simulated Telegram user_id
#         "forms": {
#             "flock_farm_information": {
#                 "Type of Chicken": "Broiler",
#                 "Age of Chicken": "6",
#                 "Housing Type": "Open-Sided",
#                 "Number of Affected Flocks/Houses": "2",
#                 "Feed Type": "Self Mix",
#                 "Environment Information": "Very humid. Rainy. Surrounded by 3 other poultry farms within 500m."
#             },
#             "symptoms_performance_data": {
#                 "Main Symptoms": "Reduced feed intake, pale combs, and lethargy",
#                 "Daily Production Performance": "Mortality increased from 1% to 5% in 3 days, feed intake dropped",
#                 "Pattern of Spread or Drop": "Symptoms began in one house, now seen in both"
#             },
#             "medical_diagnostic_records": {
#                 "Vaccination History": "Newcastle, IBD given. No IBV vaccine this cycle.",
#                 "Lab Data": "No lab results yet.",
#                 "Pathology Findings (Necropsy)": "Mild enteritis, no hemorrhage",
#                 "Current Treatment": "Tylosin started yesterday",
#                 "Management Questions": "Could this be early signs of IBV or feed-related?"
#             }
#         },
#         "current_form": "",
#         "current_question": ""
#     }
# }

# user_id = 123456789

# summary = case_summary_agent(user_session_data[user_id]["forms"])
# email_html = email_generator(summary, user_session_data[user_id]["forms"], user_name="John Doe")
# send_email("japfanotifier@gmail.com", "New Poultry Case Submission", email_html)
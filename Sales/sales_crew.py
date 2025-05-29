import os
import json
import re
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
from crewai import Agent, Task, Crew
from crewai.tools import BaseTool
from pydantic import PrivateAttr
from supabase import create_client, Client

# Load .env variables
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
llm = ChatOpenAI(model_name="gpt-4o")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

os.environ["CREWAI_TELEMETRY_DISABLED"] = "1"

class SQLTool(BaseTool):
    name: str = "SQLTool"
    description: str = "Run SQL queries against the poultry database."
    _client: Client = PrivateAttr()

    def __init__(self, supabase_url: str, supabase_key: str, **data):
        super().__init__(**data)
        self._client: Client = create_client(supabase_url, supabase_key)

    def _run(self, query: str) -> str:
            try:
                result = self._client.rpc("run_sql", {"query": query}).execute()
                return result.data if result.data else "No results found."
            except Exception as e:
                return f"Error running query: {e}"

    async def _arun(self, query: str) -> str:
        return self._run(query)

schema = """
Tables:
- flock_farm_information(id, case_id, type_of_chicken, age_of_chicken, housing_type, number_of_affected_flocks_houses, timestamp)
- symptoms_performance_data(id, case_id, main_symptoms, daily_production_performance, pattern_of_spread_or_drop, timestamp)
- medical_diagnostic_records(id, case_id, vaccination_history, lab_data, pathology_findings_necropsy, current_treatment, timestamp)
- issues(id, title, description, farm_name, status, close_reason, assigned_team, case_id, created_at, updated_at)
- farmer_problem(id, case_id, problem_description, timestamp)
- issue_attachments(id, case_id, file_name, file_path, uploaded_at)
"""

sql_tool = SQLTool(SUPABASE_URL, SUPABASE_KEY)

# AGENTS
sql_agent = Agent(
    role="SQL Query Generator",
    goal="Generate safe, parameterized SQL queries to retrieve valid form records.",
    backstory="Expert database analyst that writes clean, secure SQL queries based on user input.",
    verbose=True,
    allow_delegation=False,
    llm=ChatOpenAI(model_name="gpt-4o", temperature=0.3)
)

status_update_agent = Agent(
    role="Status Update Agent",
    goal="Help Sales/Technical team mark issues as 'Closed' or assigned_team as 'Technical'.",
    backstory="An expert in updating issue statuses and generating appropriate SQL queries to reflect changes.",
    verbose=True,
    allow_delegation=False,
    tools=[sql_tool],
    llm=ChatOpenAI(model_name="gpt-4o", temperature=0.3)
)

report_generation_agent = Agent(
    role="Report Generator",
    goal="Format the SQL query results into a readable report.",
    backstory="A skilled report generator capable of transforming raw data into clear and concise reports.",
    verbose=True,
    allow_delegation=False,
    llm=ChatOpenAI(model_name="gpt-4o", temperature=0.3)
)

def convert_placeholders(sql_query):
    count = 1
    def replacer(match):
        nonlocal count
        replacement = f"${count}"
        count += 1
        return replacement
    return re.sub(r'\?', replacer, sql_query)

# TASKS
def generate_and_execute_sql(schema: str, action_type: str = None, case_id: int = None, 
                             user_input:str = None, file_path:str = None, file_name:str = None) -> dict:
    if user_input is None:
        # Create the SQL generation prompt
        if action_type == "case_summary":
            user_input = f"Given the case_id {case_id}, show me ALL form fields and their respective data for ALL the tables in the database. Return the results."
        elif action_type == "generate_report":
            user_input = f"Generate a full report for case ID {case_id}."
        elif action_type == "view_all_issues":
            user_input = "Retrieve ALL issue cases, regardless of their status from the issues table. No need to care about case_id."
        elif action_type == "insert_attachment":
            if not (case_id , file_path and file_name):
                raise ValueError("case_id, file_path and file_name are required for inserting attachment.")
            user_input = f"Fetch the full case_id of {case_id} from issues table. Then, insert a new attachment for the retrieved full case_id with the file_path and file_name into the issue_attachments table."
        else:
            raise ValueError("Unknown action_type")
    
    sql_prompt = f"""
You are an SQL generation agent. Your job is to generate **parameterized SQL** statements to fulfill the following task:

--- USER INPUT ---
"{user_input}"
------------------

Instructions:
- Use the known form schemas listed below.
- Check all tables listed.
- The case_id provided is a partial UUID (first 8 characters only), so write ALL queries using: case_id LIKE ? and ensure the placeholder ? will be replaced with '<value>%'.
- ALWAYS fetch the farm_name in the issues table.
- Do NOT return explanations, only the SQL queries.
- Return the output in **JSON format** with keys as table names and values as SQL strings.
- Use lowercase snake_case field names exactly as defined in the schema (not display labels).

--- FORM SCHEMA ---
{schema}
-------------------

Final Output Format (**EXAMPLE**):

```json
{{
  "biosecurity_form": "SELECT * FROM biosecurity_form WHERE case_id = ? AND farm_location IS NOT NULL AND farm_location != '' ...",
  "mortality_form": "...",
  "health_status_form": "...",
  "farmer_problem": "..."
}}
"""
    sql_task = Task(
    description=sql_prompt,
    agent=sql_agent,
    expected_output="JSON with parameterized SQL"
    )
    
    crew = Crew(agents=[sql_agent], tasks=[sql_task], verbose=True)
    result = crew.kickoff()
    print("SQL Generation Result:\n", result)

    # Extract JSON from output
    match = re.search(r'\{[\s\S]*\}', str(result))
    if not match:
        print("No JSON found in output.")
        return {}

    try:
        sql_queries = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        print("Failed to parse SQL JSON:", e)
        return {}

    # Execute queries
    execution_results = {}

    for table, query in sql_queries.items():
        try:
            if action_type == "insert_attachment" and table == "issue_attachments":
                full_case_id_result = execution_results.get("issues")
                if full_case_id_result and isinstance(full_case_id_result, list) and len(full_case_id_result) > 0:
                    full_case_id = full_case_id_result[0]["case_id"]
                    params = [full_case_id, file_name, file_path]
                else:
                    print("Could not retrieve full case_id from issues query.")
                    continue
            else:
                params = [f"{case_id}%"]

            formatted_query = convert_placeholders(query)
            print("Executing query:", formatted_query)
            print("With params:", params)

            rpc_args = {"query": formatted_query}
            for i, param in enumerate(params, start=1):
                rpc_args[f"param{i}"] = param

            response = supabase_client.rpc("run_sql", rpc_args).execute()

            execution_results[table] = response.data if response.data else []

        except Exception as e:
            execution_results[table] = f"Error executing query: {e}"

    return execution_results

def generate_report_from_prompt(execution_results, case_id):
    report_prompt = f"""
    Below is the raw SQL query result data from multiple forms related to a poultry case.

    Your task is to write a professional, human-readable report summarizing the key findings from the data.

    Instructions:
    - ALWAYS include the farm name and case ID in the report.
    - Organize the report clearly by form/table.
    - Use complete sentences and avoid overly technical language.
    - Summarize observations, issues, and any notable entries.
    - Do not include raw SQL or technical field names.
    - Do not format the output in markdown, code blocks or bold certain words, just plain text.

    Raw Data:
    Case ID: {case_id}
    {json.dumps(execution_results, indent=2)}
    """

    report_task = Task(
        description=report_prompt,
        agent=report_generation_agent,
        expected_output="A clear and readable summary report.",
        output_file="report_from_prompt.txt"
    )

    report_crew = Crew(agents=[report_generation_agent], tasks=[report_task], verbose=True)
    return report_crew.kickoff()

def generate_individual_case_summary(case_id):
    case_summary_task_description = """
    You have been given the following results from SQL queries on case_id {case_id}. Your task is to generate a high-level summary of the following forms.

    --- SQL RESULTS ---
    Farmm Name:
    {farm_name}

    Flock Farm Information Form Results:
    {flock_farm_information_results}

    Symptoms Performance Data Form Results:
    {symptoms_performance_data_results}

    Medical Diagnostic Records Form Results:
    {medical_diagnostic_records_results}

    Format the output as follows:
    "Case #{case_id}:\n\n"
    "Farm Name: <farm_name>\n"
    "Flock Farm Information: <short summary>\n"
    "Symptoms Performance: <short summary>\n"
    "Medical Diagnostic Records: <short summary>\n"
    """

    execution_results_for_case_summary = generate_and_execute_sql(action_type='case_summary', case_id=case_id, schema=schema)

    # Generate the formatted report using the results from the SQL execution
    case_summary_task_description_filled = case_summary_task_description.format(
        case_id=case_id,
        farm_name=execution_results_for_case_summary.get('issues', 'No data available.'),
        flock_farm_information_results=execution_results_for_case_summary.get('flock_farm_information', 'No data available.'),
        symptoms_performance_data_results=execution_results_for_case_summary.get('symptoms_performance_data', 'No data available.'),
        medical_diagnostic_records_results=execution_results_for_case_summary.get('medical_diagnostic_records', 'No data available.')
    )

    case_summary_task = Task(
            description=case_summary_task_description_filled,
            agent=report_generation_agent,
            expected_output=f"Concise natural language summaries of the forms for case_id {case_id}.",
            output_file="formatted_case_summary.txt"
        )

    case_summary_crew = Crew(
        agents=[report_generation_agent],
        tasks=[case_summary_task],
        verbose=True
    )

    return case_summary_crew.kickoff()

def generate_case_summary_for_email(case_id):
    case_summary_task_description = """
    You have been given the following results from SQL queries on case_id {case_id}. Your task is to generate a high-level summary of the following data. 
    Limit your response to 2 sentences for each result.
    Do not wrap the output in any code block or quotes.

    --- SQL RESULTS ---
    Farm Name:
    {farm_name}

    Flock Farm Information Form Results:
    {flock_farm_information_results}

    Symptoms Performance Data Form Results:
    {symptoms_performance_data_results}

    Medical Diagnostic Records Form Results:
    {medical_diagnostic_records_results}

    Farmer's Problem:
    {farmer_problem_results}

    Format the output as follows:
    "Farm Name: <farm_name>\n"
    "Flock Farm Information: <short summary>\n"
    "Symptoms Performance: <short summary>\n"
    "Medical Diagnostic Records: <short summary>\n"
    "Farmer's Problem: <short summary>\n"
    """

    execution_results_for_case_summary = generate_and_execute_sql(action_type='case_summary', case_id=case_id, schema=schema)

    # Generate the formatted report using the results from the SQL execution
    case_summary_task_description_filled = case_summary_task_description.format(
        case_id=case_id,
        farm_name=execution_results_for_case_summary.get('issues', 'No data available.'),
        flock_farm_information_results=execution_results_for_case_summary.get('flock_farm_information', 'No data available.'),
        symptoms_performance_data_results=execution_results_for_case_summary.get('symptoms_performance_data', 'No data available.'),
        medical_diagnostic_records_results=execution_results_for_case_summary.get('medical_diagnostic_records', 'No data available.'),
        farmer_problem_results=execution_results_for_case_summary.get('farmer_problem', 'No data available.')
    )

    case_summary_task = Task(
            description=case_summary_task_description_filled,
            agent=report_generation_agent,
            expected_output=f"Concise natural language summaries of the forms for case_id {case_id}.",
            output_file="case_summary_for_email.txt"
        )

    case_summary_crew = Crew(
        agents=[report_generation_agent],
        tasks=[case_summary_task],
        verbose=True
    )

    return case_summary_crew.kickoff()

def generate_report_for_forms(case_id):
    report_task_description = """
    You have been given the following results from SQL queries on case_id {case_id}. Your task is to generate a comprehensive report including the data from the forms.

    --- SQL RESULTS ---
    Farm Name:
    {farm_name}

    Status of the case:
    {status}

   Flock Farm Information Form Results:
    {flock_farm_information_results}

    Symptoms Performance Data Form Results:
    {symptoms_performance_data_results}

    Medical Diagnostic Records Form Results:
    {medical_diagnostic_records_results}

    Farmer's Problem:
    {farmer_problem_results}

    Format the output as follows:
    "Farm Name: <farm_name>\n"
    "Status: <status>\n"

    Flock Farm Information for Case #{case_id}:
    - Field Name 1 — Value 1
    - Field Name 2 — Value 2
    ...

    Symptoms Performance for Case #{case_id}:
    - Field Name 1 — Value 1
    - Field Name 2 — Value 2
    ...

    Medical Diagnostic Records for Case #{case_id}:
    - Field Name 1 — Value 1
    - Field Name 2 — Value 2
    ...

    Farmer's Problem:
    <problem_description>
    """

    execution_results_for_full_report = generate_and_execute_sql(action_type='generate_report', case_id=case_id, schema=schema)

    # Generate the formatted report using the results from the SQL execution
    report_task_description_filled = report_task_description.format(
        case_id=case_id,
        farm_name=execution_results_for_full_report.get('issues', 'No data available.'),
        status = execution_results_for_full_report.get('issues', 'No data available.'),
        flock_farm_information_results=execution_results_for_full_report.get('flock_farm_information', 'No data available.'),
        symptoms_performance_data_results=execution_results_for_full_report.get('symptoms_performance_data', 'No data available.'),
        medical_diagnostic_records_results=execution_results_for_full_report.get('medical_diagnostic_records', 'No data available.'),
        farmer_problem_results=execution_results_for_full_report.get('farmer_problem', 'No data available.')
    )

    generate_full_report = Task(
        description=report_task_description_filled,
        agent=report_generation_agent,
        expected_output="Formatted report",
        output_file="report_output.txt"
    )

    report_crew = Crew(
        agents=[report_generation_agent],
        tasks=[generate_full_report],
        verbose=True
    )

    return report_crew.kickoff()

def generate_summary_of_all_issues():
    # Access 'issues' data from the 'execution_results'
    execution_results = generate_and_execute_sql(action_type='view_all_issues', schema=schema)
    issues_results = execution_results.get("issues", [])

    # Calculate totals
    total_cases = len(issues_results)
    open_cases = sum(1 for issue in issues_results if issue.get('status') == 'open')
    closed_cases = total_cases - open_cases
    farm_summary = {}

    for issue in issues_results:
        farm_name = issue.get('farm_name', 'Unknown')
        status = issue.get('status', 'open')

        if farm_name not in farm_summary:
            farm_summary[farm_name] = {"total": 0, "open": 0, "closed": 0, "needs_tech_help": 0}

        farm_summary[farm_name]["total"] += 1
        if status == "open":
            farm_summary[farm_name]["open"] += 1
        else:
            farm_summary[farm_name]["closed"] += 1

        # Check for 'tech' in 'Needs Tech Help' column 
        if 'tech' in (issue.get('assigned_team') or '').lower():
            farm_summary[farm_name]["needs_tech_help"] += 1

    # Format summary
    case_summary_by_farm = "\n".join(
        [f"{farm_name:<20} {summary['total']:>5} {summary['open']:>5} {summary['closed']:>5} {summary['needs_tech_help']:>15}" for farm_name, summary in farm_summary.items()]
    )

    # Prepare task description for summary
    summary_task_description = """
    You have been given the following results from SQL queries. Your task is to generate a comprehensive overview of all the issues in the database, using the format below.

    --- SQL RESULTS ---
    Issues Results:
    {issues_results}

    Format the output as follows:
    "Issues Summary:\n"
    "- Total Cases: {total_cases}"
    "  - Open Cases: {open_cases}"
    "  - Closed Cases: {closed_cases}\n"
    "\nCase Summary by Farm:\n"
    "Farm Name            Total  Open   Closed   Needs Tech Help\n"
    "------------------------------------------------------------\n"
    {case_summary_by_farm}
    """

    summary_task_description_filled = summary_task_description.format(
        issues_results=issues_results,
        total_cases=total_cases,
        open_cases=open_cases,
        closed_cases=closed_cases,
        case_summary_by_farm=case_summary_by_farm
    )

    # Create task to generate summary
    summary_task = Task(
        description=summary_task_description_filled,
        agent=report_generation_agent,
        expected_output=f"Summaries of the all the issues in the database, following the format given.",
        output_file="summary_of_all_issues.txt"
    )

    summary_crew = Crew(
        agents=[report_generation_agent],
        tasks=[summary_task],
        verbose=True
    )

    return summary_crew.kickoff()

# Task for Status Update
def check_case_exists(case_id: str) -> bool:
    try:
        response = supabase_client.rpc("case_exists_rpc", {"case_prefix": case_id}).execute()
        return response.data is True
    except Exception as e:
        print(f"Error calling RPC case_exists_rpc: {e}")
        return False
    
def execute_case_closing(case_id: str, reason: str) -> str:
    """Close a case using CrewAI's task execution flow."""
    close_task = Task(
        description=
        f"The case_id provided is only the first 8 characters of the case_id, so write queries using: case_id LIKE ? and ensure the placeholder ? will be replaced with '<value>%'. \
        Close case {case_id} with the close_reason as: {reason} in the issues table.",
        agent=status_update_agent,
        tools=[sql_tool],
        expected_output="Confirmation that the case has been closed. Give a simple explanation, don't mention '8 character case_id'."
    )

    crew = Crew(
        agents=[status_update_agent],
        tasks=[close_task],
        verbose=True
    )
    return crew.kickoff()

def execute_case_escalation(case_id: str, reason:str) -> str:
    """Escalate a case using CrewAI's task execution flow."""
    escalate_task = Task(
        description=
        f"The case_id provided is a partial UUID (first 8 characters only), so write queries using: case_id LIKE ? and ensure the placeholder ? will be replaced with '<value>%'. \
        Update the 'assigned_team' field to 'Technical' for case ID {case_id} with the escalation_reason as: {reason} in the 'issues' table.",
        agent=status_update_agent,
        tools=[sql_tool],
        expected_output="Confirmation that the case has been escalated to the Technical team."
    )

    crew = Crew(
        agents=[status_update_agent],
        tasks=[escalate_task],
        verbose=True
    )
    return crew.kickoff()

# TESTING CODE
# case_id = int(input("Enter the case ID: "))
# prompt = input("Enter the prompt: ")

# prompt_results = generate_and_execute_sql(schema=schema, case_id=case_id, user_input=prompt)
# generate_report_from_prompt(prompt_results)

# generate_report_for_forms(case_id)

# generate_individual_case_summary(case_id)

# generate_summary_of_all_issues()
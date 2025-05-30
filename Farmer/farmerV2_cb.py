import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)
from farmer_agents import dynamic_sql_agent, db_init_agent, data_validator_agent, case_summary_agent, email_generator, send_email
import ast
import uuid
import re
import textwrap
import inspect
import asyncio
from functools import partial
import logging
from datetime import datetime

# basic logging setup
# logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# States
SELECTING_FORM, SELECTING_QUESTION, ENTERING_ANSWER = range(3)

# ==================================DATA THAT CAN CHANGE=========================================

form_definitions = {
    "flock_farm_information": {
        "Type of Chicken": {
            "question": "What type of chicken is this? (e.g., Layer, Broiler, Breeder)",
            "type": "TEXT",
            "validator": lambda x: x.lower() in ["layer", "broiler", "breeder"],
            "use_agent": False
        },
        "Age of Chicken": {
            "question": "What is the age of the chicken? (years, round up to nearest whole number)",
            "type": "INTEGER",
            "validator": lambda x: x.isdigit() and int(x) > 0 and int(x) < 50,
            "use_agent": False
        },
        "Housing Type": {
            "question": "What housing type is used? (e.g., Closed House, Opened-Side)",
            "type": "TEXT",
            "validator": lambda x: x.lower() in ["closed house", "opened-side", "open-sided", "open house"],
            "use_agent": False
        },
        "Number of Affected Flocks/Houses": {
            "question": "How many flocks or houses are affected?",
            "type": "INTEGER",
            "validator": lambda x: x.isdigit() and int(x) >= 0,
            "use_agent": False
        },
        "Feed Type": {
            "question": "What type of feed is used? (e.g., Complete Feed, Self Mix)",
            "type": "TEXT",
            "validator": lambda x: x.lower() in ["complete feed", "self mix"],
            "use_agent": False
        },
        "Environment Information": {
            "question": "Describe the environmental conditions (e.g., climate, weather, cage atmosphere, nearby poultry farms)",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 10,
            "use_agent": True
        }
    },

    "symptoms_performance_data": {
        "Main Symptoms": {
            "question": "What are the main symptoms or clinical signs observed?",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        },
        "Daily Production Performance": {
            "question": "Provide daily chicken production data (e.g., mortality, %HD, feed intake, egg weight)",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        },
        "Pattern of Spread or Drop": {
            "question": "Describe if there's mortality, production drop, or spreading pattern",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        }
    },

    "medical_diagnostic_records": {
        "Vaccination History": {
            "question": "What is the vaccination history or program followed?",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        },
        "Lab Data": {
            "question": "Provide any lab results or data if available",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        },
        "Pathology Findings (Necropsy)": {
            "question": "List any pathology anatomy changes found during necropsy",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        },
        "Current Treatment": {
            "question": "What treatment is currently being administered?",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        },
        "Management Questions": {
            "question": "List any management-related concerns or questions",
            "type": "TEXT",
            "validator": lambda x: len(x.strip()) > 5,
            "use_agent": True
        }
    }
}

intent_dict = {   
    "insert_into_db": "Insert or update each form with all available fields. Always refresh the timestamp on update.",
    "get_latest_case_ids_per_form_for_user": (
        "Generate a single SQL UNION query that selects the form name, case_id, and latest timestamp from each form table. "
        "Each subquery should select the form name as a constant string. "
        "Filter by user = ?. Group by case_id. Combine using UNION ALL. "
        "Return the entire query as one string under the JSON key `unified_output`."
    ),
    "get_all_form_data_by_case_id_and_user": "Retrieve all saved answers from each form table where both user and case_id match exactly. Include timestamp column if available.",
    "get_latest_timestamp_for_case_id_per_form": "For each form table, select the latest timestamp for 2 parameters which are a given case_id and a given user_id. Order by timestamp descending and limit to 1 row per table.",
    "delete_case_by_user_and_case_id": (
        "For each form table, generate a SQL statement to delete all entries belonging to a given user and case ID. "
        "Use parameter placeholders for user and case_id. "
        "Return the statements as a JSON object with table names as keys and the SQL as values."
    ),
}

# =================================================================================================

# DB SETUP
DB_PATH = "../JAPFASNOWFLAKE.db"

os.environ["PYTHONIOENCODING"] = "utf-8"

# In-memory session data
user_session_data = {}

logging.basicConfig(
    filename='logs/bot.log',
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s'
)

# DB Setup
def init_db(form_definitions_types):
    form_types = {
        form: {q: meta["type"] for q, meta in fields.items()}
        for form, fields in form_definitions.items()
    }
    sql_block = db_init_agent(form_types)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        # Split the SQL block into individual CREATE TABLE statements
        statements = [stmt.strip() for stmt in sql_block.split(";") if stmt.strip()]
        for stmt in statements:
            # If UNIQUE constraint is not present, inject it before the closing bracket
            if "UNIQUE(case_id, user)" not in stmt:
                stmt = re.sub(r"\)(\s*)$", r",\n    UNIQUE(case_id, user)\1)", stmt)

            print(f"Executing:\n{stmt}\n")
            c.execute(stmt)
        conn.commit()
    except Exception as e:
        print("❌ Error executing SQL schema:", e)
    finally:
        conn.close()
        
def to_sql_field_name(label):
    return re.sub(r'\W+', '_', label.strip().lower())

def extract_field_names_from_insert(sql):
    match = re.search(r"INSERT INTO \w+ \((.*?)\)", sql)
    if not match:
        return []
    field_str = match.group(1)
    return [field.strip() for field in field_str.split(",")]

async def check_for_incomplete_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Starting Up Chat Bot. Please wait...")
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Query to get latest case_id entries per form table for this user
    form_types = {
        form: {q: meta["type"] for q, meta in fields.items()}
        for form, fields in form_definitions.items()
    }
    sql_json = ast.literal_eval(dynamic_sql_agent(intent_dict["get_latest_case_ids_per_form_for_user"], form_types))
    sql = sql_json.get("unified_output")
    
    # Get dynamic SQL for reading data and timestamps
    sql_dict = ast.literal_eval(dynamic_sql_agent(intent_dict["get_all_form_data_by_case_id_and_user"], form_types))
    ts_sql_dict = ast.literal_eval(dynamic_sql_agent(intent_dict["get_latest_timestamp_for_case_id_per_form"], form_types))
    
    if not sql:
        raise ValueError("Expected 'unified_output' from SQL agent, got: " + str(sql_json))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(sql, (str(user_id),) * len(form_definitions))
    all_cases = c.fetchall()

    incomplete_cases = {}
    for form_name, case_id, _ in all_cases:
        if case_id not in incomplete_cases:
            incomplete_cases[case_id] = {}
        incomplete_cases[case_id][form_name] = True

    # Now check for missing fields
    incomplete_case_ids = []
    for case_id in incomplete_cases:
        session_data = {"forms": {}}
        for form in form_definitions:
            sql = sql_dict.get(form)
            c.execute(sql, (case_id, str(user_id)))
            
            rows = c.fetchall()
            if rows:
                col_names = [desc[0] for desc in c.description]
                all_fields = {}

                for row in rows:
                    row_dict = dict(zip(col_names, row))
                    for question_key in form_definitions[form]:
                        col_key = normalize_key(question_key)
                        val = row_dict.get(col_key)
                        if col_key not in all_fields and val is not None and str(val).strip():
                            all_fields[col_key] = str(val)

                session_data["forms"][form] = {
                    question: all_fields.get(normalize_key(question), "")
                    for question in form_definitions[form]
                    if normalize_key(question) in all_fields
                }

        complete, missing = is_all_form_data_complete(session_data, form_definitions)
        if not complete:
            incomplete_case_ids.append(case_id)

    # If any, prompt user to resume or create new
    if incomplete_case_ids:
        # Build case metadata with timestamps
        case_details = []
        for case_id in incomplete_case_ids:
            latest_ts = None

            # Loop to find the latest timestamp across forms for the case
            for form in form_definitions:
                ts_sql = ts_sql_dict.get(form)
                c.execute(ts_sql, (case_id, str(user_id)))
                        
                row = c.fetchone()
                if row:
                    col_names = [desc[0] for desc in c.description]
                    row_dict = dict(zip(col_names, row))
                    ts_str = row_dict.get("timestamp")
                    
                    if ts_str:
                        latest_ts = ts_str

            # Fallback if no timestamp is found
            if not latest_ts:
                print(f"[WARN] No timestamp found for case {case_id}, using current time as fallback.")
                latest_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            dt_obj = datetime.strptime(latest_ts, "%Y-%m-%d %H:%M:%S")
            case_details.append((case_id, dt_obj))

        # Sort by datetime descending
        case_details.sort(key=lambda x: x[1], reverse=True)
        
        # Build message body instead
        message_lines = ["📂 You have unfinished cases. Would you like to resume any of them or start a new one?\n"]
        keyboard = []
        
        for idx, (cid, dt) in enumerate(case_details, 1):
            message_lines.append(
                f"{idx}. 📝 Case `{cid[:8]}` — 📅 {dt.strftime('%d %b %Y, %I:%M %p')}"
            )
            keyboard.append([InlineKeyboardButton(f"Resume Case {cid[:8]}", callback_data=f"resume:{cid}")])
        
        # Add final button
        keyboard.append([InlineKeyboardButton("➕ Start New Case", callback_data="start_new_case")])
        
        conn.close()

        await update.message.reply_text(
            "\n".join(message_lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SELECTING_FORM

    return await start(update, context)

# 🔧 Fix: Normalize DB column keys to match form definition keys
def normalize_key(label):
    return re.sub(r'\W+', '_', label.strip().lower())

async def resume_existing_case(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Resuming your case. Please wait...")
    user_id = query.from_user.id
    case_id = query.data.split(":")[1]

    # Fetch all data into user_session_data
    session_data = {
        "forms": {},
        "current_form": "",
        "current_question": "",
        "case_id": case_id
    }

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    latest_ts = None  
    
    for form in form_definitions:
        form_types = {
            form: {q: meta["type"] for q, meta in fields.items()}
            for form, fields in form_definitions.items()
        }
        sql_dict = ast.literal_eval(dynamic_sql_agent(intent_dict["get_all_form_data_by_case_id_and_user"], form_types))
        sql = sql_dict.get(form)
        c.execute(sql, (case_id, str(user_id)))
        
        rows = c.fetchall()
        if rows:
            col_names = [desc[0] for desc in c.description]
            all_fields = {}
            latest_ts_in_form = None

            for row in rows:
                row_dict = dict(zip(col_names, row))
                ts = row_dict.get("timestamp")
                if ts and (not latest_ts_in_form or ts > latest_ts_in_form):
                    latest_ts_in_form = ts

                for question_key in form_definitions[form]:
                    col_key = normalize_key(question_key)
                    val = row_dict.get(col_key)
                    if col_key not in all_fields and val is not None and str(val).strip():
                        all_fields[col_key] = str(val)

            session_data["forms"][form] = {
                question: all_fields.get(normalize_key(question), "")
                for question in form_definitions[form]
                if normalize_key(question) in all_fields
            }

            # Update latest_ts for the case (for correct sorting)
            if not latest_ts or latest_ts_in_form > latest_ts:
                latest_ts = latest_ts_in_form
    conn.close()

    user_session_data[user_id] = session_data
    await query.edit_message_text(f"✅ Resumed case {case_id[:8]}. Let's continue.")
    return await start(update, context, preserve_session=True)

def save_to_db_with_agent(user_id, form, case_id, data, sql_dict):
    sql = sql_dict.get(form)
    if not sql:
        print(f"No SQL returned for form: {form}")
        return

    try:
        # Normalize field names in data
        normalized_data = {
            to_sql_field_name(k): v for k, v in data.items()
        }

        # Extract expected field names from SQL
        field_names = extract_field_names_from_insert(sql)

        # Build value map including case_id and user
        value_map = {
            "case_id": case_id,
            "user": str(user_id),
            **normalized_data
        }
        
        # Force timestamp update if used in SQL
        if "timestamp" in field_names:
            value_map["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Only exclude timestamp if it's hardcoded in SQL
        exclude_ts = "CURRENT_TIMESTAMP" in sql.upper()
        
        if exclude_ts:
            fields_without_timestamp = [f for f in field_names if f != "timestamp"]
        else:
            fields_without_timestamp = field_names  # Include timestamp as a value
        
        values = [value_map.get(field, None) for field in fields_without_timestamp]

        print("FINAL SQL:", sql)
        print("VALUES:", values)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(sql, values)
        conn.commit()
        conn.close()

    except Exception as e:
        print("❌ Error executing dynamic SQL:", e)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, preserve_session=False):
    user_id = update.effective_user.id
    if not preserve_session:
        user_session_data[user_id] = {
            "forms": {},
            "current_form": "",
            "current_question": "",
            "case_id": user_session_data.get(user_id, {}).get("case_id", str(uuid.uuid4()))
        }
    else:
        # Ensure case_id is present for resumed sessions
        user_session_data[user_id].setdefault("case_id", str(uuid.uuid4()))
        
    session = user_session_data[user_id]
    forms_data = session.get("forms", {})

    total_forms = len(form_definitions)
    completed_forms = 0
    form_status_lines = []

    for form_name, questions in form_definitions.items():
        answered = forms_data.get(form_name, {})
        total_q = len(questions)
        answered_q = sum(1 for q in questions if answered.get(q) and str(answered[q]).strip())
        if answered_q == total_q:
            completed_forms += 1
        form_status_lines.append(f"📄 {form_name.replace('_', ' ').title()}: {answered_q}/{total_q} answered")

    all_done = completed_forms == total_forms
    header = f"📋 You have completed {completed_forms}/{total_forms} forms."
    if all_done:
        header += "\n✅ All forms completed! Ready to submit."
        
    keyboard = [[InlineKeyboardButton(name.replace("_", " ").title(), callback_data=f"form:{name}")]
                for name in form_definitions.keys()]
    keyboard.append([InlineKeyboardButton("💾 Save and Quit", callback_data="save_quit")])
    keyboard.append([InlineKeyboardButton("📩 Submit & Email", callback_data="submit_and_email")])
    keyboard.append([InlineKeyboardButton("🗑️ Delete Case", callback_data="delete_case_menu")])
    
    message = f"{header}\n\n" + "\n".join(form_status_lines)
        
    if update.message:
        await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_FORM

# Form selection
async def select_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    form_name = query.data.split(":")[1]

    # Always initialize the expected nested structure safely
    session = user_session_data.setdefault(user_id, {})
    session.setdefault("forms", {})
    session.setdefault("current_form", "")
    session.setdefault("current_question", "")

    session["current_form"] = form_name
    session["forms"].setdefault(form_name, {})

    return await show_question_menu(query, user_id)


async def show_question_menu(query, user_id):
    form = user_session_data[user_id]["current_form"]
    answered = user_session_data[user_id]["forms"].get(form, {})
    keyboard = []
    for q in form_definitions[form]:
        val = answered.get(q, "")
        status = "✅" if val and str(val).strip() else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} {q}", callback_data=f"question:{q}")])

    # Add Return to Form Select button
    keyboard.append([InlineKeyboardButton("🔙 Return to Form Select", callback_data="return_to_form_select")])

    await query.edit_message_text(
        f"📄 Answering: {form.replace('_', ' ').title()}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_QUESTION

async def return_to_form_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # user_id = query.from_user.id
    # print(f"User {user_id} pressed 'Return to Form Select'")  # Debugging log
    return await start(update, context, preserve_session=True)

async def select_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    question = query.data.split(":")[1]
    user_session_data[user_id]["current_question"] = question
    form = user_session_data[user_id]["current_form"]
    
    # Message text: the actual question + instruction
    question_text = form_definitions[form][question]["question"] + "\n\n✍️ Please type your answer below."

    # Inline keyboard for cancel
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Return to Question Menu", callback_data="return_to_question_menu")]
    ])

    # Send question with inline cancel button
    await query.edit_message_text(question_text, reply_markup=keyboard)
    
    return ENTERING_ANSWER

async def return_to_question_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    return await show_question_menu(query, user_id)

async def enter_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    answer = update.message.text.strip()
    await update.message.reply_text("⏳ Validating your input. Please wait...")
    question = user_session_data[user_id].get("current_question")
    form = user_session_data[user_id]["current_form"]

    field_meta = form_definitions[form][question]
    validator = field_meta.get("validator")
    use_agent = field_meta.get("use_agent")

    # Step 1: Local validation (fast fail for known structured fields)
    if validator and not validator(answer):
        dynamic_error = local_validator(question, validator)
        await update.message.reply_text(dynamic_error)
        await update.message.reply_text(field_meta["question"])
        return ENTERING_ANSWER

    # Step 2: Run AI-based validator (only if marked to use agent)
    if validator and use_agent:
        validator_output, processed_answer = data_validator_agent(
            question=question,
            answer=answer,
            form_def={q: form_definitions[form][q]["question"] for q in form_definitions[form]},
            form_val={form: {q: form_definitions[form][q]["validator"] for q in form_definitions[form]}}
        )

        print("🧪 Validator Output:", repr(validator_output))
        print("🧪 Answer Returned:", repr(processed_answer))

        # Handle invalid/suspicious result
        if not validator_output.strip().startswith("✅"):
            await update.message.reply_text(processed_answer or validator_output)
            await update.message.reply_text(field_meta["question"])
            return ENTERING_ANSWER

        # If valid and autocorrected, update answer
        if processed_answer != "valid" and processed_answer != answer:
            answer = processed_answer
            await update.message.reply_text(f"✅ Answer accepted (autocorrected to: {answer})")
        else:
            await update.message.reply_text("✅ Answer accepted.")
    else:
        # No agent needed, proceed
        await update.message.reply_text("✅ Answer accepted.")

    # Save the answer into session
    user_session_data[user_id]["forms"][form][question] = answer

    # All questions done for the form?
    if len(user_session_data[user_id]["forms"][form]) == len(form_definitions[form]):
        await update.message.reply_text("✅ All questions answered! Returning to main menu...")
        return await start(update, context, preserve_session=True)
    else:
        # More questions remain
        class FakeQuery:
            async def edit_message_text(self, text, reply_markup=None):
                await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)

        return await show_question_menu(FakeQuery(), user_id)
    
def local_validator(question, validator):
    try:
        src = textwrap.dedent(inspect.getsource(validator)).strip()

        # 🧠 Detect number validation
        if "x.isdigit()" in src:
            matches = re.findall(r"int\(x\)\s*([<>]=?)\s*(\d+)", src)
            if matches:
                parts = []
                for operator, threshold in matches:
                    threshold = int(threshold)
                    op_map = {
                        ">": f"greater than {threshold}",
                        ">=": f"greater than or equal to {threshold}",
                        "<": f"less than {threshold}",
                        "<=": f"less than or equal to {threshold}"
                    }
                    parts.append(op_map.get(operator, f"{operator} {threshold}"))
                return f"⚠️ Please enter only a number that is " + " and ".join(parts) + "."
            else:
                return "⚠️ Please enter only a number."

        # 🧠 Detect string list options
        elif "x.lower()" in src and "in" in src:
            options = re.findall(r"\[([^\]]+)\]", src)
            if options:
                clean_opts = options[0].replace('"', '').replace("'", "").split(",")
                formatted = ", ".join(o.strip().title() for o in clean_opts)
                return f"⚠️ Please choose one of the following: {formatted}."

        # 🧠 Detect length checks
        elif "len(x.strip())" in src:
            matches = re.findall(r"len\(x\.strip\(\)\)\s*([<>]=?)\s*(\d+)", src)
            if matches:
                msgs = []
                for operator, length in matches:
                    op_map = {
                        ">": f"at least {int(length)+1} characters",
                        ">=": f"{length} or more characters",
                        "<": f"fewer than {length} characters",
                        "<=": f"{length} or fewer characters"
                    }
                    msgs.append(op_map.get(operator, f"{operator} {length} characters"))
                return "⚠️ Please enter text with " + " and ".join(msgs) + "."
            return "⚠️ Input length does not meet requirements."

        return "⚠️ Invalid input. Please try again."

    except Exception:
        return "⚠️ Invalid input. Please try again."
    
def is_all_form_data_complete(session: dict, form_definitions: dict) -> tuple[bool, list[tuple[str, str]]]:
    """
    Checks for completeness and identifies missing fields.

    Returns:
    - (True, []) if everything is filled
    - (False, [(form, question_key), ...]) if missing fields exist
    """
    forms_data = session.get("forms", {})
    missing_fields = []

    for form_name, fields in form_definitions.items():
        answers = forms_data.get(form_name, {})
        for question_key in fields:
            if question_key not in answers or not str(answers[question_key]).strip():
                missing_fields.append((form_name, question_key))
    
    return (len(missing_fields) == 0), missing_fields


async def save_quit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Saving your case...")
    user_id = query.from_user.id
    session = user_session_data.get(user_id)
    if session:
        case_id = session.get("case_id") or str(uuid.uuid4())
        session["case_id"] = case_id  # ensure it's stored if newly created
        
    user_prompt = intent_dict["insert_into_db"]
    form_types = {
        form: {q: meta["type"] for q, meta in fields.items()}
        for form, fields in form_definitions.items()
    }
    sql_dict = ast.literal_eval(dynamic_sql_agent(user_prompt, form_types))

    for form_name in form_definitions.keys():
        answers = session.get("forms", {}).get(form_name, {})
        save_to_db_with_agent(user_id, form_name, case_id, answers, sql_dict)
    await query.edit_message_text("💾 Saved. Goodbye!")
    user_session_data.pop(user_id, None)
    return ConversationHandler.END

async def submit_and_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Submitting your case and generating email report. Please wait...")
    user_id = query.from_user.id
    session = user_session_data.get(user_id)

    logger.info(f"📨 [User {user_id}] Triggered Submit & Email.")

    if not session:
        logger.warning(f"⚠️ [User {user_id}] No session found.")
        await query.edit_message_text("⚠️ No session found. Please start a new form entry.")
        return ConversationHandler.END

    complete, missing = is_all_form_data_complete(session, form_definitions)

    if not complete:
        # Group missing fields by form
        missing_by_form = {}
        for form, question in missing:
            missing_by_form.setdefault(form, []).append(question)

        # Build formatted message
        missing_message = "⚠️ You still have the following unanswered fields:\n"
        for form_name, questions in missing_by_form.items():
            readable_form = form_name.replace('_', ' ').title()
            missing_message += f"\n📄 *{readable_form}*\n"
            for q in questions:
                missing_message += f"❌ {q}\n"

        missing_message += "\nWhat would you like to do?"

        # Buttons to resume or skip
        keyboard = [
            [InlineKeyboardButton("📝 Continue answering", callback_data="return_to_form_select")],
            [InlineKeyboardButton("💾 Save and Quit", callback_data="save_quit")]
        ]
        await query.edit_message_text(missing_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return SELECTING_FORM
    
        # ✅ Save to DB before sending email
    case_id = session.get("case_id") or str(uuid.uuid4())
    session["case_id"] = case_id

    user_prompt = intent_dict["insert_into_db"]
    form_types = {
        form: {q: meta["type"] for q, meta in fields.items()}
        for form, fields in form_definitions.items()
    }
    sql_dict = ast.literal_eval(dynamic_sql_agent(user_prompt, form_types))

    for form_name in form_definitions.keys():
        answers = session.get("forms", {}).get(form_name, {})
        save_to_db_with_agent(user_id, form_name, case_id, answers, sql_dict)

    logger.info(f"✅ [User {user_id}] All fields complete. Proceeding with email.")

    form_responses = session.get("forms", {})
    summary = case_summary_agent(form_responses)
    logger.info(f"📄 [User {user_id}] Summary generated.")

    user_name = update.effective_user.full_name
    email_html = email_generator(summary, form_responses, user_name=user_name)

    try:
        send_email(
            to_email="japfanotifier@gmail.com",
            subject="New Poultry Case Submission",
            html_content=email_html
        )
        await query.edit_message_text("📩 Case submitted and emailed successfully. Thank you!")
        logger.info(f"📧 [User {user_id}] Email sent successfully.")
    except Exception as e:
        logger.error(f"❌ [User {user_id}] Failed to send email: {e}", exc_info=True)
        await query.edit_message_text("⚠️ Submission failed during email sending. Please try again later.")
        return ConversationHandler.END

    # Clear session after email
    user_session_data.pop(user_id, None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def delete_case_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    session = user_session_data.get(user_id)
    if not session or not session.get("case_id"):
        await query.edit_message_text(
            "⚠️ No active case found to delete.\n\nReturning to main menu..."
        )
        return await start(update, context, preserve_session=True)

    case_id = session["case_id"]

    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, delete it", callback_data=f"confirm_delete_case:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"confirm_delete_case:no")
        ]
    ]
    await query.edit_message_text(
        f"⚠️ Are you sure you want to delete the current case `{case_id[:8]}`? This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SELECTING_FORM

async def confirm_delete_case(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Deleting your case. Please wait...")
    user_id = query.from_user.id
    decision = query.data.split(":")[1]

    if decision == "no":
        await query.edit_message_text("❎ Deletion cancelled. Returning to main menu.")
        return await start(update, context, preserve_session=True)

    session = user_session_data.get(user_id)
    if not session or not session.get("case_id"):
        await query.edit_message_text("⚠️ No active case found to delete.")
        return SELECTING_FORM

    case_id = session["case_id"]

    try:
        # Prepare dynamic DELETE SQL
        form_types = {
            form: {q: meta["type"] for q, meta in fields.items()}
            for form, fields in form_definitions.items()
        }
        sql_dict = ast.literal_eval(dynamic_sql_agent(intent_dict["delete_case_by_user_and_case_id"], form_types))
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        try:
            for form, sql in sql_dict.items():
                c.execute(sql, (case_id, str(user_id)))  # Note: SQL should have WHERE user = ? AND case_id = ?
            conn.commit()
        except Exception as e:
            logger.error(f"❌ Failed dynamic delete SQL: {e}", exc_info=True)
            await query.edit_message_text("⚠️ Failed to delete case due to a system error.")
            return SELECTING_FORM
        finally:
            conn.close()

        user_session_data.pop(user_id, None)
        await query.edit_message_text(f"🗑️ Case `{case_id[:8]}` has been permanently deleted.", parse_mode="Markdown")
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"❌ Failed to delete case: {e}", exc_info=True)
        await query.edit_message_text("⚠️ Failed to delete case due to a system error.")
        return SELECTING_FORM

def main():
    if not os.path.exists(DB_PATH):
        print("Database not found. Initializing...")
        init_db(form_definitions)
    else:
        print("Database already exists. Skipping initialization.")
        
    app = Application.builder().token("7685786328:AAEilDDS65J7-GB43i1LlaCJWJ3bx3i7nWs").build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", check_for_incomplete_cases)],
        states={
            SELECTING_FORM: [
                CallbackQueryHandler(select_form, pattern="^form:"),
                CallbackQueryHandler(save_quit, pattern="^save_quit$"),
                CallbackQueryHandler(submit_and_email, pattern="^submit_and_email$"),
                CallbackQueryHandler(return_to_form_select, pattern="^return_to_form_select$"),
                CallbackQueryHandler(resume_existing_case, pattern="^resume:"),
                CallbackQueryHandler(start, pattern="^start_new_case$"),
                CallbackQueryHandler(delete_case_menu, pattern="^delete_case_menu$"),
                CallbackQueryHandler(confirm_delete_case, pattern="^confirm_delete_case:(yes|no)$"),
            ],
            SELECTING_QUESTION: [
                CallbackQueryHandler(select_question, pattern="^question:"),
                CallbackQueryHandler(select_form, pattern="^form:"),
                CallbackQueryHandler(return_to_form_select, pattern="^return_to_form_select$")
            ],
            ENTERING_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_answer),
                CallbackQueryHandler(return_to_question_menu, pattern="^return_to_question_menu$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()
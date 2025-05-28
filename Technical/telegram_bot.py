import logging
import os
import re
import json
import asyncio
from langchain_openai import ChatOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from Technical.technical_crew import run_upload_analysis, upload_file_to_supabase
from Sales.sales_crew import (
    execute_case_closing,
    check_case_exists,
    generate_individual_case_summary,
    generate_report_for_forms,
    generate_summary_of_all_issues,
    generate_and_execute_sql,
    generate_report_from_prompt
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

schema = """
Tables:
- flock_farm_information(id, case_id, type_of_chicken, age_of_chicken, housing_type, number_of_affected_flocks, feed_type, environment_information, timestamp)
- symptoms_performance_data(id, case_id, main_symptoms, daily_production_performance, pattern_of_spread_or_drop, timestamp)
- medical_diagnostic_records(id, case_id, vaccination_history, lab_data, pathology_findings_necropsy, current_treatment, management_questions, timestamp)
- issues(id, title, description, farm_name, status, close_reason, assigned_team, case_id, created_at, updated_at)
- farmer_problem(id, case_id, problem_description, timestamp)
- issue_attachments(id, case_id, file_name, file_path, uploaded_at)
"""

TELEGRAM_BOT_TOKEN = os.getenv("TECH_TELE_BOT")
user_state = {}

def get_main_menu_buttons():
    keyboard = [
        [
            InlineKeyboardButton("Get Case Summary", callback_data="case_summary"),
            InlineKeyboardButton("Generate Report", callback_data="generate_report"),
            InlineKeyboardButton("View All Issues", callback_data="view_all_issues")
        ],
        [
            InlineKeyboardButton("Close Case", callback_data="close_case")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update):
    await update.message.reply_text(
        "📋 Main Menu: Please choose an option below:",
        reply_markup=get_main_menu_buttons()
    )

# /start and /cancel command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_state.pop(user_id, None)  # Clear the user's state
    await update.message.reply_text("❌ Action cancelled. Returning to the main menu.")
    await show_main_menu(update)

# /generate_dynamic_report command
async def generate_dynamic_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_state[user_id] = {"action": "dynamic_report", "step": "awaiting_prompt"}
    await update.message.reply_text(
    "Type your prompt to generate a report.\n"
    "Send /exit to return to the main menu."
    )

# /exit command for dynamic report
async def exit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id in user_state and user_state[user_id].get("action") == "dynamic_report":
        user_state.pop(user_id, None)
        await update.message.reply_text("🚪 Exiting dynamic report mode.")
        await show_main_menu(update)
    else:
        await update.message.reply_text("❓ You are not in dynamic report mode.")

# Button interactions
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "case_summary":
        user_state[user_id] = {"action": "case_summary", "step": "awaiting_case_id"}
        await query.edit_message_text("📥 Please enter the Case ID for the summary:")

    elif query.data == "generate_report":
        user_state[user_id] = {"action": "generate_report", "step": "awaiting_case_id"}
        await query.edit_message_text("📥 Please enter the Case ID for the full report:")

    elif query.data == "view_all_issues":
        await query.edit_message_text("🔍 Viewing all issues...")
        try:
            result = generate_summary_of_all_issues()
            await query.message.reply_text(f"<pre>{result}</pre>", parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {e}")

        await query.message.reply_text("📋 Main Menu: Please choose an option below:", reply_markup=get_main_menu_buttons())

    elif query.data == "close_case":
        user_state[user_id] = {"action": "closing_case", "step": "awaiting_case_id"}
        await query.edit_message_text("📥 Please enter the Case ID for the case you want to close.")


async def case_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_input = update.message.text.strip()

    if user_id not in user_state:
        await update.message.reply_text("❓ Please choose an action first using the menu.")
        await show_main_menu(update)
        return
    
    if not user_input:
        await update.message.reply_text("❗ Input cannot be empty. Please try again.")
        return
    
    state = user_state[user_id]

    if state["action"] == "closing_case":
        if state["step"] == "awaiting_case_id":
            case_id = user_input.strip()

            if not re.fullmatch(r"[0-9a-fA-F]{8}", case_id):
                await update.message.reply_text("❗ Invalid Case ID format. Please enter the first 8 characters of the case ID.")
                return

            if not check_case_exists(user_input):
                await update.message.reply_text(f"❌ Case ID {user_input} does not exist, please try again.")
                return

            state["case_id"] = user_input
            state["step"] = "awaiting_reason"
            await update.message.reply_text(f"📝 Please provide a reason for closing the case {user_input}:")

        elif state["step"] == "awaiting_reason":
            state["reason"] = user_input
            state["step"] = "waiting_for_upload_or_skip"
            await update.message.reply_text(
                "📤 Upload a document to support your case (optional), or type 'skip' to proceed without a document."
            )

        elif state["step"] == "waiting_for_upload_or_skip":
            if user_input.strip().lower() == "skip":
                reason = state.get("reason", "No reason provided.")
                try:
                    result = execute_case_closing(state["case_id"], reason)
                    await update.message.reply_text(f"✅ Case closed successfully: {result}")
                except Exception as e:
                    await update.message.reply_text(f"❌ Case closure failed: {e}")
                user_state.pop(user_id, None)
                await show_main_menu(update)
            else:
                await update.message.reply_text("❗ Please upload a document or type 'skip' to proceed without one.")

    elif state["action"] == "case_summary":
        case_id = user_input.strip()

        if not re.fullmatch(r"[0-9a-fA-F]{8}", case_id):
            await update.message.reply_text("❗ Invalid Case ID format. Please enter the first 8 characters of the case ID.")
            return
        
        if not check_case_exists(case_id):
            await update.message.reply_text(f"❌ Case ID {case_id} does not exist, please try again.")
            return
        
        await update.message.reply_text("⏳ Generating case summary...")
        result = generate_individual_case_summary(case_id)
        await update.message.reply_text(f"<pre>{result}</pre>", parse_mode="HTML")
        await show_main_menu(update)

    elif state["action"] == "generate_report":
        case_id = user_input.strip()

        if not re.fullmatch(r"[0-9a-fA-F]{8}", case_id):
            await update.message.reply_text("❗ Invalid Case ID format. Please enter the first 8 characters of the case ID.")
            return
        
        if not check_case_exists(case_id):
            await update.message.reply_text(f"❌ Case ID {case_id} does not exist, please try again.")
            return
        
        await update.message.reply_text("⏳ Generating full report...")
        result = generate_report_for_forms(case_id)
        await update.message.reply_text(f"<pre>{result}</pre>", parse_mode="HTML")
        await show_main_menu(update)

    elif state["action"] == "dynamic_report":        
        if state["step"] == "awaiting_prompt":
            user_prompt = user_input

            try:
                case_match = re.search(r"\bcase(?:[\s_]*id)?[:\s#]*?([0-9a-fA-F]{8})\b", user_input, re.IGNORECASE)
                case_id = case_match.group(1) if case_match else None

                await update.message.reply_text("⏳ Generating report from your prompt...")

                result = generate_and_execute_sql(schema=schema, user_input=user_prompt, case_id=case_id)
                report = generate_report_from_prompt(result, case_id=case_id)

                await update.message.reply_text("📝 Here's the report:")
                await update.message.reply_text(f"<pre>{report}</pre>", parse_mode="HTML")

            except Exception as e:
                await update.message.reply_text(f"❌ Failed to generate dynamic report: {e}")

            # 🔁 Do NOT pop user_state so user stays in dynamic mode
            await update.message.reply_text("Type a new prompt or /exit to leave.")

    else:
        await update.message.reply_text("⚠️ Unknown action. Please try again.")
        await show_main_menu(update)

async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_data = user_state.get(user_id)

    if not user_data or "case_id" not in user_data:
        await update.message.reply_text("⚠️ Please enter a valid case ID before uploading.")
        return

    document = update.message.document
    if not document:
        await update.message.reply_text("❌ No file found.")
        return

    file = await context.bot.get_file(document.file_id)
    file_name = document.file_name
    local_file_path = os.path.join(UPLOAD_DIR, file_name)
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    try:
        await file.download_to_drive(local_file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to download the file: {e}")
        return

    await update.message.reply_text("📄 File received. Running analysis...")

    try:
        # Upload to Supabase and get the public URL
        uploaded_file_name, supabase_file_url = upload_file_to_supabase(local_file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to upload to Supabase: {e}")
        return

    try:
        # Run relevance analysis using local file (for content extraction)
        raw_output = run_upload_analysis(user_data["case_id"], local_file_path, uploaded_file_name, supabase_file_url)

        if raw_output.json_dict:
            analysis_result = raw_output.json_dict
        else:
            raw_data = raw_output.raw
            analysis_result = json.loads(raw_data)

        is_relevant = analysis_result.get("is_relevant", False)
        explanation = analysis_result.get("explanation", "No explanation provided by the analysis system.")

        if not is_relevant:
            await update.message.reply_text(
                f"⚠️ The uploaded file is *not relevant* to Case ID {user_data['case_id']}. Case closure aborted.\n\n📄 Explanation: {explanation}",
                parse_mode="Markdown"
            )
            return

        await update.message.reply_text(
            f"✅ File is relevant to Case ID {user_data['case_id']}.\n\n📄 Explanation: {explanation}\n\n🚪 Proceeding to close the case..."
        )

        reason = user_data.get("reason", "No reason provided.")
        try:
            result = execute_case_closing(user_data["case_id"], reason)
            await update.message.reply_text(f"✅ Case closed successfully: {result}")
        except Exception as e:
            await update.message.reply_text(f"❌ Case closure failed: {e}")

    except json.JSONDecodeError:
        await update.message.reply_text("❌ Error: The raw output is not a valid JSON.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error during analysis or closure: {e}")
    finally:
        # Clean up state and local file
        user_state.pop(user_id, None)
        try:
            os.remove(local_file_path)
        except Exception:
            pass
        await show_main_menu(update)

async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Error occurred: {context.error}")

def run_technical_telegram_bot():
    async def main():
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CommandHandler("generate_dynamic_report", generate_dynamic_report_command))
        app.add_handler(CommandHandler("exit", exit_command))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, case_id_handler))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document_upload))
        app.add_error_handler(error)

        print("🚀 Technical Bot is running...")
        await app.run_polling(close_loop=False, stop_signals=None)

    import nest_asyncio
    import asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())

if __name__ == '__main__':
    run_technical_telegram_bot()

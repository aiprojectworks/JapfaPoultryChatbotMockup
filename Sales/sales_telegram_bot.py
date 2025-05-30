import logging
import os
import re
import smtplib
import asyncio
import nest_asyncio
from email.message import EmailMessage
from langchain_openai import ChatOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from supabase import create_client
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from Sales.sales_crew import (
    execute_case_closing,
    check_case_exists,
    generate_individual_case_summary,
    generate_report_for_forms,
    generate_summary_of_all_issues,
    generate_and_execute_sql,
    generate_report_from_prompt,
    execute_case_escalation,
    generate_case_summary_for_email
)

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

TELEGRAM_BOT_TOKEN = os.getenv("SALES_TELE_BOT")
EMAIL_PASSKEY = os.getenv("EMAIL_PASSKEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
user_state = {}
    
def send_escalation_email(case_id: str, reason: str, case_info: str):
    try:
        msg = EmailMessage()
        msg["Subject"] = f"🚨 Escalation Notice: Case #{case_id}"
        msg["From"] = "japfanotifier@gmail.com"
        msg["To"] = "japfanotifier@gmail.com"

        msg.set_content(f"""
A case has been escalated by a sales user.

Case ID: {case_id}
Reason for Escalation:
{reason}

Case Details:
{case_info}

Please review and follow up promptly, thank you.
""")
        html_content = f"""
        <html>
            <head>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        background-color: #f4f4f9;
                        padding: 20px;
                        color: #333;
                    }}
                    .container {{
                        background-color: #ffffff;
                        padding: 20px;
                        border-radius: 10px;
                        box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                    }}
                    h1 {{
                        color: #d9534f;
                    }}
                    .section-title {{
                        margin-top: 20px;
                        font-size: 18px;
                        color: #e67e22;
                    }}
                    .info-box {{
                        background-color: #f9f9f9;
                        border-left: 5px solid #e67e22;
                        padding: 10px;
                        margin-top: 10px;
                        white-space: pre-wrap;
                    }}
                    .footer {{
                        margin-top: 30px;
                        font-size: 12px;
                        color: #aaa;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1 style="margin-bottom: 10px; text-align:center">Technical Escalation Notice</h1>
                    <p><strong>Case ID:</strong>{case_id}</p>
                    <div class="section-title">Reason for Escalation:</div>
                    <div class="info-box">{reason}</div>

                    <div class="section-title">Case Summary:</div>
                    <div class="info-box">{case_info}</div>

                    <p>Please review and follow up promptly. Thank you.</p>
                    <div class="footer">
                    This email was automatically generated by the Japfa Case Management System.
                    </div>
                </div>
            </body>
        </html>
        """

        msg.add_alternative(html_content, subtype="html")

        # Send email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login("japfanotifier@gmail.com", EMAIL_PASSKEY)
            smtp.send_message(msg)

        return True
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return False

# Create inline button layout
def get_main_menu_buttons():
    keyboard = [
        [
            InlineKeyboardButton("Get Case Summary", callback_data="case_summary"),
            InlineKeyboardButton("Generate Report", callback_data="generate_report"),
            InlineKeyboardButton("View All Issues", callback_data="view_all_issues")
        ],
        [
            InlineKeyboardButton("Close Case", callback_data="close_case"),
            InlineKeyboardButton("Escalate Case", callback_data="escalate_case")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update):
    """Send the main menu to the user."""
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

        # Show menu again
        await query.message.reply_text("📋 Main Menu: Please choose an option below:", reply_markup=get_main_menu_buttons())

    elif query.data == "close_case":
        user_state[user_id] = {"action": "closing_case", "step": "awaiting_case_id"}
        await query.edit_message_text("📥 Please enter the Case ID for the case you want to close.")

    elif query.data == "escalate_case":
        user_state[user_id] = {"action": "escalating_case", "step": "awaiting_case_id"}
        await query.edit_message_text("📥 Please enter the Case ID for the case you want to escalate.")

async def case_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_input = update.message.text.strip()

    if user_id not in user_state:
        await update.message.reply_text("❗ Please choose an action first using the menu.")
        await show_main_menu(update)
        return
    
    if not user_input:
        await update.message.reply_text("❗ Input cannot be empty. Please try again.")
        return

    state = user_state[user_id]

    # Handle action based on user state
    if state["action"] == "closing_case":
        if state["step"] == "awaiting_case_id":
            if not re.fullmatch(r"[0-9a-fA-F]{8}", user_input):
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
            reason = state.get("reason", "No reason provided.")
            try:
                result = execute_case_closing(state["case_id"], reason)
                await update.message.reply_text(f"✅ Case closed successfully: {result}")
            except Exception as e:
                await update.message.reply_text(f"❌ Case closure failed: {e}")
            user_state.pop(user_id, None)
            await show_main_menu(update)

    elif state["action"] == "escalating_case":
        if state["step"] == "awaiting_case_id":
            if not re.fullmatch(r"[0-9a-fA-F]{8}", user_input):
                await update.message.reply_text("❗ Invalid Case ID format. Please enter the first 8 characters of the case ID.")
                return

            if not check_case_exists(user_input):
                await update.message.reply_text(f"❌ Case ID {user_input} does not exist.")
                return

            state["case_id"] = user_input
            state["step"] = "awaiting_reason"
            await update.message.reply_text(f"📝 Please enter the reason for escalating the case {user_input}:")

        elif state["step"] == "awaiting_reason":
            reason = user_input
            case_id = state["case_id"]

            case_info = generate_case_summary_for_email(case_id)
            success = send_escalation_email(case_id, reason, case_info)

            if success:
                execute_case_escalation(case_id, reason)
                await update.message.reply_text(f"✅ Case {state['case_id']} has been escalated and the technical team has been notified.")
            else:
                await update.message.reply_text("❌ Failed to send the escalation email. Please try again later.")

            user_state.pop(user_id)
            await show_main_menu(update)

    elif state["action"] == "case_summary":
        # Handle case summary generation
        case_id = user_input.strip()

        if not re.fullmatch(r"[0-9a-fA-F]{8}", case_id):
            await update.message.reply_text("❗ Invalid Case ID format. Please enter the first 8 characters of the case ID.")
            return

        if not check_case_exists(case_id):
            print(f"❌ Case ID {case_id} does not exist.")
            await update.message.reply_text(f"❌ Case ID {case_id} does not exist.")
            return
        
        await update.message.reply_text("⏳ Generating case summary...")
        result = generate_individual_case_summary(case_id)
        await update.message.reply_text(f"<pre>{result}</pre>", parse_mode="HTML")
        await show_main_menu(update)

    elif state["action"] == "generate_report":
        # Handle full report generation
        case_id = user_input.strip()

        if not re.fullmatch(r"[0-9a-fA-F]{8}", case_id):
            await update.message.reply_text("❗ Invalid Case ID format. Please enter the first 8 characters of the case ID.")
            return

        if not check_case_exists(case_id):
            await update.message.reply_text(f"❌ Case ID {case_id} does not exist.")
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

# Log errors
async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Error occurred: {context.error}")

# Launch bot
def run_sales_telegram_bot():
    async def main():
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CommandHandler("generate_dynamic_report", generate_dynamic_report_command))
        app.add_handler(CommandHandler("exit", exit_command))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, case_id_handler))
        app.add_error_handler(error)

        print("🚀 Sales Bot is running...")
        await app.run_polling(close_loop=False, stop_signals=None)

    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())

if __name__ == '__main__':
    run_sales_telegram_bot()

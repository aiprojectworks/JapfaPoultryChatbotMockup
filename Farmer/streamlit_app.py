import sys
import pysqlite3 as sqlite3
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
import subprocess
import threading
import time
import os
from streamlit_autorefresh import st_autorefresh
from farmerV2_cb import *
from farmer_agents import *

LOG_FILE = "logs/bot.log"
BOT_SCRIPT = "farmerV2_cb.py"


# Function to start bot
def start_bot():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.Popen(["python", BOT_SCRIPT], env=env)

# Initial state
if "bot_started" not in st.session_state:
    st.session_state.bot_started = False

# Sidebar controls
st.sidebar.title("🛠️ Bot Control")
if st.sidebar.button("▶️ Start Telegram Bot"):
    if not st.session_state.bot_started:
        st.session_state.bot_started = True
        threading.Thread(target=start_bot, daemon=True).start()
        st.sidebar.success("✅ Bot started.")
    else:
        st.sidebar.info("ℹ️ Bot already running.")

# Initialize session state for message
if "log_clear_time" not in st.session_state:
    st.session_state.log_clear_time = None

# Clear Logs Button
if st.sidebar.button("🧹 Clear Logs"):
    open(LOG_FILE, "w", encoding="utf-8").close()
    st.session_state.log_clear_time = time.time()

# Show temporary message (5 seconds max)
if st.session_state.log_clear_time:
    elapsed = time.time() - st.session_state.log_clear_time
    if elapsed < 5:
        st.sidebar.success("🧼 Logs cleared.")
    else:
        st.session_state.log_clear_time = None  # Clear after 5 seconds
        
        
# Log display title
st.title("📋 Real-time Bot Logs")

# CSS for styled container with auto-scroll
st.markdown(
    """
    <style>
    .log-container {
        background-color: #1e1e1e;
        color: #39ff14;
        padding: 1em;
        border-radius: 8px;
        border: 1px solid #888;
        font-family: monospace;
        font-size: 0.9em;
        height: 500px;
        overflow-y: auto;
        white-space: pre-wrap;
        display: flex;
        flex-direction: column-reverse; /* This is the trick for autoscroll */
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Stream logs
from ansi2html import Ansi2HTMLConverter
conv = Ansi2HTMLConverter()

def stream_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            raw = "".join(f.readlines()[-300:])
            return conv.convert(raw, full=False)
    return "⏳ Waiting for logs..."

log_placeholder = st.empty()


# Auto-refresh every 2 seconds
st_autorefresh(interval=2000, key="logrefresher")

# Display the logs (autorefresh will update it)
logs = stream_logs()
log_placeholder.markdown(f"<div class='log-container'>{logs}</div>", unsafe_allow_html=True)
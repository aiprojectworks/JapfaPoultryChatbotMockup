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
from farmerV2_cb import run_bot
from farmer_agents import *

stop_event = threading.Event()

# Session-based logging instead of file-based
def write_log(message):
    if "logs" not in st.session_state:
        st.session_state.logs = []
    timestamp = time.strftime("%H:%M:%S")
    st.session_state.logs.append(f"[{timestamp}] {message}")

def read_logs():
    return "\n".join(st.session_state.get("logs", []))

# Function to start bot (using imported run_bot, not subprocess)
def start_bot():
    write_log("🔄 Bot is starting...")
    try:
        run_bot(write_log=write_log)  # Inject log handler into your bot logic
    except Exception as e:
        write_log(f"❌ Error: {e}")

# Init state
if "bot_started" not in st.session_state:
    st.session_state.bot_started = False
if "stop_flag" not in st.session_state:
    st.session_state.stop_flag = False
if "log_clear_time" not in st.session_state:
    st.session_state.log_clear_time = None

# Sidebar
st.sidebar.title("🛠️ Bot Control")

if st.sidebar.button("▶️ Start Telegram Bot"):
    if not st.session_state.bot_started:
        st.session_state.bot_started = True
        threading.Thread(
            target=lambda: run_bot(
                write_log=write_log,
                stop_flag=stop_event.is_set
            ),
            daemon=True
        ).start()
        st.sidebar.success("✅ Bot started.")
    else:
        st.sidebar.info("ℹ️ Bot already running.")
        
if st.sidebar.button("🛑 Stop Telegram Bot"):
    if st.session_state.bot_started:
        stop_event.set()
        st.session_state.bot_started = False
        write_log("🛑 Stop requested. Bot will shut down shortly.")
    else:
        st.sidebar.info("ℹ️ Bot is not currently running.")
    st.session_state.bot_started = False

if st.sidebar.button("🧹 Clear Logs"):
    st.session_state.logs = []
    st.session_state.log_clear_time = time.time()

if st.session_state.log_clear_time:
    if time.time() - st.session_state.log_clear_time < 5:
        st.sidebar.success("🧼 Logs cleared.")
    else:
        st.session_state.log_clear_time = None

# Main area
st.title("📋 Real-time Bot Logs")

# Style
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
        flex-direction: column-reverse;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Refresh display
st_autorefresh(interval=2000, key="logrefresher")
logs = read_logs()
st.markdown(f"<div class='log-container'>{logs}</div>", unsafe_allow_html=True)
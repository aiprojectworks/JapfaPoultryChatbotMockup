# JapfaPoultrySystem2025P1

To run the farmer side chatbot:
1. Install requirements.txt

2a. if want to run the farmer bot locally do:
    In terminal:
    ../Farmer >python -m farmerV2_cb.py
   if want to run the streamlit app:
    In terminal:
    ../Farmer >streamlit run streamlit_app.py

2b. if want to run the sales and tech streamlit locally do:


3. Ignore this code at the top of files
try:
    import pysqlite3
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    # Fall back to built-in sqlite3 on Windows or if pysqlite3 is not installed
    import sqlite3
else:
    # Now sqlite3 refers to pysqlite3
    import sqlite3
These are necessary for all the code to run in
Streamlit Cloud

4. Here is some information to begin with.
farmerV2_cb.py - telegram chatbot code
farmer_agents.py - agent ai code
streamlit_app.py - streamlit code



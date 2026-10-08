import os
from dotenv import load_dotenv
import streamlit as st

load_dotenv()  # populates os.environ from a local .env file; no-op if none exists (e.g. in prod)

# Load AWS credentials from Streamlit secrets if available (Streamlit Cloud)
# Falls back to environment variables if set directly (Render, App Runner, EC2, etc.)
try:
    if "aws" in st.secrets:
        os.environ["AWS_ACCESS_KEY_ID"] = st.secrets["aws"]["AWS_ACCESS_KEY_ID"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = st.secrets["aws"]["AWS_SECRET_ACCESS_KEY"]
        os.environ["AWS_DEFAULT_REGION"] = st.secrets["aws"].get("AWS_DEFAULT_REGION", "us-east-1")
except Exception:
    pass  # Credentials will be read from environment variables directly

import audit
import library as lib
import time
import uuid
from io import StringIO
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from datetime import datetime
import csv
import pandas as pd
from io import BytesIO

# Initialize S3 client
s3_client = boto3.client('s3')
bucket_name = 'simplesql-logs-rag'
log_file_key = 'logs.xlsx'


def record_feedback(answer_event_id):
    """st.feedback callback: log a thumbs up/down as its own event, tied to the answer."""
    rating = st.session_state.get(f"feedback_{answer_event_id}")
    if rating is None:
        return
    error = audit.log_feedback(answer_event_id, st.session_state.session_id, "up" if rating == 1 else "down")
    if error:
        st.toast(f"Could not save feedback: {error}")


# Set up the Streamlit page
st.set_page_config(page_title="RAG AI Query")
st.title("RAG AI Query")

# Define the available menu items
menu_items = ["Home", "How To", "Generate SQL Query"]

# Sidebar menu
selected_menu_item = st.sidebar.radio("Menu", menu_items)

# Home page content
if selected_menu_item == "Home":
    st.write("This application allows you to generate SQL queries from natural language input.")
    st.write("")
    st.write("**Get Started** by selecting the button Generate SQL Query !")
    st.write("")
    st.write("")
    st.write("**Disclaimer :**")
    st.write("- Model's response depends on user's input (prompt). Please visit How-to section for writing efficient prompts.")

       
# How-to page content
elif selected_menu_item == "How To":
    st.write("The model's output completely depends on the natural language input. Below are some examples which you can keep in mind while asking the questions.")
    st.write("")
    st.write("")
    st.write("")
    st.write("")
    st.write("**Case 1 :**")
    st.write("- **Bad Input :** Cancelled orders")
    st.write("- **Good Input :** Write a query to extract the cancelled order count for the items which were listed this year")
    st.write("- It is always recommended to add required attributes, filters in your prompt.")
    st.write("**Case 2 :**")
    st.write("- **Bad Input :** I am working on XYZ project. I am creating a new metric and need the sales data. Can you provide me the sales at country level for 2023 ?")
    st.write("- **Good Input :** Write an query to extract sales at country level for orders placed in 2023 ")
    st.write("- Every input is processed as tokens. Do not provide un-necessary details as there is a cost associated with every token processed. Provide inputs only relevant to your query requirement.")

# SQL-AI page content
elif selected_menu_item == "Generate SQL Query":
    # Define the available schema types
    schema_types = ["Schema_Type_A", "Schema_Type_B", "Schema_Type_C"]
    schema_type = st.sidebar.selectbox("Select Schema Type", schema_types)

    if schema_type:
        if not lib.aws_credentials_available():
            st.error(lib.get_aws_setup_message())
            st.stop()

        # Initialize chat history if it doesn't exist in session state
        if 'chat_history' not in st.session_state:
            st.session_state.chat_history = []

        # One id per browser session, so audit events from the same conversation can be grouped
        if 'session_id' not in st.session_state:
            st.session_state.session_id = uuid.uuid4().hex

        # Initialize vector index if it doesn't exist in session state or if schema type has changed
        if 'vector_index' not in st.session_state or 'current_schema' not in st.session_state or st.session_state.current_schema != schema_type:
            try:
                with st.spinner("Indexing document..."):
                    st.session_state.vector_index = lib.get_index(schema_type)
                    st.session_state.current_schema = schema_type
            except (NoCredentialsError, PartialCredentialsError):
                st.error(lib.get_aws_setup_message())
                st.stop()

        # Display chat history
        for message in st.session_state.chat_history:
            with st.chat_message(message["role"]):
                st.markdown(message["text"])
                if message.get("audit_id"):
                    st.feedback("thumbs", key=f"feedback_{message['audit_id']}",
                                on_change=record_feedback, args=(message["audit_id"],))

        # Get user input
        input_text = st.chat_input("Chat with your bot here", max_chars=100)
        
        if input_text:
            # Display user input
            with st.chat_message("user"):
                st.markdown(input_text)

            # Add user input to chat history
            st.session_state.chat_history.append({"role": "user", "text": input_text})

            # Build a fresh agent for this turn (cheap: just wiring, no index rebuild)
            # so captured_results only ever holds this turn's query, not prior turns'.
            # query_log records every SQL attempt (for the audit trail); scope makes db.py
            # enforce the semantic layer's allowed tables/columns, not just the prompt.
            query_log = []
            agent, captured_results = lib.build_agent(
                st.session_state.vector_index,
                query_log=query_log,
                scope=lib.get_allowed_scope(schema_type),
            )

            # The agent decides for itself whether to search the schema, run SQL,
            # ask a clarifying question, or retry after a failed query -- see the
            # tool definitions and system prompt in library.py.
            agent_messages = [
                {"role": message["role"], "content": message["text"]}
                for message in st.session_state.chat_history
            ]
            started = time.time()
            try:
                agent_result = agent.invoke({"messages": agent_messages})
            except (NoCredentialsError, PartialCredentialsError):
                st.error(lib.get_aws_setup_message())
                st.stop()
            latency_s = time.time() - started

            chat_response = lib.extract_final_text(agent_result)

            # Audit trail: one immutable record per answer, including every SQL attempt
            audit_id = audit.new_event_id()
            audit_error = audit.log_answer(
                audit_id, session_id=st.session_state.session_id, schema_type=schema_type,
                model=lib.BEDROCK_CHAT_MODEL_ID, question=input_text, answer=chat_response,
                query_log=query_log, latency_s=latency_s,
            )

            # Display the agent's answer, the real query results if it ran one, and feedback buttons
            with st.chat_message("assistant"):
                st.markdown(chat_response)
                for results_df in captured_results:
                    st.dataframe(results_df)
                st.feedback("thumbs", key=f"feedback_{audit_id}",
                            on_change=record_feedback, args=(audit_id,))
                if audit_error:
                    st.warning(f"Audit log write failed: {audit_error}")

            # Add chatbot response to chat history
            st.session_state.chat_history.append({"role": "assistant", "text": chat_response, "audit_id": audit_id})

            # Log the conversation to S3
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            try:
                # Download the existing log file from S3
                log_file_obj = s3_client.get_object(Bucket=bucket_name, Key=log_file_key)
                log_file_content = log_file_obj['Body'].read()
                df = pd.read_excel(BytesIO(log_file_content))

            except s3_client.exceptions.NoSuchKey:
                # If the log file doesn't exist, create a new one
                df = pd.DataFrame(columns=["User Input", "Model Output", "Timestamp", "Schema Type"])
            except (NoCredentialsError, PartialCredentialsError):
                st.warning("Response generated, but S3 logging was skipped because AWS credentials are not configured.")
                st.stop()

            # Write the new log entry to the DataFrame
            new_row = pd.DataFrame({
                "User Input": [input_text], 
                "Model Output": [chat_response], 
                "Timestamp": [timestamp],
                "Schema Type": [schema_type]
            })
            df = pd.concat([df, new_row], ignore_index=True)
            
            # Save the updated DataFrame to a BytesIO object
            output = BytesIO()
            df.to_excel(output, index=False)
            output.seek(0)
            
            # Upload the updated log file to S3
            try:
                s3_client.put_object(Body=output.getvalue(), Bucket=bucket_name, Key=log_file_key)
            except (NoCredentialsError, PartialCredentialsError):
                st.warning("Response generated, but S3 logging was skipped because AWS credentials are not configured.")
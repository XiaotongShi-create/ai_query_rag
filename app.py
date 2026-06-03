import os
import streamlit as st

# Load AWS credentials from Streamlit secrets if available (Streamlit Cloud)
# Falls back to environment variables if set directly (Render, App Runner, EC2, etc.)
try:
    if "aws" in st.secrets:
        os.environ["AWS_ACCESS_KEY_ID"] = st.secrets["aws"]["AWS_ACCESS_KEY_ID"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = st.secrets["aws"]["AWS_SECRET_ACCESS_KEY"]
        os.environ["AWS_DEFAULT_REGION"] = st.secrets["aws"].get("AWS_DEFAULT_REGION", "us-east-1")
except Exception:
    pass  # Credentials will be read from environment variables directly

import library as lib
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

        # Initialize memory if it doesn't exist in session state
        if 'memory' not in st.session_state:
            st.session_state.memory = lib.get_memory()

        # Initialize chat history if it doesn't exist in session state
        if 'chat_history' not in st.session_state:
            st.session_state.chat_history = []

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

        # Get user input
        input_text = st.chat_input("Chat with your bot here", max_chars=100)
        
        if input_text:
            # Display user input
            with st.chat_message("user"):
                st.markdown(input_text)

            # Add user input to chat history
            st.session_state.chat_history.append({"role": "user", "text": input_text})

            # Get chatbot response
            try:
                chat_response = lib.get_rag_chat_response(input_text=input_text, memory=st.session_state.memory,
                                                           index=st.session_state.vector_index)
            except (NoCredentialsError, PartialCredentialsError):
                st.error(lib.get_aws_setup_message())
                st.stop()
            
            # Display chatbot response
            with st.chat_message("assistant"):
                st.markdown(chat_response)

            # Add chatbot response to chat history
            st.session_state.chat_history.append({"role": "assistant", "text": chat_response})

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
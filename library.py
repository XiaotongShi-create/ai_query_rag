# Import the necessary libraries
import os
import boto3  # AWS SDK for Python
from botocore.exceptions import NoCredentialsError
from langchain.agents import create_agent  # Tool-calling agent loop
from langchain_community.document_loaders import JSONLoader  # Utility to load JSON files
from langchain_aws import ChatBedrockConverse  # Chat interface for Bedrock LLM
from langchain_aws import BedrockEmbeddings  # Embeddings for Titan model
from langchain_classic.indexes import VectorstoreIndexCreator  # Create vector indexes
from langchain_community.vectorstores import FAISS  # Vector store using FAISS library
from langchain_text_splitters import RecursiveCharacterTextSplitter  # Split text into chunks

import db

AWS_REGION = "us-east-1"
BEDROCK_CHAT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

# Create a Boto3 client for Bedrock Runtime
bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    region_name=AWS_REGION
)


def aws_credentials_available():
    session = boto3.Session(region_name=AWS_REGION)
    return session.get_credentials() is not None


def get_aws_setup_message():
    return (
        "AWS credentials were not found. Configure AWS before using Bedrock or S3.\n\n"
        "Options:\n"
        "- Run `aws configure` in a terminal\n"
        "- Or set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optionally `AWS_SESSION_TOKEN`\n"
        "- Or create `~/.aws/credentials` and `~/.aws/config`\n\n"
        f"Also ensure your AWS account has access to Amazon Bedrock in region `{AWS_REGION}`."
    )


def validate_aws_credentials():
    if not aws_credentials_available():
        raise NoCredentialsError()

# Function to get the LLM (Large Language Model)
def get_llm():
    validate_aws_credentials()

    model_kwargs = {  # Configuration for Anthropic model
        "max_tokens": 512,  # Maximum number of tokens to generate
        "temperature": 0.2,  # Sampling temperature for controlling randomness
        "stop_sequences": ["\n\nHuman:"]  # Stop sequence for generation
    }

    llm = ChatBedrockConverse(
        model=BEDROCK_CHAT_MODEL_ID,  # Set the foundation model
        max_tokens=model_kwargs["max_tokens"],
        temperature=model_kwargs["temperature"],
        stop=model_kwargs["stop_sequences"],
        bedrock_client=bedrock_runtime,  # Use the Bedrock runtime client
    )

    return llm

# Function to load the schema file based on the schema type
def load_schema_file(schema_type):
    if schema_type == 'Schema_Type_A':
        schema_file = "Table_Schema_A.json"  # Path to Schema Type A
    elif schema_type == 'Schema_Type_B':
        schema_file = "Table_Schema_B.json"  # Path to Schema Type B
    elif schema_type == 'Schema_Type_C':
        schema_file = "Table_Schema_C.json"  # Path to Schema Type C
    return schema_file

# Function to get the vector index for the given schema type
def get_index(schema_type):
    validate_aws_credentials()

    embeddings = BedrockEmbeddings(model_id=BEDROCK_EMBEDDING_MODEL_ID,
                                   client=bedrock_runtime)  # Initialize embeddings

    db_schema_loader = JSONLoader(
        file_path=load_schema_file(schema_type),  # Load the schema file
        # file_path="Table_Schema_RP.json",  # Uncomment to use a different file
        jq_schema='.',  # Select the entire JSON content
        text_content=False)  # Treat the content as text

    db_schema_text_splitter = RecursiveCharacterTextSplitter(  # Create a text splitter
        separators=["separator"],  # Split chunks at the "separator" string
        chunk_size=10000,  # Divide into 10,000-character chunks
        chunk_overlap=100  # Allow 100 characters to overlap with previous chunk
    )

    db_schema_index_creator = VectorstoreIndexCreator(
        vectorstore_cls=FAISS,  # Use FAISS vector store
        embedding=embeddings,  # Use the initialized embeddings
        text_splitter=db_schema_text_splitter  # Use the text splitter
    )

    db_index_from_loader = db_schema_index_creator.from_loaders([db_schema_loader])  # Create index from loader

    return db_index_from_loader

def _make_search_schema_tool(index):
    retriever = index.vectorstore.as_retriever()

    def search_schema(question: str) -> str:
        """Look up which tables and columns are relevant to a question about the
        Northwind database. Call this before writing SQL if you aren't already
        sure which tables/columns apply."""
        docs = retriever.invoke(question)
        return "\n\n".join(doc.page_content for doc in docs)

    return search_schema


def _make_run_sql_query_tool(captured_results):
    def run_sql_query(sql: str) -> str:
        """Execute exactly one read-only SQL SELECT query (Amazon Redshift syntax)
        against the Northwind database and return the results. If this returns an
        error, read it and try a corrected query -- don't give up after one try."""
        try:
            result_df = db.run_select_query(sql)
        except db.UnsafeQueryError as e:
            return f"Query rejected: {e}"
        except Exception as e:
            return f"Query failed: {e}"

        captured_results.append(result_df)
        if result_df.empty:
            return "Query ran successfully but returned no rows."
        return result_df.to_csv(index=False)

    return run_sql_query


AGENT_SYSTEM_PROMPT = """You are a data analyst assistant for a Northwind sales \
database (customers, orders, order_details, products, categories, employees, \
shippers).

When the user asks a question:
1. If you aren't already sure which tables/columns apply, call search_schema first.
2. If the question is ambiguous or missing information you need (a time range, \
which metric, which entity), ask a short clarifying question in plain text instead \
of calling any tool. Do not guess.
3. Once you have enough information, call run_sql_query with exactly one SELECT \
statement using Amazon Redshift syntax.
4. If run_sql_query returns an error, read it and try a corrected query.
5. Once you have real results, answer in plain language summarizing what you \
found -- don't just repeat the raw table.
"""


def build_agent(index):
    """Builds a tool-calling agent bound to a specific schema index.

    Returns (agent, captured_results): captured_results is a list that the
    run_sql_query tool appends its DataFrame to, so the UI can render the table
    separately from the agent's plain-language answer.
    """
    llm = get_llm()
    captured_results = []
    tools = [_make_search_schema_tool(index), _make_run_sql_query_tool(captured_results)]
    agent = create_agent(model=llm, tools=tools, system_prompt=AGENT_SYSTEM_PROMPT)
    return agent, captured_results


def extract_final_text(agent_result):
    """Pull the plain-text answer out of the last message returned by the agent."""
    last_message = agent_result["messages"][-1]
    content = last_message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)
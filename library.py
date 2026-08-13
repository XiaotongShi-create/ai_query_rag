# Import the necessary libraries
import os
import re
import boto3  # AWS SDK for Python
from botocore.exceptions import NoCredentialsError
from langchain_community.document_loaders import JSONLoader  # Utility to load JSON files
from langchain_aws import ChatBedrockConverse  # Chat interface for Bedrock LLM
from langchain_aws import BedrockEmbeddings  # Embeddings for Titan model
from langchain_classic.memory import ConversationBufferWindowMemory  # Memory to store chat conversations
from langchain_classic.indexes import VectorstoreIndexCreator  # Create vector indexes
from langchain_community.vectorstores import FAISS  # Vector store using FAISS library
from langchain_text_splitters import RecursiveCharacterTextSplitter  # Split text into chunks
from langchain_classic.chains import ConversationalRetrievalChain  # Conversational retrieval chain

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

# Function to get the memory for storing chat conversations
def get_memory():
    memory = ConversationBufferWindowMemory(memory_key="chat_history", return_messages=True)  # Create memory

    return memory

# Template for the question prompt
template = """ Read table information from the context. Each table contains the following information:
- Name: The name of the table
- Description: A brief description of the table
- Columns: The columns of the table, listed under the 'columns' key. Each column contains:
  - Name: The name of the column
  - Description: A brief description of the column
  - Type: The data type of the column
  - Synonyms: Optional synonyms for the column name
- Sample Queries: Optional sample queries for the table, listed under the 'sample_data' key

Given this structure, your task is to provide the SQL query using Amazon Redshift syntax that would retrieve the data for the following question. The produced query should be functional, efficient, and adhere to best practices in SQL query optimization.

Only use tables and columns that appear in the context. Write exactly one query, wrapped in a fenced code block like this:
```sql
SELECT ...
```
Add one short sentence above the code block explaining what the query does.

Question: {}
"""

_SQL_BLOCK_PATTERN = re.compile(r"```sql\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_sql(response_text):
    """Pull the SQL out of a ```sql fenced block in the model's response, if present."""
    match = _SQL_BLOCK_PATTERN.search(response_text)
    if match:
        return match.group(1).strip()
    return None


# Function to get the response from the conversational retrieval chain
def get_rag_chat_response(input_text, memory, index):
    llm = get_llm()  # Get the LLM

    conversation_with_retrieval = ConversationalRetrievalChain.from_llm(
        llm, index.vectorstore.as_retriever(), memory=memory, verbose=True)  # Create conversational retrieval chain

    chat_response = conversation_with_retrieval.invoke({"question": template.format(input_text)})  # Invoke the chain

    return chat_response['answer']  # Return the answer
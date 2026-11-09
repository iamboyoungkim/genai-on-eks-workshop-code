# Loan Buddy v2: credit underwriting agent with RAG over the loan policy documents (Track A, A4 "RAG pipeline").
#
# Differences from module3/credit-validation/credit-underwriting-agent.py (v1):
#   - new tool `retrieve_loan_policies`: Bedrock Titan embedding + Milvus vector search on collection `loan_policies`
#   - the system prompt requires a policy lookup before any decision and policy citations in the answer
#   - the system prompt is passed to the agent itself (v1 put it in the graph input, where create_react_agent ignores it)
#
# Runs on the v1 image (public.ecr.aws/agentic-ai-platforms-on-k8s/loan-buddy-example), which already contains
# utils.py and the MCP servers under /app. rag-agent-deployment.yaml mounts this file and installs the two extra
# packages (langchain-aws, pymilvus) at container start.
#
# Environment (set by deploy-rag-agent.sh):
#   GATEWAY_URL, GATEWAY_MODEL_ACCESS_KEY   LiteLLM endpoint and virtual key
#   LANGFUSE_URL, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
#   S3_BUCKET_NAME, AWS_REGION              image upload bucket (shared with the MCP image processor)
#   MCP_ADDRESS_VALIDATOR, MCP_EMPLOYMENT_VALIDATOR, MCP_IMAGE_PROCESSOR
#   MILVUS_URI                              default http://milvus.milvus.svc.cluster.local:19530
#   LLM_MODEL                               default bedrock/claude-4.5-sonnet
import inspect
import logging
import os

import uvicorn
from fastapi import FastAPI, File, UploadFile
from langchain_aws import BedrockEmbeddings
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from pymilvus import MilvusClient

from utils import encode_image, generate_256_bit_hex_key, store_object

logger = logging.getLogger("loan-buddy-v2")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://milvus.milvus.svc.cluster.local:19530")
COLLECTION = os.environ.get("MILVUS_COLLECTION", "loan_policies")
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
LLM_MODEL = os.environ.get("LLM_MODEL", "bedrock/claude-4.5-sonnet")

model = ChatOpenAI(
    model=LLM_MODEL,
    temperature=0,
    api_key=os.environ.get("GATEWAY_MODEL_ACCESS_KEY", ""),
    base_url=os.environ.get("GATEWAY_URL", ""),
    max_tokens=5000,
    timeout=300,
)

# Langfuse tracing (optional, same behaviour as v1)
langfuse_handler = None
try:
    from langfuse.langchain import CallbackHandler
except ImportError:  # older langfuse SDK
    try:
        from langfuse.callback import CallbackHandler
    except ImportError:
        CallbackHandler = None

if CallbackHandler and all(os.environ.get(k) for k in ("LANGFUSE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
    os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_URL"]
    try:
        langfuse_handler = CallbackHandler()
        logger.info("Langfuse tracing enabled")
    except Exception as e:  # tracing must never block the agent
        logger.warning(f"Could not initialize Langfuse: {e}")
else:
    logger.info("Langfuse tracing disabled")

# MCP servers from module3 (same namespace). Each server name matches the tools it actually serves.
mcp_servers = {
    "address_validation_service": {
        "url": os.getenv("MCP_ADDRESS_VALIDATOR", "http://mcp-address-validator:8000") + "/sse",
        "transport": "sse",
    },
    "income_employment_validation_service": {
        "url": os.getenv("MCP_EMPLOYMENT_VALIDATOR", "http://mcp-employment-validator:8000") + "/sse",
        "transport": "sse",
    },
    "image_processor": {
        "url": os.getenv("MCP_IMAGE_PROCESSOR", "http://mcp-image-processor:8000") + "/sse",
        "transport": "sse",
    },
}

# ---------------------------------------------------------------------------
# RAG: embeddings + native Milvus client, created once and reused across requests
# ---------------------------------------------------------------------------
embeddings = BedrockEmbeddings(model_id=EMBEDDING_MODEL, region_name=AWS_REGION)
milvus_client = MilvusClient(uri=MILVUS_URI)


@tool
def retrieve_loan_policies(query: str) -> str:
    """Retrieve relevant loan policy sections from the vector database.

    Use a short description of what you need to check, for example
    "home improvement loan non-owner residency limit" or "risk score thresholds conditional approval".
    """
    query_vector = embeddings.embed_query(query)
    results = milvus_client.search(
        collection_name=COLLECTION,
        data=[query_vector],
        limit=3,
        output_fields=["text", "source"],
    )
    hits = results[0] if results else []
    if not hits:
        return "No matching loan policies found."
    return "\n\n".join(
        f"[Policy: {hit['entity'].get('source', 'unknown')}]\n{hit['entity'].get('text', '')}" for hit in hits
    )


system_prompt = """You are a credit underwriting AI assistant.

IMPORTANT: Today's date is 1st September 2024. Use this as your reference when evaluating dates on documents.

You will not see the application image. You receive an image_id; pass it to the document tools, which fetch the image.

Follow these steps:
1. Extract the applicant data with the document tools and check document authenticity.
2. Validate income, employment and address with the validation tools.
3. Before making any decision, ALWAYS retrieve the relevant loan policies with the retrieve_loan_policies tool.
   Look up at least the product policy for the loan purpose, the income requirements and the risk assessment
   guidelines. Call the tool again for any rule you are unsure about.
4. Apply the retrieved rules to the validation results and make a decision: APPROVED, CONDITIONAL APPROVAL or DECLINED.

Your decision must cite the specific policy ID and section for every rule you applied (for example
"LP-HI-2024-03 section 4.2"). Do not state any policy rule that you did not retrieve.
Always provide a clear, structured response with your final recommendation and the list of conditions, if any.
"""


def build_agent(tools):
    """Create the ReAct agent with the system prompt, across langgraph versions (prompt= vs state_modifier=)."""
    params = inspect.signature(create_react_agent).parameters
    if "prompt" in params:
        return create_react_agent(model, tools, prompt=system_prompt)
    return create_react_agent(model, tools, state_modifier=system_prompt)


app = FastAPI(title="Loan Buddy v2 - Credit Underwriting Agent with RAG")


@app.post("/api/process_credit_application_with_upload")
async def process_credit_application_with_upload(image_file: UploadFile = File(...)):
    """Upload an application image to S3 and process it with the RAG-enhanced agent."""
    try:
        image_bytes = await image_file.read()
        image_id = generate_256_bit_hex_key()
        if not store_object(encode_image(image_bytes), image_id):
            return {"status": "ERROR", "message": "Failed to store image in S3", "recommendation": "Please try again"}
        logger.info(f"Image stored in S3 with ID: {image_id}")

        client = MultiServerMCPClient(mcp_servers)
        tools = await client.get_tools() + [retrieve_loan_policies]
        logger.info(f"Available tools: {[t.name for t in tools]}")

        graph = build_agent(tools).with_config(
            {
                "run_name": "credit_underwriting_agent_v2_rag",
                "callbacks": [langfuse_handler] if langfuse_handler else [],
                "tags": ["loan-processing", "agent", "langgraph", "rag"],
                "metadata": {
                    "langfuse_session_id": "loan-buddy-v2",
                    "langfuse_tags": ["loan-processing", "agent", "langgraph", "rag"],
                },
                "recursion_limit": 30,
            }
        )

        user_message = HumanMessage(
            content=f"""
        Please process this credit application and provide a comprehensive, policy-compliant credit assessment.

        Image_Id: {image_id}

        Please:
        1. Extract all applicant information from the document using the tools
        2. Verify employment and income information
        3. Verify address information
        4. Retrieve the applicable loan policies
        5. Provide a final credit decision with reasoning and policy citations
        """
        )

        final_message = None
        retrieved_policies = []
        async for state in graph.astream({"messages": [user_message]}, stream_mode="values"):
            message = state["messages"][-1]
            if getattr(message, "name", None) == "retrieve_loan_policies":
                retrieved_policies.append(message.content)
            if isinstance(message, AIMessage) and message.content:
                final_message = message.content

        return {
            "status": "COMPLETED",
            "image_id": image_id,
            "credit_assessment": final_message,
            "policy_lookups": len(retrieved_policies),
            "processing_note": "Processed with MCP tools and policy retrieval from Milvus",
        }
    except Exception as e:
        if "RateLimitError" in str(e) or "429" in str(e):
            logger.warning(f"Rate limit encountered: {e}")
            return {"status": "RATE_LIMITED", "message": "Rate limit encountered. Please try again later."}
        logger.exception("Error processing credit application")
        return {
            "status": "ERROR",
            "message": "An error occurred while processing your application",
            "recommendation": "Check the agent logs: kubectl logs deploy/loan-buddy-agent-v2 -n workshop",
        }


@app.get("/api/policies/search")
async def search_policies(q: str):
    """Debug endpoint: run the retrieval tool directly."""
    return {"query": q, "result": retrieve_loan_policies.invoke({"query": q})}


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "credit_underwriting_agent_v2_rag", "milvus_collection": COLLECTION}


if __name__ == "__main__":
    logger.info("Starting Loan Buddy v2 (RAG) on :8080")
    uvicorn.run(app, host="0.0.0.0", port=8080)

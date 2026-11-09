"""Ingest the loan policy documents in ./policies into the Milvus collection `loan_policies`.

Pipeline: load Markdown -> split into chunks -> embed with Amazon Bedrock Titan Text Embeddings v2 -> store in Milvus.
Re-running is safe: the collection is dropped and recreated (drop_old=True).

Environment:
  AWS_REGION   region for Bedrock (required; the IDE credentials need bedrock:InvokeModel on Titan v2)
  MILVUS_URI   Milvus endpoint (default http://localhost:19530, i.e. a kubectl port-forward to svc/milvus)

Usage:
  kubectl port-forward svc/milvus -n milvus 19530:19530 &
  python3 ingest_policies.py
"""

import os
import sys
from pathlib import Path

from langchain_aws import BedrockEmbeddings
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_text_splitters import RecursiveCharacterTextSplitter

POLICY_DIR = Path(__file__).resolve().parent / "policies"
COLLECTION = "loan_policies"
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530")


def load_documents() -> list[Document]:
    files = sorted(POLICY_DIR.glob("*.md"))
    if not files:
        sys.exit(f"No policy documents found in {POLICY_DIR}")
    print(f"Loading {len(files)} policy documents...")
    # metadata["source"] is the file name; the v2 agent returns it as the policy citation.
    return [Document(page_content=f.read_text(encoding="utf-8"), metadata={"source": f.name}) for f in files]


def main() -> None:
    region = os.environ.get("AWS_REGION")
    if not region:
        sys.exit("Set AWS_REGION (for example: export AWS_REGION=$(aws configure get region))")

    documents = load_documents()

    # 1. Chunk on Markdown headings first so each chunk stays inside one policy section.
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n## ", "\n### ", "\n\n", "\n", " "],
    )
    chunks = splitter.split_documents(documents)
    print(f"Splitting into chunks: {len(chunks)} chunks created")

    # 2. Embeddings via Bedrock Titan (1024 dimensions).
    print("Generating embeddings via Bedrock Titan...")
    embeddings = BedrockEmbeddings(model_id=EMBEDDING_MODEL, region_name=region)

    # 3. Store in Milvus. Text goes to the `text` field and metadata to `source`,
    #    which is what the v2 agent reads with MilvusClient.search(output_fields=["text", "source"]).
    print(f"Storing in Milvus collection '{COLLECTION}' at {MILVUS_URI}...")
    Milvus.from_documents(
        documents=chunks,
        embedding=embeddings,
        connection_args={"uri": MILVUS_URI},
        collection_name=COLLECTION,
        drop_old=True,
        auto_id=True,
    )

    print(f"Ingested {len(chunks)} policy chunks into Milvus")


if __name__ == "__main__":
    main()

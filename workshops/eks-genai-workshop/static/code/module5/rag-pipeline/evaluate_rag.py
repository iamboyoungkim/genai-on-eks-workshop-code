"""Evaluate the loan-policy RAG pipeline with Ragas (Track A, A4 "RAG pipeline", Step 11).

For each question in a small fixed evaluation set: retrieve the top-3 policy chunks from Milvus, ask the judge LLM to
answer using only that context, then score faithfulness, answer relevancy, context precision and context recall.

Runs from the IDE, in the same virtualenv as ingest_policies.py (pip install -r requirements-ingest.txt).

Environment:
  AWS_REGION        region for Bedrock Titan embeddings (required)
  MILVUS_URI        default http://localhost:19530 (kubectl port-forward svc/milvus -n milvus 19530:19530)
  GATEWAY_URL       LiteLLM endpoint, default http://localhost:4000 (kubectl port-forward svc/litellm -n litellm 4000:4000)
  LITELLM_KEY       LiteLLM virtual key (falls back to AGENT_KEY)
  JUDGE_MODEL       default bedrock/claude-4.5-sonnet
"""

import os
import sys

from langchain_aws import BedrockEmbeddings
from langchain_milvus import Milvus
from langchain_openai import ChatOpenAI
from ragas import EvaluationDataset, evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness

# A small, fixed evaluation set keeps the scores reproducible. Each reference answer is taken from ./policies.
EVAL_SET = [
    {
        "question": "What is the minimum and maximum amount for a home improvement loan?",
        "reference": "Home improvement loans range from a minimum of $5,000 to a maximum of $50,000 (LP-HI-2024-03 section 2).",
    },
    {
        "question": "Can an applicant who lives in a property owned by a family member get a home improvement loan, and up to what amount?",
        "reference": "Yes. Non-owner applicants, including those living in a property owned by a family member, may apply for "
        "$15,000 or less, with written consent from the property owner and proof of residency dated within the last 60 "
        "days. A co-signer who owns the property removes the $15,000 cap (LP-HI-2024-03 section 4.2).",
    },
    {
        "question": "What debt-to-income ratio is allowed for a home improvement loan?",
        "reference": "The DTI ratio after the new payment must be 43% or lower for home improvement loans; a DTI between 36% "
        "and the limit adds risk (LP-INC-2024-01 section 2, LP-HI-2024-03 section 3).",
    },
    {
        "question": "Which risk score leads to a conditional approval?",
        "reference": "A risk score from 60 to 79 leads to a conditional approval, which must list every condition to be met "
        "before funding. 80 or higher is approve, below 60 is decline (LP-RISK-2024-04 section 5).",
    },
    {
        "question": "What happens when the document authenticity score is below 75?",
        "reference": "An authenticity score below 75 deducts 15 points from the risk score and requires manual review of the "
        "document before funding (LP-RISK-2024-04 section 2).",
    },
    {
        "question": "What is the minimum income and employment length for a personal loan?",
        "reference": "Personal loans require at least $30,000 annual gross income and 1 year with the current employer or 2 "
        "years in the same field (LP-PL-2024-02 section 3).",
    },
]


def main() -> None:
    region = os.environ.get("AWS_REGION")
    api_key = os.environ.get("LITELLM_KEY") or os.environ.get("AGENT_KEY")
    if not region or not api_key:
        sys.exit("Set AWS_REGION and LITELLM_KEY (or AGENT_KEY) first.")

    embeddings = BedrockEmbeddings(model_id="amazon.titan-embed-text-v2:0", region_name=region)
    vectorstore = Milvus(
        embedding_function=embeddings,
        connection_args={"uri": os.environ.get("MILVUS_URI", "http://localhost:19530")},
        collection_name="loan_policies",
    )
    judge = ChatOpenAI(
        model=os.environ.get("JUDGE_MODEL", "bedrock/claude-4.5-sonnet"),
        base_url=os.environ.get("GATEWAY_URL", "http://localhost:4000"),
        api_key=api_key,
        temperature=0,
        max_tokens=2000,
        timeout=120,
    )

    samples = []
    for item in EVAL_SET:
        # Retrieve the top-3 policy chunks, then ask the judge to answer using only that context.
        docs = vectorstore.similarity_search(item["question"], k=3)
        contexts = [d.page_content for d in docs]
        answer = judge.invoke(
            "Answer using ONLY the policy context. Cite the policy ID and section.\n\nContext:\n"
            + "\n\n".join(contexts)
            + f"\n\nQuestion: {item['question']}"
        ).content
        samples.append(
            {
                "user_input": item["question"],
                "retrieved_contexts": contexts,
                "response": answer,
                "reference": item["reference"],
            }
        )
        print(f"answered: {item['question']}")

    dataset = EvaluationDataset.from_list(samples)

    # answer_relevancy asks for several generations per call by default (n=3). Claude via the gateway does not
    # support n>1, so generate one question per call.
    answer_relevancy.strictness = 1

    results = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
        llm=LangchainLLMWrapper(judge),
        embeddings=LangchainEmbeddingsWrapper(embeddings),
    )

    print("\n=== Ragas RAG Evaluation Results ===")
    print(results)


if __name__ == "__main__":
    main()

# Module 5: RAG pipeline (Loan Buddy v2)

English | [한국어](README.ko.md)

Code for the Track A lab **A4 "Production ready > RAG pipeline"**: loan policy documents are embedded with Amazon
Bedrock Titan Text Embeddings v2, stored in Milvus, and retrieved by Loan Buddy v2 before every credit decision.

| File | Purpose | Runs where |
|---|---|---|
| `policies/*.md` | 4 sample loan policy documents (home improvement, personal loan, income, risk) | - |
| `requirements-ingest.txt` | Python deps for ingestion, the similarity check and Ragas | IDE virtualenv |
| `ingest_policies.py` | Chunk (500/50) -> Titan embeddings -> Milvus collection `loan_policies` (24 chunks) | IDE |
| `credit-underwriting-agent-v2.py` | Loan Buddy v2: module3 agent + `retrieve_loan_policies` tool | cluster (`workshop`) |
| `rag-agent-deployment.yaml` | ServiceAccount, Deployment and ClusterIP Service `loan-buddy-agent-v2` | cluster |
| `deploy-rag-agent.sh` | IAM role, Pod Identity, Secret, ConfigMap, then the Deployment | IDE |
| `evaluate_rag.py` | Ragas faithfulness / answer relevancy / context precision / context recall | IDE |
| `example1.png` | Same sample application as module3 (John Michael Doe, $8,500 home improvement) | - |

## Prerequisites

- A2 "Running application" done: `deployment/loan-buddy-agent` and the three MCP servers run in namespace `workshop`.
  `deploy-rag-agent.sh` copies the LiteLLM key, Langfuse keys, S3 bucket and region from that deployment.
- Milvus installed (lab Step 2).
- The IDE credentials can call `bedrock:InvokeModel` on `amazon.titan-embed-text-v2:0` and create IAM roles and
  Pod Identity associations.

## Run (lab steps 5 to 11)

```bash
cd /workshop/workshops/eks-genai-workshop/static/code/module5/rag-pipeline
export AWS_REGION=$(aws configure get region)

# Step 6: ingest
python3 -m venv .ingest-venv && source .ingest-venv/bin/activate
pip install --quiet -r requirements-ingest.txt
kubectl port-forward svc/milvus -n milvus 19530:19530 >/tmp/milvus-pf.log 2>&1 &
python3 ingest_policies.py

# Step 8: deploy v2
./deploy-rag-agent.sh

# Step 9: test (see the port note below)
kubectl port-forward service/loan-buddy-agent-v2 8081:8080 -n workshop >/tmp/loan-buddy-v2-pf.log 2>&1 &
curl -X POST -F "image_file=@./example1.png" http://localhost:8081/api/process_credit_application_with_upload | jq '.'

# Step 11: Ragas (needs the LiteLLM port-forward on 4000 and a key that may call bedrock/claude-4.5-sonnet)
export LITELLM_KEY=$AGENT_KEY
python3 evaluate_rag.py
```

`evaluate_rag.py` must run in the same virtualenv (`source .ingest-venv/bin/activate`).

## Design notes

- **No custom image.** The v2 Deployment reuses the module3 image (`loan-buddy-example:1.0.4`, which already has
  `utils.py` and the MCP servers) and mounts `credit-underwriting-agent-v2.py` from a ConfigMap. `langchain-aws` and
  `pymilvus` are installed with pip when the container starts, so the pod needs outbound access to PyPI and takes about
  1 to 2 minutes to become Ready. To change the agent code, edit the file and re-run `./deploy-rag-agent.sh`.
- **Own identity, least privilege.** v2 does not reuse the copied `langfuse` service account. It gets
  `ServiceAccount loan-buddy-v2` with the IAM role `loan-buddy-v2`: `s3:GetObject`/`s3:PutObject` on the Langfuse
  image bucket and `bedrock:InvokeModel` on Titan Text Embeddings v2 only. LLM calls go through LiteLLM with the
  virtual key.
- **Secrets.** The LiteLLM and Langfuse keys are stored in `Secret loan-buddy-v2-env`, not in the manifest.
- **System prompt.** v1 passes the system prompt in the graph input, where `create_react_agent` ignores it. v2 passes
  it to the agent (`prompt=`, or `state_modifier=` on older langgraph), so the "retrieve policies first, cite them"
  instruction is actually applied.
- **Index/metric.** Ingestion uses the langchain-milvus defaults (text field `text`, metadata field `source`); the agent
  searches with the native `MilvusClient`, which uses the collection's index metric.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `should create connection first` during ingest | pymilvus 2.6 in the venv. Reinstall from `requirements-ingest.txt` (pins `<2.6`). |
| `AccessDeniedException` on `InvokeModel` (ingest) | The IDE role lacks Bedrock access to Titan v2, or model access is not enabled in this region. |
| v2 pod stuck in startup, logs show pip errors | No outbound access to PyPI from the node subnets. |
| v2 answers with `ERROR`, logs show `NoCredentialProviders` / `AccessDenied` | Pod Identity was not injected. Re-run `./deploy-rag-agent.sh` (it recreates the pod). |
| `collection not found: loan_policies` | Ingest has not run, or ran against another Milvus. |
| `port-forward ... 8081: address already in use` | The Grafana port-forward from module4 also uses 8081. Stop it (`pkill -f "port-forward -n grafana"`) or use another port, for example `8082:8080`, and change the curl URL. |
| Ragas `answer_relevancy` error about `n` | The script sets `answer_relevancy.strictness = 1`; keep it when changing metrics. |

Quick retrieval check without a full agent run:

```bash
curl -s "http://localhost:8081/api/policies/search?q=home%20improvement%20non-owner" | jq -r '.result'
```

## Cleanup

```bash
pkill -f "port-forward service/loan-buddy-agent-v2"; pkill -f "port-forward svc/milvus"
kubectl delete -f rag-agent-deployment.yaml --ignore-not-found
kubectl delete secret/loan-buddy-v2-env configmap/loan-buddy-v2-code -n workshop --ignore-not-found
helm uninstall milvus -n milvus
```

The IAM role `loan-buddy-v2` and its Pod Identity association are not removed by these commands.

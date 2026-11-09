#!/usr/bin/env bash
# Deploy Loan Buddy v2 (RAG) next to the module3 Loan Buddy in namespace `workshop`.
#
# Prerequisites:
#   - A2 "Running application": module3 Loan Buddy (deployment/loan-buddy-agent + MCP servers) is running
#   - Steps 2 to 6 of this page: Milvus is running and `loan_policies` was ingested
#
# What it does (safe to re-run):
#   1. reads the LiteLLM key, Langfuse keys, S3 bucket and region from the module3 agent
#   2. IAM role loan-buddy-v2 (Pod Identity trust) with S3 Get/Put on that bucket + Bedrock InvokeModel on Titan v2
#   3. Pod Identity association workshop/loan-buddy-v2, then waits so the first pod gets credentials
#   4. Secret loan-buddy-v2-env, ConfigMap loan-buddy-v2-code, then rag-agent-deployment.yaml
#   5. verifies the pod received Pod Identity credentials (recreates it once if not)
set -euo pipefail

NS=workshop
SA=loan-buddy-v2
ROLE_NAME="${RAG_AGENT_ROLE:-loan-buddy-v2}"
HERE="$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${YELLOW}==> $*${NC}"; }
ok()   { echo -e "${GREEN}    $*${NC}"; }
die()  { echo -e "${RED}ERROR: $*${NC}" >&2; exit 1; }

[ -f /workshop/.env ] && set -a && . /workshop/.env && set +a
CLUSTER_NAME="${CLUSTER_NAME:-${EKS_CLUSTER_NAME:-genai-on-eks}}"

step "Checking prerequisites"
kubectl get deployment loan-buddy-agent -n "$NS" >/dev/null 2>&1 \
  || die "deployment/loan-buddy-agent not found in namespace $NS. Run the A2 'Running application' lab first."
kubectl get svc milvus -n milvus >/dev/null 2>&1 \
  || die "svc/milvus not found. Run Step 2 (helm install milvus) first."
ok "module3 Loan Buddy and Milvus found (cluster: $CLUSTER_NAME)"

# Read one env var from the module3 agent container.
v1_env() {
  kubectl get deployment loan-buddy-agent -n "$NS" \
    -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='$1')].value}"
}

step "Reading configuration from the module3 agent"
GATEWAY_MODEL_ACCESS_KEY="$(v1_env GATEWAY_MODEL_ACCESS_KEY)"
LANGFUSE_PUBLIC_KEY_V1="$(v1_env LANGFUSE_PUBLIC_KEY)"
LANGFUSE_SECRET_KEY_V1="$(v1_env LANGFUSE_SECRET_KEY)"
S3_BUCKET_NAME="$(v1_env S3_BUCKET_NAME)"
AWS_REGION="$(v1_env AWS_REGION)"
AWS_REGION="${AWS_REGION:-${REGION:-$(aws configure get region)}}"
for v in GATEWAY_MODEL_ACCESS_KEY LANGFUSE_PUBLIC_KEY_V1 LANGFUSE_SECRET_KEY_V1 S3_BUCKET_NAME AWS_REGION; do
  [ -n "${!v}" ] || die "$v is empty on deployment/loan-buddy-agent"
done
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ok "bucket=$S3_BUCKET_NAME region=$AWS_REGION account=$AWS_ACCOUNT_ID"

step "IAM role $ROLE_NAME"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"pods.eks.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}' \
    >/dev/null
  ok "created"
else
  ok "exists"
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name loan-buddy-v2 --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ApplicationImages", "Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject"],
     "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}/*"},
    {"Sid": "TitanEmbeddings", "Effect": "Allow",
     "Action": "bedrock:InvokeModel",
     "Resource": "arn:aws:bedrock:${AWS_REGION}::foundation-model/amazon.titan-embed-text-v2:0"}
  ]
}
EOF
)"
ok "inline policy: s3 Get/Put on ${S3_BUCKET_NAME}/*, bedrock:InvokeModel on Titan Text Embeddings v2"

step "Pod Identity association $NS/$SA"
ASSOC=$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
  --namespace "$NS" --service-account "$SA" --query 'associations[0].associationId' --output text)
if [ -z "$ASSOC" ] || [ "$ASSOC" = "None" ]; then
  aws eks create-pod-identity-association --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$NS" --service-account "$SA" \
    --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}" >/dev/null
  ok "created; waiting 15s for it to become effective"
  sleep 15
else
  ok "exists ($ASSOC)"
fi

step "Secret loan-buddy-v2-env and ConfigMap loan-buddy-v2-code"
kubectl create secret generic loan-buddy-v2-env -n "$NS" \
  --from-literal=GATEWAY_MODEL_ACCESS_KEY="$GATEWAY_MODEL_ACCESS_KEY" \
  --from-literal=LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY_V1" \
  --from-literal=LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY_V1" \
  --from-literal=S3_BUCKET_NAME="$S3_BUCKET_NAME" \
  --from-literal=AWS_REGION="$AWS_REGION" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create configmap loan-buddy-v2-code -n "$NS" \
  --from-file=credit-underwriting-agent-v2.py="$HERE/credit-underwriting-agent-v2.py" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "applied"

step "Deploying Loan Buddy v2"
kubectl apply -f "$HERE/rag-agent-deployment.yaml"
# Pick up a changed ConfigMap/Secret on re-runs.
kubectl rollout restart deployment/loan-buddy-agent-v2 -n "$NS" >/dev/null
echo "    waiting for the pod (installs langchain-aws + pymilvus on start, about 1-2 min)..."
kubectl rollout status deployment/loan-buddy-agent-v2 -n "$NS" --timeout=360s

POD_ENV=$(kubectl get pod -n "$NS" -l app=loan-buddy-agent-v2 --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].spec.containers[0].env[*].name}')
case " $POD_ENV " in
  *" AWS_CONTAINER_CREDENTIALS_FULL_URI "*) ok "Pod Identity credentials injected" ;;
  *)
    echo -e "${YELLOW}    Pod Identity credentials missing, recreating the pod${NC}"
    kubectl rollout restart deployment/loan-buddy-agent-v2 -n "$NS"
    kubectl rollout status deployment/loan-buddy-agent-v2 -n "$NS" --timeout=360s
    ;;
esac

kubectl get pods -n "$NS" -l app=loan-buddy-agent-v2
cat <<EOF

Loan Buddy v2 is running. Next (Step 9):
  kubectl port-forward service/loan-buddy-agent-v2 8081:8080 -n $NS &
  curl -X POST -F "image_file=@./example1.png" http://localhost:8081/api/process_credit_application_with_upload | jq '.'

Quick retrieval check without the full agent run:
  curl -s "http://localhost:8081/api/policies/search?q=home%20improvement%20non-owner" | jq -r '.result'
EOF

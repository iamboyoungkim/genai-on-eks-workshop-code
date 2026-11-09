#!/usr/bin/env bash
# Deploy the in-cluster Grafana (grafana.yaml) with the Amazon Managed Prometheus data source, in an order that
# avoids the Pod Identity race: IAM role -> Pod Identity association -> wait -> admin Secret -> manifest.
# Safe to re-run: existing role, association and admin password are kept.
#
# Required env (set in the A3 "Monitoring & Scaling" lab, step 1):
#   AMP_ENDPOINT   AMP workspace prometheusEndpoint (with trailing slash)
#   AWS_REGION     region of the cluster and the AMP workspace
#   CLUSTER_NAME   EKS cluster name
# Optional:
#   AWS_ACCOUNT_ID (looked up with sts if unset)
#   GRAFANA_ROLE   IAM role name (default grafana-amp-reader)
#   GRAFANA_PORT   local port for the port-forward hint (default 8081, the Workshop Studio IDE /app route)
set -euo pipefail

: "${AMP_ENDPOINT:?set AMP_ENDPOINT (lab step 1)}"
: "${AWS_REGION:?set AWS_REGION}"
: "${CLUSTER_NAME:?set CLUSTER_NAME}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
GRAFANA_ROLE="${GRAFANA_ROLE:-grafana-amp-reader}"
GRAFANA_PORT="${GRAFANA_PORT:-8081}"
NS=grafana
SA=grafana
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> IAM role ${GRAFANA_ROLE} (AmazonPrometheusQueryAccess)"
if ! aws iam get-role --role-name "$GRAFANA_ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$GRAFANA_ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"pods.eks.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}' \
    >/dev/null
fi
aws iam attach-role-policy --role-name "$GRAFANA_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonPrometheusQueryAccess

echo "==> Pod Identity association ${NS}/${SA}"
EXISTING=$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
  --namespace "$NS" --service-account "$SA" --query 'associations[0].associationId' --output text)
if [ -z "$EXISTING" ] || [ "$EXISTING" = "None" ]; then
  aws eks create-pod-identity-association --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$NS" --service-account "$SA" \
    --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${GRAFANA_ROLE}" >/dev/null
  echo "    created; waiting 15s for it to become effective"
  sleep 15
else
  echo "    exists (${EXISTING})"
fi

echo "==> Namespace and admin password Secret"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if ! kubectl get secret grafana-admin -n "$NS" >/dev/null 2>&1; then
  kubectl create secret generic grafana-admin -n "$NS" \
    --from-literal=GF_SECURITY_ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=')" >/dev/null
  echo "    generated a new admin password"
else
  echo "    kept the existing admin password"
fi

echo "==> Applying grafana.yaml"
sed -e "s#__AMP_ENDPOINT__#${AMP_ENDPOINT}#" -e "s#__REGION__#${AWS_REGION}#" "$HERE/grafana.yaml" | kubectl apply -f -
kubectl rollout status deployment/grafana -n "$NS" --timeout=180s

# Pod Identity env vars are injected only at pod creation; recreate once if they are missing.
POD_ENV=$(kubectl get pod -n "$NS" -l app=grafana -o jsonpath='{.items[0].spec.containers[0].env[*].name}')
case " $POD_ENV " in
  *" AWS_CONTAINER_CREDENTIALS_FULL_URI "*) echo "==> Pod Identity credentials injected" ;;
  *)
    echo "==> Pod Identity credentials missing, recreating the pod"
    kubectl rollout restart deployment/grafana -n "$NS"
    kubectl rollout status deployment/grafana -n "$NS" --timeout=180s
    ;;
esac

cat <<EOF

Grafana is ready (ClusterIP only). To open it:

  nohup kubectl port-forward -n ${NS} svc/grafana ${GRAFANA_PORT}:3000 >/tmp/grafana-pf.log 2>&1 &

  Workshop Studio IDE:  https://<your-ide>.cloudfront.net/app/
  Local machine:        http://localhost:${GRAFANA_PORT}/app/
  API base:             http://localhost:${GRAFANA_PORT}/app/api/

  user: admin
  password: kubectl get secret grafana-admin -n ${NS} -o jsonpath='{.data.GF_SECURITY_ADMIN_PASSWORD}' | base64 -d; echo

The IDE /app route is not behind the IDE login. Stop the port-forward when you are done:
  pkill -f "port-forward -n ${NS}"
EOF

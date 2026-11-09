# Module 5: RAG 파이프라인 (Loan Buddy v2)

[English](README.md) | 한국어

Track A 실습 **A4 "Production ready > RAG pipeline"** 코드입니다. 대출 정책 문서를 Amazon Bedrock Titan Text
Embeddings v2로 임베딩해 Milvus에 저장하고, Loan Buddy v2가 신용 결정 전에 관련 정책을 검색합니다.

| 파일 | 용도 | 실행 위치 |
|---|---|---|
| `policies/*.md` | 샘플 대출 정책 문서 4종 (주택 개선, 개인 대출, 소득, 위험 평가) | - |
| `requirements-ingest.txt` | 수집, 유사도 검색 확인, Ragas용 Python 의존성 | IDE virtualenv |
| `ingest_policies.py` | 청크 분할(500/50) → Titan 임베딩 → Milvus 컬렉션 `loan_policies` (24개 청크) | IDE |
| `credit-underwriting-agent-v2.py` | Loan Buddy v2: module3 에이전트 + `retrieve_loan_policies` 도구 | 클러스터 (`workshop`) |
| `rag-agent-deployment.yaml` | ServiceAccount, Deployment, ClusterIP Service `loan-buddy-agent-v2` | 클러스터 |
| `deploy-rag-agent.sh` | IAM 역할, Pod Identity, Secret, ConfigMap 생성 후 Deployment 배포 | IDE |
| `evaluate_rag.py` | Ragas faithfulness / answer relevancy / context precision / context recall 평가 | IDE |
| `example1.png` | module3와 같은 샘플 신청서 (John Michael Doe, $8,500 주택 개선) | - |

## 사전 조건

- A2 "Running application" 완료: `workshop` namespace에 `deployment/loan-buddy-agent`와 MCP 서버 3개가 실행 중이어야
  합니다. `deploy-rag-agent.sh`가 이 Deployment에서 LiteLLM 키, Langfuse 키, S3 버킷, 리전을 가져옵니다.
- Milvus 설치 완료 (실습 Step 2)
- IDE 자격 증명으로 `amazon.titan-embed-text-v2:0`에 `bedrock:InvokeModel`을 호출할 수 있고, IAM 역할과 Pod Identity
  association을 생성할 수 있어야 합니다.

## 실행 (실습 Step 5~11)

```bash
cd /workshop/workshops/eks-genai-workshop/static/code/module5/rag-pipeline
export AWS_REGION=$(aws configure get region)

# Step 6: 수집
python3 -m venv .ingest-venv && source .ingest-venv/bin/activate
pip install --quiet -r requirements-ingest.txt
kubectl port-forward svc/milvus -n milvus 19530:19530 >/tmp/milvus-pf.log 2>&1 &
python3 ingest_policies.py

# Step 8: v2 배포
./deploy-rag-agent.sh

# Step 9: 테스트 (아래 포트 참고사항 확인)
kubectl port-forward service/loan-buddy-agent-v2 8081:8080 -n workshop >/tmp/loan-buddy-v2-pf.log 2>&1 &
curl -X POST -F "image_file=@./example1.png" http://localhost:8081/api/process_credit_application_with_upload | jq '.'

# Step 11: Ragas (LiteLLM 4000 port-forward와 bedrock/claude-4.5-sonnet 호출 가능한 키 필요)
export LITELLM_KEY=$AGENT_KEY
python3 evaluate_rag.py
```

`evaluate_rag.py`는 같은 virtualenv(`source .ingest-venv/bin/activate`)에서 실행해야 합니다.

## 설계 참고

- **별도 이미지 없음:** v2 Deployment는 module3 이미지(`loan-buddy-example:1.0.4`, `utils.py`와 MCP 서버 포함)를
  그대로 쓰고, `credit-underwriting-agent-v2.py`는 ConfigMap으로 마운트합니다. `langchain-aws`와 `pymilvus`는
  컨테이너 시작 시 pip로 설치하므로 파드에서 PyPI로 나가는 연결이 필요하고, Ready까지 1~2분 정도 걸립니다.
  에이전트 코드를 수정한 뒤에는 `./deploy-rag-agent.sh`를 다시 실행하면 됩니다.
- **별도 자격 증명, 최소 권한:** v2는 복사된 `langfuse` service account를 쓰지 않습니다. `ServiceAccount loan-buddy-v2`와
  IAM 역할 `loan-buddy-v2`를 사용하며, 권한은 Langfuse 이미지 버킷의 `s3:GetObject`/`s3:PutObject`와 Titan Text
  Embeddings v2의 `bedrock:InvokeModel`뿐입니다. LLM 호출은 virtual key로 LiteLLM을 거칩니다.
- **시크릿:** LiteLLM 키와 Langfuse 키는 매니페스트가 아니라 `Secret loan-buddy-v2-env`에 저장합니다.
- **System prompt:** v1은 system prompt를 graph 입력에 넣는데, `create_react_agent`는 이 값을 무시합니다. v2는
  에이전트 생성 시 전달하므로(`prompt=`, 구버전 langgraph는 `state_modifier=`) "정책 먼저 검색, 정책 인용" 지시가 실제로
  적용됩니다.
- **인덱스/메트릭:** 수집은 langchain-milvus 기본값(텍스트 필드 `text`, 메타데이터 필드 `source`)을 쓰고, 에이전트는
  native `MilvusClient`로 검색하며 컬렉션 인덱스의 메트릭을 그대로 사용합니다.

## 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| 수집 중 `should create connection first` | venv에 pymilvus 2.6 설치됨. `requirements-ingest.txt`(`<2.6` 고정)로 재설치 |
| 수집 중 `InvokeModel`에서 `AccessDeniedException` | IDE 역할에 Titan v2 Bedrock 권한이 없거나, 해당 리전에서 모델 액세스가 활성화되지 않음 |
| v2 파드가 시작 단계에서 멈추고 로그에 pip 오류 | 노드 서브넷에서 PyPI로 나가는 연결 없음 |
| v2가 `ERROR` 응답, 로그에 `NoCredentialProviders` / `AccessDenied` | Pod Identity 미주입. `./deploy-rag-agent.sh` 재실행 (파드를 다시 생성) |
| `collection not found: loan_policies` | 수집을 실행하지 않았거나 다른 Milvus에 수집함 |
| `port-forward ... 8081: address already in use` | module4 Grafana port-forward도 8081을 사용. Grafana port-forward 종료(`pkill -f "port-forward -n grafana"`) 또는 `8082:8080` 등 다른 포트로 변경 후 curl URL 수정 |
| Ragas `answer_relevancy`에서 `n` 관련 오류 | 스크립트가 `answer_relevancy.strictness = 1`을 설정함. 메트릭 변경 시에도 유지 |

에이전트 전체를 실행하지 않고 검색만 확인하는 방법:

```bash
curl -s "http://localhost:8081/api/policies/search?q=home%20improvement%20non-owner" | jq -r '.result'
```

## 정리

```bash
pkill -f "port-forward service/loan-buddy-agent-v2"; pkill -f "port-forward svc/milvus"
kubectl delete -f rag-agent-deployment.yaml --ignore-not-found
kubectl delete secret/loan-buddy-v2-env configmap/loan-buddy-v2-code -n workshop --ignore-not-found
helm uninstall milvus -n milvus
```

IAM 역할 `loan-buddy-v2`와 Pod Identity association은 위 명령으로 삭제되지 않습니다.

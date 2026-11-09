# Module 4: 인프라 모니터링 매니페스트

[English](README.md) | 한국어

Track A 실습 **A3 "Monitoring & Scaling"**, **A4 "Ray Serve autoscaling"**에서 사용하는 매니페스트입니다.

| 파일 | 배포 내용 | Namespace |
|---|---|---|
| `neuron-monitor.yaml` | inf/trn 노드의 Neuron Monitor DaemonSet (NeuronCore 메트릭, `:8000`) | `kube-system` |
| `adot-collector.yaml` | vLLM과 neuron-monitor를 스크랩해 Amazon Managed Prometheus로 remote-write하는 ADOT collector | `adot` |
| `grafana.yaml` | AMP 데이터 소스(SigV4)가 사전 구성된 인-클러스터 Grafana, `/app/` 경로로 서비스 | `grafana` |
| `deploy-grafana.sh` | IAM 역할, Pod Identity association, 랜덤 admin 비밀번호와 함께 `grafana.yaml` 배포 | `grafana` |

`adot-collector.yaml`과 `grafana.yaml`에는 실습에서 `sed`로 치환하는 placeholder 두 개가 있습니다.

- `__AMP_ENDPOINT__`: AMP 워크스페이스의 `prometheusEndpoint` (끝에 `/` 포함)
- `__REGION__`: AWS 리전

```bash
sed -e "s#__AMP_ENDPOINT__#${AMP_ENDPOINT}#" -e "s#__REGION__#${AWS_REGION}#" adot-collector.yaml | kubectl apply -f -
sed -e "s#__AMP_ENDPOINT__#${AMP_ENDPOINT}#" -e "s#__REGION__#${AWS_REGION}#" grafana.yaml | kubectl apply -f -
```

## AWS 자격 증명 (EKS Pod Identity)

| Service account | 실습에서 사용하는 IAM 역할 | Managed policy |
|---|---|---|
| `adot/adot-collector` | `adot-collector-amp` | `AmazonPrometheusRemoteWriteAccess` |
| `grafana/grafana` | `grafana-amp-reader` | `AmazonPrometheusQueryAccess` |

EKS는 Pod Identity 환경 변수(`AWS_CONTAINER_CREDENTIALS_FULL_URI`, `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`)를 파드가
**생성될 때만**, 그리고 association이 이미 활성화된 경우에만 주입합니다.

- 컨테이너 재시작(CrashLoopBackOff)으로는 반영되지 않으므로 파드 재생성 필요
- 새 association은 반영까지 몇 초가 걸리므로, `create-pod-identity-association` 직후 바로 `rollout restart`하면
  자격 증명 없는 파드가 다시 생성될 수 있음

이 경우 ADOT 로그에 아래 오류가 나타납니다.

```
extensions::sigv4auth: could not retrieve credential provider: ... no EC2 IMDS role found
```

권장 순서입니다. association은 namespace나 service account가 아직 없어도 생성할 수 있습니다.

```bash
aws eks create-pod-identity-association --cluster-name "$CLUSTER_NAME" \
  --namespace adot --service-account adot-collector \
  --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/adot-collector-amp"
sleep 15
sed -e "s#__AMP_ENDPOINT__#${AMP_ENDPOINT}#" -e "s#__REGION__#${AWS_REGION}#" adot-collector.yaml | kubectl apply -f -
```

자격 증명 주입 여부를 확인하고, 주입되지 않았다면 파드를 다시 생성합니다.

```bash
kubectl get pod -n adot -l app=adot-collector -o jsonpath='{.items[0].spec.containers[0].env[*].name}'; echo
kubectl rollout restart deployment/adot-collector -n adot   # AWS_CONTAINER_CREDENTIALS_FULL_URI 가 없을 때만
```

Grafana도 동일합니다(`--namespace grafana --service-account grafana`).

## Grafana 접속

`./deploy-grafana.sh`는 Grafana 설정 전체를 안전한 순서로 실행합니다(IAM 역할 → Pod Identity association → 대기 →
admin Secret → 매니페스트 → 자격 증명 확인). 실습 단계 1의 `AMP_ENDPOINT`, `AWS_REGION`, `CLUSTER_NAME`이 필요하며,
다시 실행해도 안전합니다.

Grafana는 ClusterIP Service이고 **`/app/`** 하위 경로로 서비스합니다. 로컬 포트 **8081**로 port-forward해서 접속합니다.

```bash
nohup kubectl port-forward -n grafana svc/grafana 8081:3000 >/tmp/grafana-pf.log 2>&1 &
```

| 브라우저 위치 | URL |
|---|---|
| Workshop Studio IDE (CloudFront 뒤의 Code Editor) | `https://<your-ide>.cloudfront.net/app/` |
| 로컬 PC | `http://localhost:8081/app/` |

8081과 `/app/`을 쓰는 이유: IDE의 nginx는 `/app/...` 요청을 경로 그대로 `localhost:8081/app/...`로 전달하고, 나머지
요청은 모두 Code Editor로 보냅니다. Code Editor 자체의 `/proxy/<port>/` 경로는 Grafana HTML은 반환하지만 정적 파일은
전달하지 못해서, Grafana가 "failed to load its application files" 화면을 표시합니다.

로그인: 사용자 `admin`, 비밀번호는 `grafana-admin` Secret에 저장된 값입니다. 스크립트가 생성하며, `admin/admin`
기본값은 없습니다.

```bash
kubectl get secret grafana-admin -n grafana -o jsonpath='{.data.GF_SECURITY_ADMIN_PASSWORD}' | base64 -d; echo
```

Grafana HTTP API도 같은 prefix를 사용합니다. `http://admin:admin@localhost:3000/api/...`를 호출하는 실습 명령
(예: A4 Ray 대시보드 import)은 아래처럼 변경합니다.

```bash
GF_PW=$(kubectl get secret grafana-admin -n grafana -o jsonpath='{.data.GF_SECURITY_ADMIN_PASSWORD}' | base64 -d)
GRAFANA="http://admin:${GF_PW}@localhost:8081/app"
curl -s "$GRAFANA/api/datasources/uid/amp/health"
```

보안: IDE의 `/app` 경로는 IDE 로그인을 거치지 **않습니다**. port-forward가 실행되는 동안에는 IDE URL을 아는 누구나
Grafana 로그인 화면에 접근할 수 있습니다. 생성된 비밀번호를 그대로 사용하고, 실습이 끝나면 port-forward를 종료하십시오
(`pkill -f "port-forward -n grafana"`). Grafana에 LoadBalancer나 Ingress를 추가하지 마십시오.

### 데이터 소스와 대시보드 쿼리

- 데이터 소스는 새로 추가하지 않고, 사전 구성된 **Amazon Managed Prometheus**(uid `amp`)를 사용합니다.
  Prometheus server URL 입력칸의 `http://localhost:9090`은 예시 값입니다.
- 쿼리 입력칸 하나에는 PromQL 식 하나만 입력합니다. P50/P95/P99처럼 식이 여러 개인 패널은 **+ Add query**로 쿼리를
  나눠야 하며, 한 칸에 여러 줄을 넣으면 `parse error: unexpected identifier "histogram_quantile"` 오류가 발생합니다.
- 여러 파드의 버킷을 합치려면 `sum by (le)`를 사용합니다.
  `histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))`

## 정리

```bash
pkill -f "port-forward -n grafana"
kubectl delete -f grafana.yaml --ignore-not-found   # namespace와 함께 grafana-admin Secret도 삭제
kubectl delete -f adot-collector.yaml --ignore-not-found
kubectl delete -f neuron-monitor.yaml --ignore-not-found
```

AMP 워크스페이스, IAM 역할, Pod Identity association은 실습에서 AWS CLI로 생성한 리소스라 위 명령으로 삭제되지 않습니다.

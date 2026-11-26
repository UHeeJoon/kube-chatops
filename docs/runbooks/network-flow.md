# 정책에 막힌 연결, 포트 스캔, 예상 밖 외부 연결 (네트워크 흐름)

관련 알림: `DemoFlowPolicyDenied`, `DemoPortScan`, `DemoUnexpectedEgress`.

## 증상

- `DemoFlowPolicyDenied`: demo 의 워크로드가 정책에 없는 곳으로 나가려다 5분에 3번 넘게 막혔다.
- `DemoPortScan`: 한 워크로드가 5분에 20번 넘게 막혔다.
- `DemoUnexpectedEgress`: demo 에서 클러스터 밖으로 실제로 연결이 나갔다 (DNS 제외).

## 확인

- 어디로 가려 했나: `sum by (destination_namespace, destination_workload) (increase(hubble_drop_total{source_namespace="demo", reason="POLICY_DENIED"}[15m]))`. 목적지가 비어 있으면 외부이거나 다른 노드의 파드다.
- 외부 목적지와 포트: `sum by (destination, port, verdict) (increase(hubble_flows_to_world_total{source="demo"}[15m]))`.
- 그 파드에서 무슨 일이 있었나: mcp-loki `security_events` (같은 파드의 커널 보안 이벤트).
- 평소 demo 의 정책 차단은 0 이다. 한 건이라도 있으면 평소와 다른 것이다.

## 판단

- 다른 네임스페이스의 서비스(n8n, keycloak, DB)로 가려 했으면 횡이동 시도다.
- 외부 IP 로 가려 했으면 명령 서버 연결이나 추가 도구 내려받기 시도다.
- 여러 포트를 연달아 두드렸으면 스캔이다.
- 배포 직후이고 새 의존 서비스가 생겼다면 정책에 그 경로를 추가하지 않은 설정 실수일 수 있다.
- `DemoUnexpectedEgress` 는 막힌 것이 아니라 나간 것이다. 정책에 구멍이 있다.

## 조치

- 차단은 이미 됐다. 그 파드가 침해됐는지가 핵심이다. 커널 보안 이벤트가 함께 있으면 침해로 보고 격리를 권한다.
- 설정 실수면 네트워크 정책에 필요한 경로를 추가한다.
- `DemoUnexpectedEgress` 는 어느 정책이 허용했는지 찾아 닫는다.

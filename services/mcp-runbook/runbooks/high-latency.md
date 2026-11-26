# 응답 지연 급증 (demo-app)

관련 알림: `DemoHighLatency`. 장애 시나리오 S2.

## 증상

- p95 응답 시간이 평소(약 10ms)의 수십 배로 오른다. 오류율은 그대로일 수 있다.
- 이상 탐지 모델에서 `p95_log_ms` 의 z-score 가 4 를 넘는다.
- 호출하는 쪽에 timeout 로그가 보인다.

## 확인

- p95: `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{namespace="demo"}[2m])))`.
- 모든 파드가 느린지, 한 파드만 느린지 (`by (pod)` 로 나눠 본다). 한 파드만이면 그 파드나 그 노드 문제다.
- CPU 제한: `container_cpu_cfs_throttled_periods_total` 이 늘면 CPU limit 에 걸린 것이다.
- 트래픽이 함께 늘었는지. 늘었으면 용량 부족이다.
- 주입 여부: `demo_chaos_active{kind="latency"}`.

## 원인 후보

- CPU 부족 또는 throttling.
- 트래픽 급증으로 파드 수가 모자람.
- 느린 의존 서비스 (DB, 외부 API).
- 최근 배포의 성능 저하.

## 조치

- 트래픽 증가가 원인이면 `scale` (replicas 늘리기).
- 한 파드만 느리면 그 파드를 `restart`.
- 배포 직후면 `rollback`.
- 시험 주입이면 `/chaos/reset`.

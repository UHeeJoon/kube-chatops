# 오류율 급증 (demo-app, 5xx)

관련 알림: `DemoHighErrorRate`. 장애 시나리오 S3.

## 증상

- 5xx 응답 비율이 평소 0 에서 수 % 이상으로 오른다.
- 이상 탐지 모델에서 `error_ratio` 의 z-score 가 크게 오른다.
- 앱 로그에 스택트레이스나 `500` 이 반복된다.

## 확인

- 오류율: `sum(rate(http_requests_total{namespace="demo", status=~"5.."}[2m])) / sum(rate(http_requests_total{namespace="demo"}[2m]))`.
- 어떤 경로에서 나는지 (`by (path)`). 한 경로만이면 그 기능의 문제다.
- 로그의 오류 문구 (Loki `{namespace="demo", container="app"} |= "ERROR"`). 같은 예외가 반복되는지 본다.
- 시작 시각과 최근 배포 시각이 겹치는지 (rollout 이력).
- 주입 여부: `demo_chaos_active{kind="error"}`.

에이전트가 조사를 시작할 때 미리 실행하는 조회 (도구 이름과 인자, 한 줄에 하나):

```checks
prometheus__query_range {"query": "sum(rate(http_requests_total{namespace=\"demo\", status=~\"5..\"}[2m])) / sum(rate(http_requests_total{namespace=\"demo\"}[2m]))", "minutes": 30}
prometheus__query_instant {"query": "sum by (path, pod) (rate(http_requests_total{namespace=\"demo\", status=~\"5..\"}[2m]))"}
loki__error_summary {"namespace": "demo", "minutes": 15}
prometheus__query_instant {"query": "demo_chaos_active{namespace=\"demo\"}"}
k8s__rollout_history {"name": "demo-app", "namespace": "demo"}
```

## 원인 후보

- 잘못된 배포 (코드 오류, 잘못된 환경 변수).
- 의존 서비스 장애 (DB 연결 실패 등).
- 자원 부족으로 인한 실패 (메모리, 연결 수).

## 조치

- 배포 직후 시작됐으면 `rollback` 이 가장 빠르다.
- 의존 서비스 문제면 그 서비스를 먼저 본다. 앱 재시작은 도움이 안 된다.
- 시험 주입이면 `/chaos/reset`.

# 이상 탐지 멈춤과 모델 승격·되돌리기

관련 알림: `AnomalyMonitorStale`, `AnomalyMonitorAbsent`.

## 증상

- `AnomalyMonitorStale`: 탐지기(mcp-anomaly)가 5분 넘게 판정에 성공하지 못했다.
- 탐지기 로그에 `monitor failed: ... 500 Internal Server Error`.
- 모델을 승격한 직후에 시작됐다면 새 모델과 예측 서버가 맞지 않는 것이다.

## 확인

- 예측 서버 로그: `kubectl -n mlops logs deploy/demo-anomaly-predictor -c kserve-container`. `Model is missing inputs [...]` 이면 모델이 요구하는 입력 열을 서버가 넘기지 않은 것이다.
- 지금 서빙 중인 모델: 지표 `anomaly_model_info` 의 `model_version`, MLflow 의 `champion` 별칭.
- 탐지기 지표: `time() - anomaly_monitor_last_success_timestamp_seconds`.
- InferenceService 가 Ready 인지 (`kubectl -n mlops get inferenceservice`).

에이전트가 조사를 시작할 때 미리 실행하는 조회 (도구 이름과 인자, 한 줄에 하나):

```checks
anomaly__detect_anomalies {"minutes": 60}
prometheus__list_firing_alerts {"namespace": "demo"}
```

## 겪은 사례 (2026-11-27)

- v8 승격 직후 예측이 전부 500 이었다. 예측 서버가 입력 열을 4개로 고정해 두어, 5개(`mem_growth` 포함)를 요구하는 모델에 열을 버리고 넘겼다. 15분 동안 이상 탐지가 멈췄다.
- 서버가 모델의 입력 스키마에서 열을 읽도록 고쳤다.

## 조치

- 급하면 이전 버전으로 되돌린다: Slack 의 이전 학습 결과 카드에서 `vN 로 되돌리기` 버튼 (operator 만).
- 승격은 사람의 결정이다. 에이전트는 승격·되돌리기를 실행하지 않고 권고만 한다.
- 승격 뒤 15분 안에 새 모델로 응답하지 않으면 champion 별칭은 옮기지 않는다. 서빙 모델과 별칭이 어긋났는지 확인한다.

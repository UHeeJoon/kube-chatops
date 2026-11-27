# 이상 탐지 멈춤과 모델 승격·되돌리기

관련 알림: `DemoAnomalyUnexplained`, `DemoSinglePodAnomaly`, `AnomalyBaselineDrift`, `AnomalyMonitorStale`, `AnomalyMonitorAbsent`.

## 증상

- `AnomalyMonitorStale`: 탐지기(mcp-anomaly)가 5분 넘게 판정에 성공하지 못했다.
- `DemoSinglePodAnomaly`: 서비스 합계는 멀쩡해 보이는데 파드 하나만 이상으로 판정된다.
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
prometheus__query_instant {"query": "demo_chaos_active{namespace=\"demo\"}"}
prometheus__query_instant {"query": "sum by (pod) (rate(http_requests_total{namespace=\"demo\", status=~\"5..\"}[5m])) / sum by (pod) (rate(http_requests_total{namespace=\"demo\"}[5m]))"}
```

## 겪은 사례 (2026-11-27)

- v8 승격 직후 예측이 전부 500 이었다. 예측 서버가 입력 열을 4개로 고정해 두어, 5개(`mem_growth` 포함)를 요구하는 모델에 열을 버리고 넘겼다. 15분 동안 이상 탐지가 멈췄다.
- 서버가 모델의 입력 스키마에서 열을 읽도록 고쳤다.

## 판단

- `DemoAnomalyUnexplained`: 모델은 이상이라는데 지연·오류율·메모리 알림이 없다 (요청 급감이 대표적이다. `rule="traffic_drop"` 과 한 시간 전 같은 시점 대비 비율 `anomaly_model_rps_vs_prev_cycle` 을 본다). `detect_anomalies` 의 main_signal 과 z 로 어느 지표가 얼마나 벗어났는지 보고, 그 지표를 직접 조회해 실제 변화인지 확인한다. 실제 변화가 없고 z 만 한쪽으로 치우쳐 있으면 기준 이동이다 (아래).
- `AnomalyBaselineDrift`: 특성 하나의 z 가 6시간 중앙값으로 2 를 넘은 채 머문다. 장애가 아니라 평소 수준이 바뀐 것이다 (배포로 메모리 사용량이 달라짐, 트래픽 수준 변화). 그대로 두면 작은 변화에도 이상으로 판정하거나 반대로 무뎌진다. 재학습 대상이다.
- `DemoSinglePodAnomaly`: 서비스 합계는 알림 기준 아래인데 파드 하나의 값만 벗어나 있다 (예: 파드 둘 중 하나가 오류 6% → 합계 3%). 알림의 pod 와 feature 가 대상이다. 그 파드의 로그(`search_logs` 에 pod 지정)와 상태, 재시작 이력을 다른 파드와 비교한다. 그 파드에만 장애 주입 표시(`demo_chaos_active`)가 켜져 있는지도 본다. 한 파드만의 문제이고 원인이 그 파드의 상태(메모리, 주입된 장애)면 재시작이 조치 후보다.
- `detect_anomalies` 의 `pods` 에 일부 파드만 나오면 서비스 전체가 아니라 그 파드의 문제다. 그 파드의 로그와 상태부터 본다.
- 그 특성에 대한 알림(`DemoHighLatency`, `DemoMemoryNearLimit` 등)이 함께 울리고 있으면 기준 이동이 아니라 오래가는 장애다. 그 알림부터 본다.

## 조치

- 기준 이동이면 재학습이 조치다. 보고서의 조치 유형은 `retrain` 이고, operator 가 `재학습 시작` 버튼으로 승인하면 학습이 돌아 challenger 로 등록된다. 결과 카드의 오탐·재현율을 본 뒤 사람이 승격한다.
- 재학습은 서빙 중인 모델을 바꾸지 않는다. 바뀌는 것은 승격했을 때뿐이다.

- 급하면 이전 버전으로 되돌린다: Slack 의 이전 학습 결과 카드에서 `vN 로 되돌리기` 버튼 (operator 만).
- 승격은 사람의 결정이다. 에이전트는 승격·되돌리기를 실행하지 않고 권고만 한다.
- 승격 뒤 15분 안에 새 모델로 응답하지 않으면 champion 별칭은 옮기지 않는다. 서빙 모델과 별칭이 어긋났는지 확인한다.

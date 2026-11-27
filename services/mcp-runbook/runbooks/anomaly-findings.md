# 이상 탐지 모델의 판정 알림 (모델만 이상, 파드 하나만 이상, 기준 이동)

관련 알림: `DemoAnomalyUnexplained`, `DemoSinglePodAnomaly`, `AnomalyBaselineDrift`. 탐지기나 예측 서버가 멈춘 경우는 `anomaly-monitor.md` 를 본다.

## 증상

- `DemoAnomalyUnexplained`: 모델은 5분 넘게 이상이라고 하는데 지연·오류율·메모리 알림은 울리지 않는다. 임계값 규칙이 모르는 종류의 변화다.
- `DemoSinglePodAnomaly`: 서비스 합계는 멀쩡해 보이는데 파드 하나의 값만 평소에서 벗어나 있다.
- `AnomalyBaselineDrift`: 장애는 없는데 어떤 지표가 여섯 시간 넘게 평소보다 높거나 낮은 채로 머문다.
- 들어오는 요청이 평소 이 시간대의 절반 아래로 줄었다 (오류는 없다).

## 확인

- `detect_anomalies`: 지금과 최근 구간의 main_signal, z, 그리고 `pods` (파드별로 따로 채점했을 때 이상인 파드).
- 요청 급감이면 `anomaly_model_is_anomaly{rule="traffic_drop"}` 과 한 시간 전 같은 시점 대비 비율 `anomaly_model_rps_vs_prev_cycle` (0.6 아래면 급감).
- 파드 하나만 이상이면 그 파드의 오류율·지연을 다른 파드와 나란히 보고, 그 파드에만 장애 주입 표시(`demo_chaos_active`)가 켜져 있는지 본다.
- 함께 울리는 알림이 있는지. 그 지표에 대한 알림이 이미 울리고 있으면 그쪽이 본래의 사건이다.

에이전트가 조사를 시작할 때 미리 실행하는 조회 (도구 이름과 인자, 한 줄에 하나):

```checks
anomaly__detect_anomalies {"minutes": 60}
prometheus__list_firing_alerts {"namespace": "demo"}
prometheus__query_instant {"query": "demo_chaos_active{namespace=\"demo\"}"}
prometheus__query_instant {"query": "sum by (pod) (rate(http_requests_total{namespace=\"demo\", status=~\"5..\"}[5m])) / sum by (pod) (rate(http_requests_total{namespace=\"demo\"}[5m]))"}
k8s__get_pod_status {"namespace": "demo"}
```

## 판단

- `DemoAnomalyUnexplained`: 모델은 이상이라는데 지연·오류율·메모리 알림이 없다 (요청 급감이 대표적이다. `rule="traffic_drop"` 과 한 시간 전 같은 시점 대비 비율 `anomaly_model_rps_vs_prev_cycle` 을 본다). `detect_anomalies` 의 main_signal 과 z 로 어느 지표가 얼마나 벗어났는지 보고, 그 지표를 직접 조회해 실제 변화인지 확인한다. 실제 변화가 없고 z 만 한쪽으로 치우쳐 있으면 기준 이동이다 (아래).
- `DemoSinglePodAnomaly`: 서비스 합계는 알림 기준 아래인데 파드 하나의 값만 벗어나 있다 (예: 파드 둘 중 하나가 오류 6% → 합계 3%). 알림의 pod 와 feature 가 대상이다. 그 파드의 로그(`search_logs` 에 pod 지정)와 상태, 재시작 이력을 다른 파드와 비교한다. 그 파드에만 장애 주입 표시(`demo_chaos_active`)가 켜져 있는지도 본다. 한 파드만의 문제이고 원인이 그 파드의 상태(메모리, 주입된 장애)면 재시작이 조치 후보다.
- `detect_anomalies` 의 `pods` 에 일부 파드만 나오면 서비스 전체가 아니라 그 파드의 문제다. 그 파드의 로그와 상태부터 본다.
- `AnomalyBaselineDrift`: 특성 하나의 z 가 6시간 중앙값으로 2 를 넘은 채 머문다. 장애가 아니라 평소 수준이 바뀐 것이다 (배포로 메모리 사용량이 달라짐, 트래픽 수준 변화). 그대로 두면 작은 변화에도 이상으로 판정하거나 반대로 무뎌진다. 재학습 대상이다.
- 그 특성에 대한 알림(`DemoHighLatency`, `DemoMemoryNearLimit` 등)이 함께 울리고 있으면 기준 이동이 아니라 오래가는 장애다. 그 알림부터 본다.

## 겪은 사례 (2026-11-28)

- 파드 둘 중 하나에 오류 6% 를 넣었다. 합계는 3% 라 `DemoHighErrorRate`(5%)에 닿지 않았고 합계 z 는 2.9 였는데, 파드별 채점은 4분에 그 파드를 z 5.3 으로 짚었다.
- 요청을 절반으로 줄였을 때 예전 모델은 8분 동안 판정하지 못했다. 한 시간 전 같은 시점과 비교하는 규칙을 넣은 뒤에는 5분에 판정한다.

## 조치

- 파드 하나만의 문제이고 원인이 그 파드의 상태(메모리, 주입된 장애)면 `restart` 가 후보다. 원인이 최근 배포면 `rollback` 이다 (`bad-deploy.md`).
- 요청 급감은 서비스가 아니라 들어오는 쪽(부하 생성기, 앞단 WAF, 게이트웨이)을 본다 (`traffic-drop.md`). 서비스에 할 조치는 없다.
- 기준 이동이면 재학습이 조치다. 보고서의 조치 유형은 `retrain` 이고, operator 가 `재학습 시작` 버튼으로 승인하면 학습이 돌아 challenger 로 등록된다. 결과 카드의 오탐·재현율을 본 뒤 사람이 승격한다.
- 재학습은 서빙 중인 모델을 바꾸지 않는다. 바뀌는 것은 승격했을 때뿐이다.

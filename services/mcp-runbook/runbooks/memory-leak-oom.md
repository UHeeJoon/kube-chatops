# 메모리 누수와 OOM 재시작 (demo-app)

관련 알림: `DemoMemoryNearLimit`, `DemoMemoryLeakSuspected`, `DemoPodRestarting`. 장애 시나리오 S1.

## 증상

- 파드 메모리가 한도(limit) 쪽으로 계속 오른다. 요청 수는 평소와 같다.
- 한도에 닿으면 컨테이너가 OOMKilled 로 재시작한다. 재시작 직후 메모리는 낮아졌다가 다시 오른다.
- 파드가 죽었다 살아나기를 주기적으로 반복하고, 그때마다 메모리(램) 사용량이 다시 늘어난다.
- 이상 탐지 모델에서는 `mem_growth`(분당 증가 속도) 나 `mem_ratio` 의 z-score 가 먼저 오른다.

## 확인

- `DemoMemoryLeakSuspected` 는 이상 탐지 모델이 메모리 증가 속도를 평소보다 빠르다고 본 것이다 (분당 한도의 2% 이상이 5분). 아직 한도 80% 전이므로 지금 사용률과 기울기로 한도까지 남은 시간을 어림한다.

- 메모리 추세: `container_memory_working_set_bytes{namespace="demo", container="app"}` 를 한도(`kube_pod_container_resource_limits`)로 나눈 값. 계단이 아니라 꾸준한 기울기면 누수다.
- 재시작 이유: 파드의 마지막 종료 상태가 `OOMKilled` 인지 (`kubectl -n demo describe pod`, Last State).
- 트래픽: `sum(rate(http_requests_total{namespace="demo"}[5m]))` 가 평소 수준인지. 트래픽이 늘어 메모리가 는 것이면 누수가 아니라 용량 문제다.
- 주입 여부: `demo_chaos_active{kind="leak"}` 가 1 이면 시험으로 넣은 누수다.

에이전트가 조사를 시작할 때 미리 실행하는 조회 (도구 이름과 인자, 한 줄에 하나):

```checks
prometheus__query_range {"query": "max by (pod) (container_memory_working_set_bytes{namespace=\"demo\", container=\"app\"}) / max by (pod) (kube_pod_container_resource_limits{namespace=\"demo\", container=\"app\", resource=\"memory\"})", "minutes": 60}
k8s__get_pod_status {"namespace": "demo", "label_selector": "app.kubernetes.io/name=demo-app"}
prometheus__query_instant {"query": "sum(rate(http_requests_total{namespace=\"demo\"}[5m]))"}
prometheus__query_instant {"query": "demo_chaos_active{namespace=\"demo\"}"}
```

## 원인 후보

- 애플리케이션 누수 (요청마다 해제되지 않는 객체). 배포 직후 시작됐으면 그 배포를 의심한다.
- 한도가 실제 사용량보다 작게 잡힘. 재시작 주기가 일정하고 트래픽에 비례하면 이쪽이다.

## 조치

- 급한 불: `restart` (deployment/demo-app 롤링 재시작). 메모리가 초기화돼 시간을 번다. 근본 해결은 아니다.
- 배포 직후 시작됐으면 `rollback`.
- 시험 주입이면 `/chaos/reset` 으로 해제한다.
- 재시작만 반복하면 같은 일이 다시 온다. 재시작 간격(누수 속도)을 보고서에 적는다.

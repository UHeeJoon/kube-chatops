# 파드가 뜨지 않음 (자원 부족으로 Pending, Evicted)

노드는 정상인데 새 파드가 스케줄되지 않는 경우. 노드 장애는 '노드 장애 또는 drain' 런북을 본다.

## 증상

- 파드가 Pending 에 머문다. 이벤트에 `FailedScheduling: Insufficient memory` 또는 `Insufficient cpu`.
- 실행 중이던 파드가 `Evicted` 로 쫓겨난다 (노드 메모리·디스크 압박).
- 증설(scale) 승인 뒤 새 파드만 Pending 인 경우가 흔하다.

## 확인

- Pending 이유: 파드 이벤트의 FailedScheduling 문구.
- 노드 여유: `kube_node_status_allocatable` 대비 요청 합계. 이 클러스터는 노드 2대라 여유가 크지 않다.
- 노드 압박: `kube_node_status_condition{condition=~"MemoryPressure|DiskPressure", status="true"}`.
- 네임스페이스 한도: ResourceQuota 에 걸렸는지 (이벤트에 `exceeded quota`).

## 원인 후보

- 요청(request)이 실제 사용량보다 크게 잡혀 노드를 다 차지함.
- 증설로 replicas 가 노드 용량을 넘음.
- 다른 워크로드가 메모리를 많이 써 노드가 압박 상태.

## 조치

- 증설이 원인이면 replicas 를 되돌린다 (배포 저장소 커밋).
- 자동 조치 대상이 아니다. 어느 자원이 얼마나 모자란지 보고한다.
- 재시작은 도움이 안 된다. 새 파드도 같은 이유로 뜨지 못한다.

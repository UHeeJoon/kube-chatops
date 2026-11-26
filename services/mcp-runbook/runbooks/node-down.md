# 노드 장애 또는 drain (파드 Pending)

장애 시나리오 S5. 노드는 2대(kube-master, kube-worker)다.

## 증상

- 한 노드가 NotReady 이거나 cordon/drain 됐다.
- 그 노드에 있던 파드가 다른 노드로 가지 못하고 Pending 이다.
- Longhorn 볼륨을 쓰는 파드는 볼륨이 원래 노드에 묶여 있어 더 오래 걸린다.

## 확인

- 노드 상태: `kubectl get nodes`, `kube_node_status_condition{condition="Ready"}`.
- Pending 이유: 파드 이벤트의 `FailedScheduling` 문구 (자원 부족, 노드 선택 조건, 볼륨 노드 고정).
- 남은 노드의 여유: 요청(request) 합계가 할당 가능량을 넘는지.
- 노드 간 링크: 직결선(eno1, 10.10.10.0/30)이 끊기면 노드는 Ready 여도 파드 통신이 안 된다 (11-26 EEE 설정으로 링크가 끊긴 적이 있다).

## 원인 후보

- 노드 재부팅, 전원, kubelet 정지.
- 유지보수로 drain 한 뒤 uncordon 을 잊음.
- 노드 2대 구성의 한계: 한 대가 빠지면 남은 한 대에 다 올릴 자원이 없을 수 있다.

## 조치

- 자동 조치 대상이 아니다. 사람에게 보고한다 (어느 노드, 언제부터, 영향받는 워크로드).
- drain 이었다면 `kubectl uncordon`.

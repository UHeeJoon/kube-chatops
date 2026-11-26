# 재부팅 후 Longhorn 볼륨 attach 지연

장애 시나리오 S6. 2026-11-24 실제로 겪었다 (Prometheus 가 약 15분 Init 상태).

## 증상

- 노드 재부팅 뒤 볼륨을 쓰는 파드가 `ContainerCreating` 또는 `Init` 에서 멈춘다.
- 파드 이벤트에 `FailedMount` 또는 `FailedAttachVolume`: `volume ... hasn't been attached yet`.
- Longhorn 볼륨 상태가 `degraded` 또는 `attaching`.

## 확인

- 파드 이벤트의 FailedMount 문구와 지속 시간.
- Longhorn 볼륨·복제본 상태 (`kubectl -n storage get volumes.longhorn.io`, Longhorn UI).
- instance-manager 파드가 재부팅한 노드에서 다시 떴는지.
- 노드가 Ready 가 된 뒤 얼마나 지났는지. 보통 몇 분 안에 스스로 붙는다.

## 원인

- 재부팅으로 볼륨의 엔진과 복제본이 함께 내려갔다가, 노드가 올라온 뒤 Longhorn 이 복제본을 다시 맞추는 동안 attach 가 미뤄진다.
- 복제본이 한 노드에만 있던 볼륨은 그 노드가 올라올 때까지 붙을 수 없다.

## 조치

- 대부분 기다리면 풀린다. 15분 넘게 그대로면 사람에게 보고한다.
- 자동 조치 대상이 아니다. 파드를 지워 다시 만들어도 볼륨이 준비되지 않았으면 같은 상태다.
- 재부팅 전에 워크로드를 다른 노드로 옮기거나, 복제본 수를 노드 수에 맞게 유지한다.

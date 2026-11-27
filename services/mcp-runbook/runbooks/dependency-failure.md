# 의존 서비스 연결 실패 (DNS, 네트워크 정책, DB)

앱은 살아 있는데 다른 서비스에 붙지 못해 오류나 지연이 나는 경우.

## 증상

- 로그에 `connection refused`, `timeout`, `Name or service not known`, `no such host`.
- 오류율이나 지연이 오르는데 CPU·메모리는 평소와 같다.
- 배포나 네트워크 정책 변경 직후 시작된다.

## 확인

- 로그의 연결 오류 문구와 대상 주소 (Loki `{namespace="demo"} |= "timeout"`).
- 정책 차단: `sum by (destination_namespace, destination_workload) (increase(hubble_drop_total{source_namespace="demo", reason="POLICY_DENIED"}[15m]))`. 값이 있으면 네트워크 정책이 막은 것이다.
- DNS: `hubble_drop_total` 의 목적지가 kube-dns 인지, 로그에 이름 풀이 실패가 있는지.
- 상대 서비스: 그 서비스의 파드가 Ready 인지, 엔드포인트가 비어 있지 않은지.

## 원인 후보

- 새 의존 서비스를 추가하고 네트워크 정책에 경로를 넣지 않음 (기본이 모두 차단이다).
- 상대 서비스 장애 또는 재시작 중.
- DNS 로 가는 길이 정책에 빠짐.
- DB 연결 수 한도 초과.

## 조치

- 정책이 막은 것이면 네트워크 정책에 필요한 경로를 추가한다 (배포 저장소).
- 앱 재시작은 도움이 안 된다. 침해된 파드의 횡이동 시도와 구분해야 한다: 배포 직후이고 목적지가 그 배포에서 새로 쓰는 서비스면 설정 실수, 아니면 '네트워크 흐름' 런북을 본다.

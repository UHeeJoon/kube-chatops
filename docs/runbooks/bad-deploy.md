# 잘못된 배포 (CrashLoopBackOff, ImagePullBackOff)

관련 알림: `DemoPodRestarting`, Argo CD 앱 Degraded. 장애 시나리오 S4.

## 증상

- 새 파드가 Ready 가 되지 않는다. 상태가 `CrashLoopBackOff` 또는 `ImagePullBackOff`.
- 롤링 업데이트가 멈춰 있다 (이전 파드는 아직 살아 서비스는 유지될 수 있다).

## 확인

- 파드 상태와 이벤트 (`kubectl -n <ns> describe pod`). `ImagePullBackOff` 면 이미지 주소·digest, `CrashLoopBackOff` 면 컨테이너 로그의 마지막 줄.
- rollout 이력: 언제 무엇이 바뀌었나 (`kubectl rollout history`, 배포 저장소의 최근 커밋).
- 이미지: 이 프로젝트의 자체 이미지는 CI 가 digest 로 올린다. 사람이 태그를 직접 고쳤으면 그 커밋이 원인일 가능성이 높다.
- 환경 변수·Secret: 새로 추가한 값이 없어서 시작하다 죽는 경우 (`환경 변수 X 가 없다` 같은 로그).

## 원인 후보

- 없는 이미지 주소 또는 잘못된 digest.
- 시작에 필요한 설정·Secret 누락.
- 준비 상태 검사(readiness)가 통과하지 못하는 코드 변경.

## 조치

- `rollback` (이전 revision 으로). GitOps 이므로 배포 저장소의 커밋을 되돌리는 것이 정식 경로다.
- 되돌린 뒤 원인을 고쳐 다시 배포한다.

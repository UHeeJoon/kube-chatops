# 서명되지 않았거나 digest 없는 이미지 (Kyverno 정책 위반)

Kyverno 가 테넌트 네임스페이스에 뜨는 자체 이미지의 cosign 서명과 digest 고정을 확인한다. 지금은 기록 모드(Audit)다.

## 증상

- PolicyReport 에 `verify-kube-chatops-images` 또는 `require-image-digest` 가 fail.
- 차단 모드로 바꾼 뒤라면 파드 생성이 거절된다 (이벤트에 `admission webhook ... denied`).

## 확인

- 위반 목록: `kubectl get policyreport -A` 에서 fail 인 항목과 대상 파드.
- 이미지 주소: `ghcr.io/uheejoon/kube-chatops/*` 인데 서명이 없으면 CI 를 거치지 않고 올라간 이미지다.
- 누가 바꿨나: 배포 저장소의 최근 커밋. CI 는 digest 로만 올린다. 태그(`:latest`)가 보이면 사람이 직접 고친 것이다.

## 판단

- 외부 이미지(Postgres, KServe 구성 요소)의 digest 위반은 알려진 예외다.
- 자체 이미지의 서명 실패는 심각하다. 공급망을 거치지 않은 이미지가 클러스터에 들어온 것이다.

## 조치

- 자동 조치 대상이 아니다. 그 이미지를 올린 커밋을 되돌리고, CI 로 다시 빌드해 서명된 이미지로 배포한다.
- 레지스트리 접근 토큰이 유출됐을 가능성을 사람에게 알린다.

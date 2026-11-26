# 로그인 실패 급증 (keycloak 무차별 대입)

관련 알림: `KeycloakLoginFailuresHigh`.

## 증상

- keycloak 의 로그인 실패(`LOGIN_ERROR`)가 5분에 20건을 넘는다.
- 이 알림은 에이전트 조사 없이 Slack 으로 바로 온다 (플랫폼 알림).

## 확인

- 어느 realm 인지 (알림 라벨 `realm`).
- 한 계정에 몰렸는지, 여러 계정에 흩어졌는지: keycloak 관리 콘솔의 Events 또는 keycloak 로그.
- 출발 IP: Cloudflare 를 거치므로 실제 IP 는 keycloak 이벤트의 `ipAddress`.
- 성공한 로그인이 섞여 있는지. 실패가 이어지다 성공이 나오면 계정이 뚫렸을 수 있다.

## 판단

- 한 계정에 여러 번: 비밀번호 추측. Brute Force Detection 이 계정을 잠근다 (11-27 켬).
- 여러 계정에 한두 번씩: 유출된 계정 목록을 대입하는 것(credential stuffing). 계정 잠금으로는 안 막힌다.
- 배포 직후 서비스 계정의 실패: 잘못된 클라이언트 비밀 값 설정.

## 조치

- 계정 잠금은 keycloak 이 한다. 사람은 Cloudflare WAF 에서 출발 IP 를 막을지 판단한다.
- 성공한 로그인이 있으면 그 계정의 세션을 끊고 비밀번호를 바꾼다.

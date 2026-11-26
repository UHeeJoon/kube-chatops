# 웹 공격 차단과 요청 수 제한 (WAF)

관련 알림: `DemoWafAttackBlocked`, `DemoRateLimited`, `DemoWafDown`.

## 증상

- `DemoWafAttackBlocked`: 웹 방화벽(Caddy + Coraza, OWASP CRS)이 5분에 5건 넘게 403 으로 막았다.
- `DemoRateLimited`: 한 IP 가 10초 100건을 넘겨 429 를 5분에 50건 넘게 받았다.
- `DemoWafDown`: WAF 파드가 없어 demo 가 바깥에서 닿지 않는다.

## 확인

- 누가, 무엇을: mcp-loki `waf_blocks` 도구. 출발 IP 별 건수, 처음·마지막 시각, 걸린 규칙, 예시 요청이 나온다.
  - 규칙 번호: 942xxx SQL 삽입, 941xxx XSS, 930xxx 경로 조작, 932xxx 명령 주입, 913xxx 스캐너, 944xxx Java(Log4Shell), 933xxx PHP.
- 출발 IP 는 실제 접속자다 (Cloudflare 의 `CF-Connecting-IP`). 사설 IP 면 클러스터 안에서 온 요청이다.
- 통과한 것이 있는지: 같은 IP 의 200 응답과 앱 오류를 함께 본다. 막힌 것만 있으면 피해는 없다.
- 그 뒤 단계: 같은 시간대에 커널 보안 이벤트(mcp-loki `security_events`)가 있는지. 있으면 요청 단계를 넘어간 것이다.

## 판단

- 스캐너가 한 번 지나간 것 (짧은 시간, 여러 규칙, 스캐너 User-Agent): 차단으로 끝난 것이다. 조치 불필요.
- 한 IP 가 계속 시도: Cloudflare WAF 에서 그 IP 를 막는 것을 사람에게 권한다.
- 정상 사용자가 막힘 (오탐): 걸린 규칙 번호와 요청을 적어 규칙 예외를 검토한다.

## 조치

- 자동 조치 없음. 이미 막혔다. 보고서에 출발 IP, 건수, 규칙, 통과 여부를 적는다.
- `DemoWafDown` 은 deployment/waf 의 파드 상태와 이벤트를 본다.

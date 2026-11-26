# 웹셸, 파일 변조, 실행 파일 투하, 권한 상승 (런타임 차단과 격리)

관련 알림: `DemoWebshellBlocked`, `DemoWebshellRepeated`, `DemoFileTamperBlocked`, `DemoCompromiseBlocked`.

## 증상

- Tetragon 이 커널에서 프로세스를 종료(SIGKILL)하거나 호출을 실패(EPERM)시켰다.
  - `webshell-block`: 웹 서버 프로세스가 프로그램을 실행하려 했다.
  - `webroot-tamper-block`: 웹 서버가 웹 루트나 시스템 경로에 파일을 쓰려 했다.
  - `dropper-block`: `/tmp` 같은 임시 경로나 웹 루트의 파일을 실행하려 했다.
  - `privesc-block`: `su`, `sudo` 실행 또는 setuid 비트 설정.
- `DemoCompromiseBlocked`, `DemoWebshellRepeated` 는 그 파드를 자동으로 격리한다 (라벨을 떼어 서비스에서 빼고 모든 통신 차단). 새 파드가 대신 뜬다.

## 확인

- 무엇을 하려 했나: mcp-loki `security_events` 도구. 파드별 시도 횟수, 처음·마지막 시각, 대상 파일(target), 조치(action)가 나온다.
- 어떻게 들어왔나: 같은 시간대의 `waf_blocks` 와 앱 접근 로그. WAF 를 통과한 요청이 있는지 본다.
- 다른 파드로 번졌나: 같은 워크로드의 다른 파드, 네트워크 흐름 알림(`DemoFlowPolicyDenied`, `DemoPortScan`).
- 격리됐나: 파드에 `security.kube-chatops/quarantine=true` 라벨이 있는지.

## 판단

- 실행·쓰기 시도가 있었다는 것은 요청 단계 방어를 넘어 앱 안에서 코드가 돌았다는 뜻이다. 차단됐어도 그 파드는 침해된 것으로 본다.
- 한 번이면 취약점 탐색, 반복이면 실제 공격이다.

## 조치

- 격리된 파드는 증거다. 로그와 파일을 확인한 뒤 사람이 Slack 의 [격리 파드 삭제] 버튼으로 지운다.
- 들어온 경로(취약한 엔드포인트)를 찾아 막지 않으면 새 파드도 같은 공격을 받는다. 보고서에 진입 요청을 적는다.
- 이미지를 다시 빌드해 배포해야 하는지 (취약한 의존성) 사람에게 넘긴다.

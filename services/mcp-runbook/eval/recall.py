#!/usr/bin/env python3
"""런북 검색 평가: 질문마다 "찾아야 할 런북" 을 정답으로 두고 recall@1, recall@3 을 잰다.

질문은 세 종류다. 에이전트가 실제로 넣을 만한 것(알림 이름 + 증상), 낱말이 겹치지 않게 바꿔 말한 것, 사람이 Slack 에 쓸 법한 말.
바꿔 말한 질문이 맞아야 뜻으로 찾는 것(벡터)이 일하는 것이고, 알림 이름 질문이 맞아야 낱말로 찾는 것(전문 검색)이 일하는 것이다.

사용: python3 eval/recall.py --url http://localhost:8080/mcp --token <토큰>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

CASES = [
    # (종류, 질문, 정답 런북 파일)
    ("알림", "DemoMemoryNearLimit 메모리 사용량이 한도에 가깝다", "memory-leak-oom.md"),
    ("바꿔 말함", "파드가 주기적으로 죽었다 살아나고 램이 계속 늘어난다", "memory-leak-oom.md"),
    ("사람", "demo-app 이 OOMKilled 로 재시작했는데 왜 그래?", "memory-leak-oom.md"),
    ("알림", "DemoHighLatency p95 응답 시간 증가", "high-latency.md"),
    ("바꿔 말함", "주문 조회가 평소보다 훨씬 느려졌어요", "high-latency.md"),
    ("알림", "DemoHighErrorRate 5xx 비율 상승", "high-error-rate.md"),
    ("바꿔 말함", "요청 열 번 중 세 번이 500 으로 실패한다", "high-error-rate.md"),
    ("사람", "새 버전 올렸더니 CrashLoopBackOff 가 떠", "bad-deploy.md"),
    ("바꿔 말함", "이미지를 못 받아와서 컨테이너가 안 뜬다", "bad-deploy.md"),
    ("사람", "워커 노드가 NotReady 고 파드가 Pending 이야", "node-down.md"),
    ("바꿔 말함", "서버 한 대를 껐더니 옮겨 갈 곳이 없어 대기 중인 워크로드가 있다", "node-down.md"),
    ("알림", "FailedMount volume hasn't been attached yet", "longhorn-attach-delay.md"),
    ("바꿔 말함", "재부팅하고 나서 디스크가 안 붙어 Prometheus 가 Init 에서 멈춰 있다", "longhorn-attach-delay.md"),
    ("사람", "ExternalSecret 이 SecretSyncedError 야", "vault-sealed.md"),
    ("바꿔 말함", "비밀 저장소가 잠겨서 새 파드가 암호를 못 받아 온다", "vault-sealed.md"),
    ("알림", "DemoWafAttackBlocked 웹 방화벽이 5분 동안 10건을 막음", "waf-attack.md"),
    ("바꿔 말함", "누군가 SQL 삽입과 경로 조작 요청을 계속 보낸다", "waf-attack.md"),
    ("알림", "DemoRateLimited 한 IP 가 429 를 200건 받음", "waf-attack.md"),
    ("알림", "DemoWebshellBlocked 웹 서버가 셸을 실행하려다 종료됨", "runtime-compromise.md"),
    ("알림", "DemoCompromiseBlocked 파드가 격리됨", "runtime-compromise.md"),
    ("바꿔 말함", "/tmp 에 올린 파일을 실행하려다 커널에서 죽었다", "runtime-compromise.md"),
    ("알림", "DemoPortScan 한 워크로드가 5분에 40번 정책에 막힘", "network-flow.md"),
    ("바꿔 말함", "침해된 컨테이너가 다른 네임스페이스의 DB 로 붙으려 한다", "network-flow.md"),
    ("알림", "DemoUnexpectedEgress 클러스터 밖으로 연결이 나감", "network-flow.md"),
    ("알림", "AnomalyMonitorStale 이상 탐지가 10분 동안 판정하지 못함", "anomaly-monitor.md"),
    ("바꿔 말함", "모델을 새 버전으로 바꾼 뒤 예측 서버가 500 을 낸다", "anomaly-monitor.md"),
    ("알림", "DemoSinglePodAnomaly 파드 하나만 이상으로 판정됨", "anomaly-findings.md"),
    ("알림", "DemoAnomalyUnexplained 모델이 이상이라는데 해당하는 알림이 없음", "anomaly-findings.md"),
    ("알림", "AnomalyBaselineDrift 평소 기준이 지금과 다름", "anomaly-findings.md"),
    ("바꿔 말함", "복제본 둘 중 한쪽에서만 실패 응답이 나온다", "anomaly-findings.md"),
    ("알림", "KeycloakLoginFailuresHigh 로그인 실패 5분 28건", "keycloak-login-failures.md"),
    ("바꿔 말함", "누가 관리자 계정 비밀번호를 계속 틀리게 넣고 있다", "keycloak-login-failures.md"),
    ("바꿔 말함", "오류는 없는데 들어오는 요청이 갑자기 0 이 됐다", "traffic-drop.md"),
    ("사람", "load-gen 이 죽었는지 rps 가 뚝 떨어졌어", "traffic-drop.md"),
    ("알림", "FailedScheduling Insufficient memory", "pod-pending-resources.md"),
    ("바꿔 말함", "replicas 를 늘렸는데 새로 만든 것만 안 뜬다", "pod-pending-resources.md"),
    ("사람", "로그에 connection refused 랑 timeout 이 계속 찍혀", "dependency-failure.md"),
    ("바꿔 말함", "앱은 멀쩡한데 다른 서비스 이름을 못 찾는다", "dependency-failure.md"),
    ("사람", "봇이 예산 다 썼다고 조사를 안 해", "agent-degraded.md"),
    ("바꿔 말함", "AI 가 도구 서버에 붙지 못해 근거 없이 답한다", "agent-degraded.md"),
    ("알림", "verify-kube-chatops-images PolicyReport fail", "image-policy-violation.md"),
    ("바꿔 말함", "CI 를 거치지 않은 컨테이너가 배포됐다", "image-policy-violation.md"),
    ("사람", "보고서에 지시처럼 보이는 문장이 있었다는 경고가 붙었어", "suspicious-instructions-in-data.md"),
    ("바꿔 말함", "User-Agent 에 AI 한테 거짓 보고를 시키는 글이 들어 있다", "suspicious-instructions-in-data.md"),
]


def call(url: str, token: str, tool: str, args: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):  # SSE 로 온 경우
        raw = next(line[5:] for line in raw.splitlines() if line.startswith("data:"))
    text = json.loads(raw)["result"]["content"][0]["text"]
    return json.loads(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080/mcp")
    ap.add_argument("--token", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--set", choices=["dev", "heldout"], default="dev", help="dev: 런북과 함께 만든 질문, heldout: 런북을 보지 않고 쓴 질문 (eval/heldout.py)")
    a = ap.parse_args()
    cases = CASES
    if a.set == "heldout":
        from heldout import CASES as cases  # noqa: PLC0415
    by_kind: dict[str, list[tuple[bool, bool]]] = {}
    misses = []
    for kind, query, want in cases:
        res = call(a.url, a.token, "search_runbooks", {"query": query, "k": 3})
        files = [r["source"].split("/")[-1] for r in res["results"]]
        at1, at3 = bool(files) and files[0] == want, want in files
        by_kind.setdefault(kind, []).append((at1, at3))
        if not at3 or a.verbose:
            misses.append((kind, query, want, files, [r["matched_by"] for r in res["results"]]))
    print("| 질문 종류 | 수 | recall@1 | recall@3 |\n|---|---|---|---|")
    total = [x for v in by_kind.values() for x in v]
    for kind, v in list(by_kind.items()) + [("전체", total)]:
        print(f"| {kind} | {len(v)} | {sum(x[0] for x in v) / len(v):.2f} | {sum(x[1] for x in v) / len(v):.2f} |")
    for m in misses:
        print(("놓침" if m[2] not in m[3] else "확인"), m)
    if a.set == "dev" and sum(x[1] for x in total) / len(total) < 0.9:
        sys.exit(1)


if __name__ == "__main__":
    main()

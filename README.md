# kube-chatops

Slack에서 쿠버네티스 장애를 조사하고, 사람이 승인하면 조치까지 하는 ChatOps 프로젝트입니다.

```
Slack → chat-gateway → n8n → agent (LangGraph) ─MCP→ prometheus · loki · k8s · anomaly · runbook
                              └ 사람 승인 후에만 실행: restart (Argo CD) / rollback · scale (배포 저장소 PR)
```

AI는 조사와 제안만 하고, 쓰기 권한은 승인 뒤 n8n만 가집니다.

## 디렉터리

| 경로 | 내용 | 언어 |
|---|---|---|
| `services/chat-gateway` | Slack Socket Mode 수신, n8n으로 전달 | Python |
| `services/agent` | LangGraph 에이전트 (`/route`, `/investigate`) | Python |
| `services/mcp-prometheus` | 메트릭 조회 MCP 서버 | Python |
| `services/mcp-loki` | 로그 검색 MCP 서버 | Python |
| `services/mcp-k8s` | 파드, 이벤트, 롤아웃 조회 MCP 서버 (읽기 전용) | Python |
| `services/mcp-anomaly` | 이상 탐지 모델(KServe) 호출 MCP 서버 | Python |
| `services/mcp-runbook` | 런북과 과거 장애 검색 (RAG, pgvector) | Rust |
| `demo/demo-app` | 장애 주입용 앱 (`/chaos/*`) | Python |
| `demo/load-gen` | 부하 생성기 | Python |
| `ml/training` | IsolationForest 학습, 평가 | Python |
| `ml/pipelines` | Argo Workflows 템플릿 | YAML |
| `n8n/workflows` | n8n 워크플로 export (JSON) | |
| `docs/runbooks` | 장애 대응 런북 (mcp-runbook 수집 대상) | Markdown |
| `scenarios` | 장애 시나리오와 주입 시각 기록 (모델 평가 정답) | |

## 배포

이 저장소는 코드와 이미지 빌드만 담당합니다. 쿠버네티스 매니페스트는 별도 배포 저장소에 둡니다.

```
main push → GitHub Actions: 바뀐 서비스만 빌드 → ghcr.io 푸시 → cosign 서명 → 배포 저장소에 digest bump
```

플랫폼(Argo CD, Vault, 모니터링)은 [`UHeeJoon/homelab`](https://github.com/UHeeJoon/homelab)에서 관리합니다.
비밀 값은 Vault `secret/kube-chatops/*`에 두고 External Secrets로 주입합니다. 이 저장소에는 비밀 값을 두지 않습니다.

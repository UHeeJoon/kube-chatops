"""환경 변수 설정. 비밀 값(API 키, 토큰, DB 비밀번호)은 Secret → env 로만 들어온다."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 인증: n8n 이 보내는 Bearer 토큰 (Vault secret/kube-chatops/agent api_token)
    api_token: SecretStr = Field(min_length=32)
    anthropic_api_key: SecretStr

    # 모델은 일의 종류에 따라 고른다 (app/models.py). /route 는 짧은 분류라 Haiku + effort low (Opus 대비 입력 단가 1/40).
    # model / investigate_effort: 무거운 조사 (보안 사건, 비교해 보지 않은 알림, 사람이 시킨 조사).
    model: str = "claude-opus-5-5"
    route_model: str = "claude-haiku-5-5"
    investigate_effort: str = "medium"
    # 질문(단순 조회, 정리): 도구 한두 번과 짧은 답.
    answer_model: str = "claude-sonnet-5-5"
    answer_effort: str = "low"
    # 가벼운 알림 조사: 런북의 확인 조회만으로 원인이 드러나는 알림. 11-28 에 같은 장애로 비교해 Opus medium 과 같은 원인·조치를 낸 것만 넣는다
    # (지연 $0.040 → $0.018, 오류 $0.047 → $0.019, 파드 하나 $0.066 → $0.022). 보안 알림은 공격자가 만든 글을 읽으므로 넣지 않는다.
    light_model: str = "claude-sonnet-5-5"
    light_effort: str = "low"
    light_alerts: str = "DemoHighLatency,DemoHighErrorRate,DemoSinglePodAnomaly"
    # 시스템 프롬프트 + 도구 정의(약 8.8천 토큰)의 캐시 유효 시간. "1h" 는 쓰기가 2배(5분은 1.25배)지만 한 시간 동안 읽기(0.05배)로 쓴다.
    # 알림과 질문은 대개 5분~1시간 간격으로 온다 (11-27 기록: 캐시 없이 시작한 34회 중 22회가 그 구간) → 조사마다 다시 쓰던 $0.044 가 $0.002 가 된다.
    # 한 시간 넘게 조용하다 오는 요청은 $0.026 더 든다. 되돌리려면 SYSTEM_CACHE_TTL=5m.
    system_cache_ttl: str = "1h"
    route_effort: str = "low"

    # 조사 한 번의 상한 (호출 수, 시간, 비용)
    max_llm_calls: int = 10
    max_tool_calls: int = 15
    max_seconds: int = 180
    max_request_usd: float = 0.60
    # 하루 예산 (KST 기준 날짜). 넘으면 새 요청을 429 로 거절
    daily_budget_usd: float = 5.0
    budget_timezone: str = "Asia/Seoul"
    max_concurrent: int = 2
    repeat_alert_minutes: int = 20  # 같은 알림·같은 대상을 이 시간 안에 조사했으면 다시 조사하지 않고 앞의 결과를 알려 준다 (0 이면 끔)
    # 질문 모드(단순 조회): 짧은 답, 작은 상한. 모델과 effort 는 스레드의 첫 턴에 정해 끝까지 쓴다 (바꾸면 그 스레드의 대화 캐시가 깨진다)
    answer_max_llm_calls: int = 4
    answer_max_tool_calls: int = 5

    # MCP 서버: 이름=URL, 토큰은 MCP_TOKEN_<이름 대문자>
    mcp_servers: str = (
        "prometheus=http://mcp-prometheus.mcp.svc:8080/mcp,"
        "loki=http://mcp-loki.mcp.svc:8080/mcp,"
        "k8s=http://mcp-k8s.mcp.svc:8080/mcp"
    )
    mcp_token_prometheus: SecretStr | None = None
    mcp_token_loki: SecretStr | None = None
    mcp_token_k8s: SecretStr | None = None
    mcp_token_anomaly: SecretStr | None = None
    mcp_token_runbook: SecretStr | None = None
    # 사건 보고를 런북 검색 서버(mcp-runbook)에 넣는 주소. 비어 있으면 넣지 않는다 (similar_incidents 의 원본)
    runbook_ingest_url: str = ""
    max_tool_result_chars: int = 12000

    # Postgres (aiops-pg 의 agent DB): 체크포인터와 예산 장부
    pg_host: str = "aiops-pg-rw"
    pg_port: int = 5432
    pg_database: str = "agent"
    pg_user: str = "agent"
    pg_password: SecretStr

    otel_exporter_otlp_endpoint: str | None = None  # 예: http://tempo.monitoring.svc:4317
    metrics_port: int = 9090

    def mcp_targets(self) -> dict[str, tuple[str, str]]:
        out = {}
        for item in self.mcp_servers.split(","):
            name, url = item.strip().split("=", 1)
            token = getattr(self, f"mcp_token_{name}", None)
            if token is None:
                raise ValueError(f"MCP_TOKEN_{name.upper()} 가 없습니다")
            out[name] = (url, token.get_secret_value())
        return out

    def pg_conninfo(self) -> str:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(
            host=self.pg_host, port=self.pg_port, dbname=self.pg_database, user=self.pg_user,
            password=self.pg_password.get_secret_value(), application_name="agent",
        )


settings = Settings()  # type: ignore[call-arg]

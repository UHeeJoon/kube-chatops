"""환경 변수 설정. 비밀 값(API 키, 토큰, DB 비밀번호)은 Secret → env 로만 들어온다."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 인증: n8n 이 보내는 Bearer 토큰 (Vault secret/kube-chatops/agent api_token)
    api_token: SecretStr = Field(min_length=32)
    anthropic_api_key: SecretStr

    # 모델: 조사는 Opus. /route 는 짧은 분류라 Haiku + effort low (Opus 대비 입력 단가 1/40).
    model: str = "claude-opus-5-5"
    route_model: str = "claude-haiku-5-5"
    investigate_effort: str = "medium"
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
    # 질문 모드(단순 조회): 짧은 답, 작은 상한. effort 는 조사와 같게 둔다 (effort 가 바뀌면 같은 스레드의 대화 캐시가 깨진다)
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

"""환경 변수 설정. 토큰은 전부 Secret → env 로만 들어온다."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Slack 앱 (Vault secret/kube-chatops/slack). xapp 은 Socket Mode 연결, xoxb 는 메시지 전송
    slack_app_token: SecretStr
    slack_bot_token: SecretStr

    # gateway → n8n: 받은 이벤트를 넘기는 내부 webhook 과 그 Bearer 토큰
    n8n_webhook_url: str = "http://n8n.aiops.svc:5678/webhook/chat"
    n8n_webhook_token: SecretStr = Field(min_length=32)
    # n8n → gateway: /slack/post, /audit 호출에 쓰는 Bearer 토큰
    api_token: SecretStr = Field(min_length=32)

    # 알림처럼 채널이 정해지지 않은 메시지를 보낼 기본 채널 (채널 ID)
    alert_channel: str = ""
    metrics_port: int = 9090
    forward_timeout_seconds: float = 5.0


settings = Settings()

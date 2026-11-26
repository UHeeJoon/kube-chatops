"""chat-gateway: Slack 과 n8n 사이의 얇은 연결층.

- 받기: Slack Socket Mode(웹소켓, 공개 엔드포인트 없음)로 멘션, DM, 버튼 클릭을 받아 바로 ack 하고
  공통 이벤트 JSON 으로 n8n 내부 webhook 에 넘긴다. 판단(권한, 의도 분류)은 n8n 과 agent 가 한다.
- 보내기: n8n 이 `/slack/post`, `/slack/update` 를 부르면 Slack 에 메시지를 쓴다.
  Slack 토큰은 이 서비스에만 있다 (n8n 에는 Slack 자격 증명이 없다).
- 감사: n8n 이 흐름이 끝날 때 `/audit` 에 결과를 보내면 JSON 한 줄로 남긴다 → Alloy → Loki.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter, start_http_server
from pydantic import BaseModel, Field
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        out.update(getattr(record, "fields", {}))
        return json.dumps(out, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("gateway")
audit_log = logging.getLogger("audit")

EVENTS = Counter("gateway_slack_events_total", "Slack 에서 받은 이벤트", ["type"])
FORWARD_FAILURES = Counter("gateway_forward_failures_total", "n8n 전달 실패")
POSTS = Counter("gateway_slack_posts_total", "Slack 에 쓴 메시지", ["method", "status"])
AUDITS = Counter("gateway_audit_events_total", "감사 로그 수", ["flow", "result"])

bolt = AsyncApp(token=settings.slack_bot_token.get_secret_value())
state: dict[str, Any] = {"bot_user_id": None, "session": None, "socket": None}

# Slack 은 ack 가 늦으면 같은 이벤트를 다시 보낸다. 최근 이벤트 키를 기억해 두 번 처리하지 않는다.
_seen: OrderedDict[str, float] = OrderedDict()


def _first_time(key: str) -> bool:
    if key in _seen:
        return False
    _seen[key] = time.time()
    while len(_seen) > 500:
        _seen.popitem(last=False)
    return True


def _clean_text(text: str) -> str:
    """멘션(<@U...>)을 지우고 공백을 정리한다."""
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


async def _forward(payload: dict[str, Any]) -> bool:
    headers = {"Authorization": f"Bearer {settings.n8n_webhook_token.get_secret_value()}"}
    try:
        async with state["session"].post(
            settings.n8n_webhook_url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=settings.forward_timeout_seconds),
        ) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"n8n 응답 {resp.status}")
        log.info("forwarded", extra={"fields": {k: payload.get(k) for k in ("type", "user_id", "channel_id", "thread_id")}})
        return True
    except Exception as e:  # noqa: BLE001 — 실패하면 사용자에게 알리고 계속 동작
        FORWARD_FAILURES.inc()
        log.error("forward failed", extra={"fields": {"error": f"{type(e).__name__}: {e}"}})
        return False


async def _handle_message(event: dict[str, Any], client, kind: str) -> None:
    if event.get("bot_id") or event.get("subtype"):
        return
    if not _first_time(f"{event.get('channel')}:{event.get('ts')}"):
        return
    EVENTS.labels(kind).inc()
    thread_ts = event.get("thread_ts") or event["ts"]
    payload = {
        "platform": "slack",
        "type": "message",
        "source": kind,  # mention | dm
        "user_id": event.get("user"),
        "channel_id": event["channel"],
        "thread_id": thread_ts,
        "message_ts": event["ts"],
        "text": _clean_text(event.get("text", "")),
    }
    try:  # 받았다는 표시. 권한(reactions:write)이 없어도 흐름은 계속
        await client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="eyes")
    except Exception:  # noqa: BLE001
        pass
    if not await _forward(payload):
        await client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts,
                                      text="지금은 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.")


@bolt.event("app_mention")
async def on_mention(event, client):
    await _handle_message(event, client, "mention")


@bolt.event("message")
async def on_message(event, client):
    # 채널 메시지는 app_mention 으로 따로 오므로 DM 만 처리한다
    if event.get("channel_type") == "im":
        await _handle_message(event, client, "dm")


@bolt.action(re.compile(".*"))
async def on_action(ack, body, client):
    await ack()
    action = (body.get("actions") or [{}])[0]
    msg = body.get("message") or {}
    channel = (body.get("channel") or {}).get("id")
    EVENTS.labels("button_click").inc()
    payload = {
        "platform": "slack",
        "type": "button_click",
        "user_id": (body.get("user") or {}).get("id"),
        "channel_id": channel,
        "thread_id": msg.get("thread_ts") or msg.get("ts"),
        "message_ts": msg.get("ts"),
        # 버튼이 달린 글의 내용. n8n 이 버튼을 누른 뒤 그 글을 고칠 때(승격 진행·완료 표시) 원래 블록이 필요하다
        "message": {"text": msg.get("text"), "blocks": msg.get("blocks")},
        "action": {"id": action.get("action_id"), "value": action.get("value")},
    }
    if not await _forward(payload) and channel:
        await client.chat_postEphemeral(channel=channel, user=payload["user_id"], text="버튼 처리에 실패했습니다.")


# ---- n8n 이 부르는 내부 API ----

def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.api_token.get_secret_value()}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


class PostIn(BaseModel):
    channel: str | None = None  # 없으면 ALERT_CHANNEL
    thread_ts: str | None = None
    text: str = Field(max_length=4000)  # 알림, 접근성용 대체 텍스트
    blocks: list[dict[str, Any]] | None = None


class UpdateIn(BaseModel):
    channel: str
    ts: str
    text: str = Field(max_length=4000)
    blocks: list[dict[str, Any]] | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_http_server(settings.metrics_port)
    state["session"] = aiohttp.ClientSession()
    auth = await bolt.client.auth_test()
    state["bot_user_id"] = auth["user_id"]
    log.info("slack auth ok", extra={"fields": {"team": auth.get("team"), "bot_user_id": auth["user_id"]}})
    handler = AsyncSocketModeHandler(bolt, settings.slack_app_token.get_secret_value())
    await handler.connect_async()
    state["socket"] = handler
    log.info("socket mode connected")
    yield
    await handler.close_async()
    await state["session"].close()


api = FastAPI(title="chat-gateway", docs_url=None, redoc_url=None, lifespan=lifespan)
app = api  # uvicorn app.main:app


@api.get("/healthz")
async def healthz():
    return {"status": "ok"}


@api.get("/readyz")
async def readyz():
    handler = state.get("socket")
    connected = bool(handler) and await handler.client.is_connected()
    return JSONResponse({"socket": connected}, status_code=200 if connected else 503)


@api.post("/slack/post", dependencies=[Depends(require_token)])
async def slack_post(body: PostIn):
    channel = body.channel or settings.alert_channel
    if not channel:
        raise HTTPException(status_code=400, detail="channel 이 없고 ALERT_CHANNEL 도 설정되지 않았습니다")
    try:
        resp = await bolt.client.chat_postMessage(channel=channel, thread_ts=body.thread_ts, text=body.text,
                                                  blocks=body.blocks, unfurl_links=False, unfurl_media=False)
    except Exception as e:  # noqa: BLE001
        POSTS.labels("post", "error").inc()
        raise HTTPException(status_code=502, detail=f"slack: {e}") from e
    POSTS.labels("post", "ok").inc()
    return {"ok": True, "channel": resp["channel"], "ts": resp["ts"]}


@api.post("/slack/update", dependencies=[Depends(require_token)])
async def slack_update(body: UpdateIn):
    try:
        resp = await bolt.client.chat_update(channel=body.channel, ts=body.ts, text=body.text, blocks=body.blocks)
    except Exception as e:  # noqa: BLE001
        POSTS.labels("update", "error").inc()
        raise HTTPException(status_code=502, detail=f"slack: {e}") from e
    POSTS.labels("update", "ok").inc()
    return {"ok": True, "channel": resp["channel"], "ts": resp["ts"]}


@api.post("/audit", dependencies=[Depends(require_token)])
async def audit(record: dict[str, Any]):
    flow = str(record.get("flow", "unknown"))[:40]
    result = str(record.get("result", "unknown"))[:40]
    AUDITS.labels(flow, result).inc()
    audit_log.info("audit", extra={"fields": {"audit": True, **record}})
    return {"ok": True}


if __name__ == "__main__":  # 로컬 실행용
    import uvicorn

    asyncio.run(uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=8080)).serve())

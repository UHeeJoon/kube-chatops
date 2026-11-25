"""agent — LangGraph 조사 에이전트 HTTP API (n8n 이 호출).

POST /route        {text}                               → {intent, target, reply, usage}
POST /investigate  {thread_id, question, alert?}        → {thread_id, report, usage, stopped_by}
GET  /healthz      인증 없음
메트릭은 별도 포트 :9090/metrics (prometheus_client).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from .telemetry import setup_tracing

_provider = setup_tracing()  # graph/toolbox 가 tracer 를 잡기 전에 전역 프로바이더를 등록

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402
from prometheus_client import Counter, Histogram, start_http_server  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from . import graph as g  # noqa: E402
from .budget import Ledger, Usage  # noqa: E402
from .config import settings  # noqa: E402
from .toolbox import Toolbox  # noqa: E402

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("agent")

REQUESTS = Counter("agent_requests_total", "API 요청 수", ["endpoint", "status"])
DURATION = Histogram("agent_request_duration_seconds", "API 처리 시간", ["endpoint"], buckets=(1, 2.5, 5, 10, 20, 40, 60, 120, 180, 300))
STOPS = Counter("agent_investigation_stops_total", "조사 종료 이유", ["reason"])


class InvestigateIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200, description="Slack thread_ts 등. 같은 값이면 대화가 이어진다")
    question: str = Field(min_length=1, max_length=4000)
    alert: dict[str, Any] | None = None


class RouteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        settings.pg_conninfo(), min_size=1, max_size=5, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=30)
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    ledger = Ledger(pool)
    await ledger.setup()
    app.state.pool, app.state.ledger = pool, ledger
    app.state.graph = g.build(saver)
    app.state.sem = asyncio.Semaphore(settings.max_concurrent)
    start_http_server(settings.metrics_port)
    log.info("agent ready")
    yield
    await pool.close()
    if _provider:
        _provider.shutdown()


app = FastAPI(title="kube-chatops agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
EXPECTED = f"Bearer {settings.api_token.get_secret_value()}".encode()


def require_token(request: Request) -> None:
    got = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(got, EXPECTED):
        REQUESTS.labels(request.url.path.strip("/"), "401").inc()
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


async def check_budget(request: Request) -> None:
    spent = await request.app.state.ledger.spent_today()
    if spent >= settings.daily_budget_usd:
        REQUESTS.labels(request.url.path.strip("/"), "429").inc()
        raise HTTPException(429, f"오늘 LLM 예산(${settings.daily_budget_usd:.2f})을 다 썼습니다. 사용: ${spent:.2f}")


def _usage(u: Usage) -> dict[str, Any]:
    return {"cost_usd": round(u.cost_usd, 4), "llm_calls": u.llm_calls, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens}


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/route", dependencies=[Depends(require_token), Depends(check_budget)])
async def route(body: RouteIn, request: Request) -> dict:
    usage = Usage()
    with DURATION.labels("route").time():
        result = await g.route(body.text, request.app.state.ledger, usage)
    REQUESTS.labels("route", "200").inc()
    return {**result, "usage": _usage(usage)}


@app.post("/investigate", dependencies=[Depends(require_token), Depends(check_budget)])
async def investigate(body: InvestigateIn, request: Request) -> dict:
    usage = Usage()
    state = request.app.state
    async with state.sem:
        with DURATION.labels("investigate").time():
            async with Toolbox() as toolbox:
                ctx = g.Ctx(toolbox=toolbox, ledger=state.ledger, usage=usage)
                # 실행마다 상한 카운터와 결과를 초기화한다 (messages 는 체크포인트에서 이어 붙는다)
                inp = {"question": body.question, "alert": body.alert, "started": time.time(), "llm_calls": 0, "tool_calls": 0, "cost_usd": 0.0, "stopped_by": None, "report": None}
                out = await state.graph.ainvoke(inp, config={"configurable": {"thread_id": body.thread_id}, "recursion_limit": 60}, context=ctx)
    stopped = out.get("stopped_by") or "done"
    STOPS.labels(stopped).inc()
    REQUESTS.labels("investigate", "200").inc()
    log.info("investigate thread=%s stopped_by=%s tools=%s cost=%.4f", body.thread_id, stopped, out.get("tool_calls"), usage.cost_usd)
    return {"thread_id": body.thread_id, "report": out.get("report"), "stopped_by": stopped, "tool_calls": out.get("tool_calls", 0), "usage": _usage(usage)}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    REQUESTS.labels(request.url.path.strip("/"), "500").inc()
    return JSONResponse({"error": type(exc).__name__}, status_code=500)

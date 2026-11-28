"""agent — LangGraph 조사 에이전트 HTTP API (n8n 이 호출).

POST /route        {text}                               → {intent, target, reply, usage}
POST /investigate  {thread_id, question, alert?}        → {thread_id, report, usage, stopped_by}
GET  /incidents    ?limit=                              → 최근 사건 목록 (조사 보고서를 사건 단위로 묶은 기록)
GET  /incidents/{id}                                    → 사건 하나 (보고 전부 + 마크다운)
GET  /healthz      인증 없음
메트릭은 별도 포트 :9090/metrics (prometheus_client).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import weakref
from contextlib import asynccontextmanager
from typing import Any, Literal

from .telemetry import setup_tracing

_provider = setup_tracing()  # graph/toolbox 가 tracer 를 잡기 전에 전역 프로바이더를 등록

import httpx2  # noqa: E402
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
from .incidents import Incidents, search_document, snapshot, to_markdown, tool_trace  # noqa: E402
from .toolbox import Toolbox  # noqa: E402

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("agent")

REQUESTS = Counter("agent_requests_total", "API 요청 수", ["api", "status"])
DURATION = Histogram("agent_request_duration_seconds", "API 처리 시간", ["api"], buckets=(1, 2.5, 5, 10, 20, 40, 60, 120, 180, 300))
STOPS = Counter("agent_investigation_stops_total", "조사 종료 이유", ["reason"])
for _api in ("route", "investigate"):  # 0 으로 미리 생성 (budget.py 주석 참고)
    DURATION.labels(_api)
    for _status in ("200", "401", "429", "500"):
        REQUESTS.labels(_api, _status)
for _reason in ("done", "llm_calls", "tool_calls", "time", "cost", "refusal"):
    STOPS.labels(_reason)


class InvestigateIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200, description="Slack thread_ts 등. 같은 값이면 대화가 이어진다")
    question: str = Field(min_length=1, max_length=4000)
    alert: dict[str, Any] | None = None
    mode: Literal["investigate", "answer"] = Field("investigate", description="answer: 단순 조회. 짧은 답, 보고서 없음")


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
    incidents = Incidents(pool)
    await incidents.setup()
    app.state.pool, app.state.ledger, app.state.incidents = pool, ledger, incidents
    app.state.graph = g.build(saver)
    app.state.sem = asyncio.Semaphore(settings.max_concurrent)
    app.state.thread_locks = weakref.WeakValueDictionary()  # thread_id → Lock. 쓰는 요청이 없으면 사라진다
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
    # 같은 스레드의 조사는 한 번에 하나만. 조사 중인 스레드에 질문이 오면 진행 중 대화(도구 호출로 끝난 상태) 위에
    # 새 메시지가 붙어 API 가 거절하거나 대화가 섞인다. 뒤에 온 요청은 앞의 것이 끝난 뒤 그 결과를 이어받아 시작한다.
    lock = state.thread_locks.get(body.thread_id)
    if lock is None:
        lock = state.thread_locks[body.thread_id] = asyncio.Lock()
    async with lock, state.sem:
        # 기다리는 동안 다른 요청이 예산을 다 썼을 수 있다. 실행 직전에 다시 본다 (들어올 때의 검사는 대기열 앞에서 한 것)
        await check_budget(request)
        with DURATION.labels("investigate").time():
            async with Toolbox() as toolbox:
                ctx = g.Ctx(toolbox=toolbox, ledger=state.ledger, usage=usage)
                # 실행마다 상한 카운터와 결과를 초기화한다 (messages 는 체크포인트에서 이어 붙는다)
                inp = {"question": body.question, "mode": body.mode, "answer": None, "alert": body.alert, "started": time.time(), "llm_calls": 0, "tool_calls": 0, "cost_usd": 0.0, "stopped_by": None, "report": None, "finishing": False}
                out = await state.graph.ainvoke(inp, config={"configurable": {"thread_id": body.thread_id}, "recursion_limit": 60}, context=ctx)
                stopped = out.get("stopped_by") or "done"
                # 이번 요청만이 아니라 이 스레드가 지금까지 읽은 것 전부 (대화에 남아 있으므로)
                suspicious = sorted(set(out.get("tainted") or []) | set(toolbox.suspicious))
                # 사건 기록: 조사(보고서가 나온 것)만. 저장이 실패해도 보고는 그대로 돌려준다.
                incident = None
                if body.mode == "investigate" and (out.get("report") or body.alert):
                    try:
                        incident = await state.incidents.record(
                            thread_id=body.thread_id, question=body.question, alert=body.alert, report=out.get("report"),
                            trace=tool_trace(out.get("messages") or [], out.get("tool_calls", 0) + 1),
                            timeline=(timeline := await snapshot(toolbox, body.alert)), stopped_by=stopped,
                            tool_calls=out.get("tool_calls", 0), cost_usd=usage.cost_usd)
                        await _index_incident(incident, body, out.get("report"), timeline, suspicious=len(toolbox.suspicious))
                    except Exception:  # noqa: BLE001
                        log.exception("사건 기록 실패 thread=%s", body.thread_id)
    STOPS.labels(stopped).inc()
    REQUESTS.labels("investigate", "200").inc()
    log.info("investigate thread=%s mode=%s model=%s/%s stopped_by=%s tools=%s cost=%.4f", body.thread_id, body.mode, out.get("model"), out.get("effort"), stopped, out.get("tool_calls"), usage.cost_usd)
    return {"thread_id": body.thread_id, "mode": body.mode, "answer": out.get("answer"), "report": out.get("report"), "stopped_by": stopped, "tool_calls": out.get("tool_calls", 0), "usage": _usage(usage), "model": out.get("model"), "effort": out.get("effort"),
            "incident": incident, "injection_suspected": suspicious}


async def _index_incident(incident: dict[str, Any], body: InvestigateIn, report: dict[str, Any] | None, timeline: dict[str, Any], suspicious: int = 0) -> None:
    """사건 보고를 런북 검색 서버에 넣는다 (다음 조사의 similar_incidents 가 찾을 수 있게). 실패해도 조사 결과에는 영향이 없다."""
    token = settings.mcp_token_runbook
    if not settings.runbook_ingest_url or token is None:
        return
    labels = ((body.alert or {}).get("alerts") or [{}])[0].get("labels") or {}
    doc = search_document(incident["incident_id"], incident["report_no"], labels.get("alertname"), labels.get("namespace") or "-", report, body.question, timeline,
                          pod=labels.get("pod"), suspicious=suspicious)
    try:
        async with httpx2.AsyncClient(timeout=20) as client:
            r = await client.post(settings.runbook_ingest_url, json=doc, headers={"Authorization": f"Bearer {token.get_secret_value()}"})
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("사건 색인 실패 incident=%s: %s: %s", incident["incident_id"], type(e).__name__, e)


@app.get("/incidents", dependencies=[Depends(require_token)])
async def incidents_list(request: Request, limit: int = 10) -> dict:
    return {"incidents": await request.app.state.incidents.list(max(1, min(limit, 50)))}


@app.get("/incidents/{incident_id}", dependencies=[Depends(require_token)])
async def incidents_get(incident_id: int, request: Request) -> dict:
    inc = await request.app.state.incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, f"사건 #{incident_id} 이 없습니다")
    return {"incident": inc, "markdown": to_markdown(inc)}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    REQUESTS.labels(request.url.path.strip("/"), "500").inc()
    return JSONResponse({"error": type(exc).__name__}, status_code=500)

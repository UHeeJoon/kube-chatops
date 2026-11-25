"""장애 주입용 데모 API.

정상 엔드포인트(/api/orders)와 장애 주입 엔드포인트(/chaos/*)를 가진다.
장애 시나리오 S1~S3(메모리 누수, 지연, 에러율)을 재현하고, 그 근거가 메트릭과 로그에 남게 한다.
"""

import json
import logging
import os
import random
import sys
import time
import traceback

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = os.getenv("SERVICE_NAME", "demo-app")
CHAOS_TOKEN = os.getenv("CHAOS_TOKEN", "")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "service": SERVICE, "msg": record.getMessage()}
        data.update(getattr(record, "extra", {}))
        if record.exc_info:
            data["stack"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(data, ensure_ascii=False)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.addHandler(handler)
log.setLevel(logging.INFO)

REQUESTS = Counter("http_requests_total", "HTTP 요청 수", ["method", "path", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP 요청 처리 시간", ["method", "path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CHAOS = Gauge("demo_chaos_active", "현재 켜진 장애 주입 (1=켜짐)", ["kind"])
LEAKED = Gauge("demo_leaked_bytes", "누수 시뮬레이션으로 잡고 있는 메모리")

state = {"latency_ms": 0, "error_rate": 0.0, "leak_mb_per_req": 0}
leak: list[bytes] = []

app = FastAPI(title=SERVICE, docs_url=None, redoc_url=None)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # 라우트 템플릿(/api/orders/{order_id})으로 기록해 라벨 수가 늘지 않게 한다. 라우팅 후에야 scope 에 생긴다.
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        if path not in ("/metrics", "/healthz", "/readyz"):
            LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
            REQUESTS.labels(request.method, path, str(status)).inc()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/orders/{order_id}")
def get_order(order_id: int):
    if state["latency_ms"]:
        time.sleep(random.uniform(0.5, 1.5) * state["latency_ms"] / 1000)
    if state["leak_mb_per_req"]:
        leak.append(b"x" * state["leak_mb_per_req"] * 1024 * 1024)
        LEAKED.set(sum(len(b) for b in leak))
    if random.random() < state["error_rate"]:
        try:
            raise RuntimeError(f"주문 저장소 응답 없음 (order_id={order_id})")
        except RuntimeError:
            log.exception("주문 조회 실패", extra={"extra": {"order_id": order_id, "path": "/api/orders"}})
            raise HTTPException(status_code=503, detail="upstream unavailable")
    return {"order_id": order_id, "status": "shipped", "amount": round(random.uniform(10, 500), 2)}


def _check_token(request: Request):
    if CHAOS_TOKEN and request.headers.get("x-chaos-token") != CHAOS_TOKEN:
        raise HTTPException(status_code=401, detail="invalid chaos token")


def _set(kind: str, key: str, value, request: Request):
    _check_token(request)
    state[key] = value
    CHAOS.labels(kind).set(1 if value else 0)
    log.warning("장애 주입 변경", extra={"extra": {"chaos": kind, "value": value}})
    return {"chaos": kind, "value": value, "state": state}


@app.post("/chaos/latency")
def chaos_latency(request: Request, ms: int = 0):
    return _set("latency", "latency_ms", max(0, min(ms, 10000)), request)


@app.post("/chaos/error")
def chaos_error(request: Request, rate: float = 0.0):
    return _set("error", "error_rate", max(0.0, min(rate, 1.0)), request)


@app.post("/chaos/leak")
def chaos_leak(request: Request, mb: int = 0):
    return _set("leak", "leak_mb_per_req", max(0, min(mb, 10)), request)


@app.post("/chaos/reset")
def chaos_reset(request: Request):
    _check_token(request)
    state.update(latency_ms=0, error_rate=0.0, leak_mb_per_req=0)
    leak.clear()
    LEAKED.set(0)
    for kind in ("latency", "error", "leak"):
        CHAOS.labels(kind).set(0)
    log.warning("장애 주입 해제", extra={"extra": {"chaos": "reset"}})
    return {"state": state}


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("처리되지 않은 예외", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})

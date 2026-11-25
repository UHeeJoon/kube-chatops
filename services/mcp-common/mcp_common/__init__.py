"""MCP 서버 공통 실행 코드.

각 MCP 서버 이미지는 빌드할 때 이 패키지를 함께 복사한다 (Dockerfile 의 `COPY --from=mcp-common`).
여기서 처리하는 것:
- Bearer 토큰 인증 (토큰이 없으면 시작하지 않는다)
- Host 헤더 허용 목록 (MCP SDK 의 DNS rebinding 방지 기능에 클러스터 DNS 이름을 등록)
- 도구 호출 메트릭과 JSON 감사 로그
- 도구 결과 길이 제한
"""

from __future__ import annotations

import functools
import hmac
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import Counter, Histogram, start_http_server
from starlette.requests import Request
from starlette.responses import JSONResponse

P = ParamSpec("P")

TOOL_CALLS = Counter("mcp_tool_calls_total", "MCP 도구 호출 수", ["tool", "status"])
TOOL_SECONDS = Histogram(
    "mcp_tool_duration_seconds", "MCP 도구 실행 시간", ["tool"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
)
AUTH_FAILURES = Counter("mcp_auth_failures_total", "Bearer 토큰 인증 실패 수")

MAX_RESULT_CHARS = int(os.environ.get("MCP_MAX_RESULT_CHARS", "12000"))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        out.update(getattr(record, "fields", {}))
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    return logging.getLogger("mcp")


log = setup_logging()


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """도구 결과가 너무 길면 자르고 잘랐다는 표시를 남긴다 (모델 컨텍스트와 비용 보호)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[{len(text) - limit}자 생략. 범위를 좁혀 다시 조회하세요]"


def to_json(data: Any) -> str:
    return clip(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str))


def instrumented(fn: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
    """도구 함수에 메트릭과 감사 로그를 붙인다. 예외는 모델이 읽을 수 있는 오류 문자열로 바꾼다."""
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        start = time.perf_counter()
        status = "ok"
        try:
            return await fn(*args, **kwargs)
        except ToolInputError as e:
            status = "invalid"
            return f"입력 오류: {e}"
        except Exception as e:  # noqa: BLE001 — 백엔드 오류를 모델에 전달하고 서버는 계속 동작
            status = "error"
            log.exception("tool failed", extra={"fields": {"tool": name}})
            return f"도구 실행 실패 ({type(e).__name__}): {e}"
        finally:
            elapsed = time.perf_counter() - start
            TOOL_CALLS.labels(name, status).inc()
            TOOL_SECONDS.labels(name).observe(elapsed)
            log.info("tool call", extra={"fields": {"tool": name, "status": status, "seconds": round(elapsed, 3), "args": kwargs}})

    return wrapper


class ToolInputError(ValueError):
    """모델이 허용 범위를 벗어난 값을 넣었을 때. 오류 내용을 그대로 모델에게 돌려준다."""


def allowed_namespaces() -> list[str]:
    return [n.strip() for n in os.environ.get("ALLOWED_NAMESPACES", "demo").split(",") if n.strip()]


def check_namespace(namespace: str) -> str:
    allowed = allowed_namespaces()
    if namespace not in allowed:
        raise ToolInputError(f"namespace '{namespace}' 는 조회할 수 없습니다. 허용: {', '.join(allowed)}")
    return namespace


class BearerAuth:
    """ASGI 미들웨어. /healthz 외 모든 HTTP 요청에 `Authorization: Bearer <token>` 을 요구한다."""

    def __init__(self, app: Any, token: str, open_paths: tuple[str, ...] = ("/healthz",)) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.open_paths = open_paths

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] in self.open_paths:
            return await self.app(scope, receive, send)
        got = dict(scope["headers"]).get(b"authorization", b"")
        if hmac.compare_digest(got, self.expected):
            return await self.app(scope, receive, send)
        AUTH_FAILURES.inc()
        await JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)


def serve(server: MCPServer, service_name: str) -> None:
    """Streamable HTTP(:8080/mcp) 와 메트릭(:9090/metrics) 을 띄운다."""
    token = os.environ.get("MCP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_TOKEN 이 없거나 너무 짧습니다 (32자 이상). 인증 없이 시작하지 않습니다.")

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    namespace = os.environ.get("POD_NAMESPACE", "mcp")
    hosts = [service_name, f"{service_name}.{namespace}", f"{service_name}.{namespace}.svc", f"{service_name}.{namespace}.svc.cluster.local", "localhost", "127.0.0.1"]
    security = TransportSecuritySettings(allowed_hosts=[h for host in hosts for h in (host, f"{host}:*")])

    # stateless + JSON 응답: 서버가 세션을 들고 있지 않아 재시작이나 복제본 증가에 영향이 없다
    app = server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)

    start_http_server(int(os.environ.get("METRICS_PORT", "9090")))
    uvicorn.run(BearerAuth(app, token), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")

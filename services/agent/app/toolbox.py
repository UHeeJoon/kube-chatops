"""MCP 서버의 도구를 Claude 도구 정의로 바꾸고, 호출을 해당 서버로 보낸다.

- 도구 이름은 `<서버>__<도구>` (예: prometheus__query_instant). 서버 간 이름 충돌을 막고 메트릭 라벨로도 쓴다.
- 조사 요청 하나마다 서버별 MCP 세션을 열고 닫는다 (MCP 서버는 stateless 라 연결 비용이 작다).
- 도구 정의는 이름순으로 정렬한다 → 요청마다 같은 바이트 → 프롬프트 캐시가 맞는다.
- 도구 결과는 <tool_output> 으로 감싸 '데이터'임을 표시하고 길이를 자른다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import trace
from prometheus_client import Counter, Histogram

from .config import settings

TOOL_CALLS = Counter("agent_tool_calls_total", "에이전트가 부른 MCP 도구 수", ["server", "tool", "status"])
MCP_UNAVAILABLE = Counter("agent_mcp_unavailable_total", "연결하지 못해 요청에서 뺀 MCP 서버 수", ["server"])
TOOL_SECONDS = Histogram("agent_tool_duration_seconds", "MCP 도구 호출 시간", ["server"], buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
tracer = trace.get_tracer("agent.toolbox")
log = logging.getLogger("agent.toolbox")


class Toolbox:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._clients: dict[str, Client] = {}
        self.tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> Toolbox:
        await self._stack.__aenter__()
        for name, (url, token) in settings.mcp_targets().items():
            # 서버 하나가 죽어도 나머지 도구로 조사한다 (그 서버의 도구만 빠진다)
            try:
                http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0, connect=5.0))
                client = await self._stack.enter_async_context(Client(streamable_http_client(url, http_client=http)))
                listed = await client.list_tools()
            except Exception as e:  # noqa: BLE001 — 연결·인증·프로토콜 오류 모두 같은 처리
                log.warning("MCP 서버 연결 실패, 이번 요청에서 제외: %s (%s: %s)", name, type(e).__name__, e)
                MCP_UNAVAILABLE.labels(name).inc()
                continue
            self._clients[name] = client
            for t in listed.tools:
                self.tools.append({"name": f"{name}__{t.name}", "description": t.description or "", "input_schema": t.input_schema})
        self.tools.sort(key=lambda t: t["name"])
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.__aexit__(*exc)

    async def call(self, full_name: str, args: dict[str, Any]) -> tuple[str, bool]:
        server, _, tool = full_name.partition("__")
        client = self._clients.get(server)
        if client is None:
            return f"알 수 없는 도구: {full_name}", True
        start = time.perf_counter()
        status = "ok"
        with tracer.start_as_current_span(f"mcp {full_name}", attributes={"mcp.server": server, "mcp.tool": tool}):
            try:
                result = await asyncio.wait_for(client.call_tool(tool, args), timeout=40)
                text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
                is_error = bool(result.is_error) or text.startswith(("입력 오류", "도구 실행 실패"))
                status = "error" if is_error else "ok"
            except Exception as e:  # noqa: BLE001 — 도구 실패는 모델에게 오류 결과로 알린다
                text, is_error, status = f"도구 호출 실패: {type(e).__name__}: {e}", True, "error"
        TOOL_CALLS.labels(server, tool, status).inc()
        TOOL_SECONDS.labels(server).observe(time.perf_counter() - start)
        limit = settings.max_tool_result_chars
        if len(text) > limit:
            text = text[:limit] + "\n...[잘림]"
        return f"<tool_output>\n{text}\n</tool_output>", is_error

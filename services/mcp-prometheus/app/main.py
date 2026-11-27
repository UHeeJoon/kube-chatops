"""mcp-prometheus — Prometheus 를 읽기 전용으로 조회하는 MCP 서버.

도구: query_instant, query_range, list_firing_alerts
제약: 조회 기간(최대 MCP_MAX_RANGE_MINUTES 분), 시계열 수, 점 개수, 결과 길이를 제한한다.
범위: 지표 선택마다 허용 네임스페이스 조건이 있어야 한다 (scope.py). 알림 목록도 허용 네임스페이스 것만.
"""

from __future__ import annotations

import math
import os
import time

import httpx2
from mcp.server.mcpserver import MCPServer

from mcp_common import ToolInputError, allowed_namespaces, instrumented, serve, to_json

from .scope import check_query

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://kube-prometheus-kube-prome-prometheus.monitoring.svc:9090")
MAX_RANGE_MINUTES = int(os.environ.get("MCP_MAX_RANGE_MINUTES", "360"))
MAX_SERIES = int(os.environ.get("MCP_MAX_SERIES", "20"))
MAX_POINTS = int(os.environ.get("MCP_MAX_POINTS", "60"))
# 알림 목록에서 허용 네임스페이스 외에 더 보여 줄 곳 (이상 탐지 서빙 등 이 프로젝트의 것)
ALERT_NAMESPACES = {n for n in os.environ.get("ALERT_NAMESPACES", "mlops").split(",") if n}

http = httpx2.AsyncClient(base_url=PROM_URL, timeout=20.0)

server = MCPServer(
    name="mcp-prometheus",
    instructions="Prometheus 메트릭 조회 (읽기 전용). PromQL 로 지표를 확인하고, 발생 중인 알림을 본다.",
)


async def _api(path: str, params: dict) -> dict:
    resp = await http.get(f"/api/v1/{path}", params=params)
    body = resp.json()
    if body.get("status") != "success":
        raise ToolInputError(f"Prometheus 오류: {body.get('errorType')}: {body.get('error')}")
    return body["data"]


def _num(v: str) -> float | None:
    f = float(v)
    return None if math.isnan(f) or math.isinf(f) else round(f, 6)


@server.tool()
@instrumented
async def query_instant(query: str) -> str:
    """PromQL 을 현재 시점으로 조회한다.

    예: sum by (path, status) (rate(http_requests_total{namespace="demo"}[5m]))
    결과는 최대 20개 시계열의 라벨과 값이다. 많으면 sum by / topk 로 줄여서 다시 조회한다.
    지표 선택마다 네임스페이스 조건(namespace, source_namespace, destination_namespace 를 `=` 로)이 있어야 한다. 노드 지표(node_, kube_node_)와 anomaly_, demo_ 는 예외.
    """
    check_query(query)
    data = await _api("query", {"query": query, "timeout": "15s"})
    rtype, result = data["resultType"], data["result"]
    if rtype in ("scalar", "string"):
        return to_json({"type": rtype, "value": result[1]})
    series = [{"labels": r["metric"], "value": _num(r["value"][1])} for r in result[:MAX_SERIES]]
    return to_json({"type": rtype, "total_series": len(result), "shown": len(series), "series": series})


@server.tool()
@instrumented
async def query_range(query: str, minutes: int = 60) -> str:
    """PromQL 을 최근 `minutes` 분(최대 360) 동안 조회한다. 추세, 변화 시점 확인용.

    각 시계열마다 최소, 최대, 평균, 마지막 값과 일정 간격으로 줄인 값 목록(최대 60개)을 돌려준다.
    """
    if not 1 <= minutes <= MAX_RANGE_MINUTES:
        raise ToolInputError(f"minutes 는 1~{MAX_RANGE_MINUTES} 사이여야 합니다")
    check_query(query)
    end = time.time()
    start = end - minutes * 60
    step = max(15, math.ceil(minutes * 60 / MAX_POINTS))
    data = await _api("query_range", {"query": query, "start": start, "end": end, "step": step, "timeout": "20s"})
    result = data["result"]
    series = []
    for r in result[:MAX_SERIES]:
        values = [_num(v) for _, v in r["values"]]
        nums = [v for v in values if v is not None]
        series.append(
            {
                "labels": r["metric"],
                "first_ts": int(r["values"][0][0]) if r["values"] else None,
                "min": min(nums, default=None),
                "max": max(nums, default=None),
                "avg": round(sum(nums) / len(nums), 6) if nums else None,
                "last": values[-1] if values else None,
                "values": values,
            }
        )
    return to_json({"step_seconds": step, "minutes": minutes, "total_series": len(result), "shown": len(series), "series": series})


@server.tool()
@instrumented
async def list_firing_alerts(namespace: str | None = None) -> str:
    """현재 firing 또는 pending 상태인 알림 목록 (조회가 허용된 네임스페이스의 알림만). namespace 를 주면 그 네임스페이스만."""
    visible = set(allowed_namespaces()) | ALERT_NAMESPACES
    if namespace is not None and namespace not in visible:
        raise ToolInputError(f"namespace '{namespace}' 의 알림은 조회할 수 없습니다. 허용: {', '.join(sorted(visible))}")
    data = await _api("alerts", {})
    alerts = [
        {
            "name": a["labels"].get("alertname"),
            "state": a["state"],
            "severity": a["labels"].get("severity"),
            "labels": a["labels"],
            "summary": a.get("annotations", {}).get("summary") or a.get("annotations", {}).get("description"),
            "active_at": a.get("activeAt"),
        }
        for a in data.get("alerts", [])
        if a["state"] in ("firing", "pending")
        and a["labels"].get("namespace") in visible
        and (namespace is None or a["labels"].get("namespace") == namespace)
    ]
    alerts.sort(key=lambda a: (a["state"] != "firing", a["active_at"] or ""))
    return to_json({"count": len(alerts), "alerts": alerts[:50]})


if __name__ == "__main__":
    serve(server, "mcp-prometheus")

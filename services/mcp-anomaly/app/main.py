"""mcp-anomaly — demo 서비스의 최근 지표를 이상 탐지 모델(KServe)에 넣어 이상 구간을 알려 주는 MCP 서버.

도구: detect_anomalies
흐름: Prometheus 에서 1분 단위 특성 4개를 읽음 → KServe 예측 서버(V1) 호출 → 이상 구간으로 묶어 요약.
모델은 demo 서비스로만 학습했으므로 대상은 demo 하나다.
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone

import httpx2
from mcp.server.mcpserver import MCPServer

from mcp_common import ToolInputError, instrumented, serve, to_json

from .features import FEATURE_QUERIES

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://kube-prometheus-kube-prome-prometheus.monitoring.svc:9090")
MODEL_URL = os.environ.get(
    "MODEL_URL", "http://demo-anomaly-predictor.mlops.svc.cluster.local/v1/models/demo-anomaly:predict"
)
MAX_MINUTES = int(os.environ.get("MCP_MAX_RANGE_MINUTES", "360"))
STEP = 60

prom = httpx2.AsyncClient(base_url=PROM_URL, timeout=20.0)
model = httpx2.AsyncClient(timeout=20.0)

server = MCPServer(
    name="mcp-anomaly",
    instructions=(
        "demo 서비스 이상 탐지 모델 호출. 최근 지표(요청 수, 오류율, p95 지연, 메모리 사용률)를 학습된 모델로 점수 매겨 "
        "이상 구간과 가장 크게 벗어난 지표를 알려 준다. 장애 조사 초반에 '언제부터 무엇이 이상했나' 를 확인할 때 쓴다."
    ),
)


def _num(v: str) -> float | None:
    f = float(v)
    return None if math.isnan(f) or math.isinf(f) else f


async def _range(query: str, start: float, end: float) -> dict[int, float | None]:
    resp = await prom.get(
        "/api/v1/query_range", params={"query": query, "start": start, "end": end, "step": STEP, "timeout": "20s"}
    )
    body = resp.json()
    if body.get("status") != "success":
        raise ToolInputError(f"Prometheus 오류: {body.get('errorType')}: {body.get('error')}")
    result = body["data"]["result"]
    return {int(ts): _num(v) for ts, v in result[0]["values"]} if result else {}


KST = timezone(timedelta(hours=9))


def _kst(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=KST).strftime("%m-%d %H:%M")


def segments(rows: list[dict], preds: list[dict]) -> list[dict]:
    """연속된 이상 분을 구간으로 묶는다. 구간마다 시작·끝, 가장 자주 벗어난 지표, 최대 z, 그 구간 지표 최댓값."""
    out: list[dict] = []
    cur: dict | None = None
    for row, p in zip(rows, preds):
        if p["is_anomaly"]:
            if cur is None:
                cur = {"start": row["ts"], "end": row["ts"], "minutes": 0, "features": {}, "max_z": 0.0, "peak": {}}
            cur["end"] = row["ts"]
            cur["minutes"] += 1
            cur["features"][p["top_feature"]] = cur["features"].get(p["top_feature"], 0) + 1
            cur["max_z"] = max(cur["max_z"], p["top_z"])
            for k in ("error_ratio", "p95_seconds", "mem_ratio"):
                v = row.get(k)
                if v is not None:
                    cur["peak"][k] = max(cur["peak"].get(k, v), v)
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return [
        {
            "start": _kst(s["start"]),
            "end": _kst(s["end"]),
            "minutes": s["minutes"],
            "main_signal": max(s["features"], key=s["features"].get),
            "max_z": round(s["max_z"], 1),
            "peak": {k: round(v, 4) for k, v in s["peak"].items()},
        }
        for s in out
    ]


@server.tool()
@instrumented
async def detect_anomalies(minutes: int = 60) -> str:
    """demo 서비스의 최근 `minutes` 분(5~360)을 이상 탐지 모델로 검사한다.

    돌려주는 것: 이상으로 판정된 분 수, 이상 구간 목록(시작·끝 KST, 주된 지표, 최대 z-score, 구간 최댓값),
    지금(마지막 분) 상태, 모델 버전. main_signal 은 error_ratio(오류율), p95_log_ms(지연), mem_ratio(메모리), rps(요청 수) 중 하나.
    z-score 는 평소(학습 기간) 대비 몇 표준편차 벗어났는지다. 4 이상이면 이상으로 본다.
    """
    if not 5 <= minutes <= MAX_MINUTES:
        raise ToolInputError(f"minutes 는 5~{MAX_MINUTES} 사이여야 합니다")
    end = int(time.time()) // STEP * STEP
    start = end - minutes * STEP
    series = {name: await _range(q, start, end) for name, q in FEATURE_QUERIES.items()}
    stamps = sorted(series["rps"])
    if not stamps:
        return to_json({"status": "no_data", "message": "최근 demo 요청 지표가 없다 (수집 중단 또는 트래픽 없음)"})
    rows = [{"ts": ts, **{name: series[name].get(ts) for name in FEATURE_QUERIES}} for ts in stamps]
    resp = await model.post(MODEL_URL, json={"instances": [{k: r[k] for k in FEATURE_QUERIES} for r in rows]})
    if resp.status_code != 200:
        raise ToolInputError(f"모델 서버 오류 {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    preds = body["predictions"]
    segs = segments(rows, preds)
    last, last_p = rows[-1], preds[-1]
    return to_json(
        {
            "window": f"{_kst(stamps[0])} ~ {_kst(stamps[-1])} (KST, 1분 단위 {len(rows)}개)",
            "anomalous_minutes": sum(1 for p in preds if p["is_anomaly"]),
            "segments": segs[-10:],
            "now": {
                "is_anomaly": last_p["is_anomaly"],
                "main_signal": last_p["top_feature"],
                "z": last_p["top_z"],
                "values": {k: (round(last[k], 4) if last[k] is not None else None) for k in FEATURE_QUERIES},
            },
            "model_version": body.get("model_version"),
        }
    )


if __name__ == "__main__":
    serve(server, "mcp-anomaly")

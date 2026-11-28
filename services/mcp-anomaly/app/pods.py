"""파드별 채점: 서비스 합계로는 절반으로 보이는 "파드 하나의 문제" 를 가려낸다.

모델 입력(오류율, 지연)은 demo 전체 합계다. 파드 둘 중 하나가 오류 8% 를 내면 합계는 4% 라 판정 경계에 걸리고,
걸려도 어느 파드인지 알 수 없다. 같은 모델에 파드별 값을 한 줄씩 넣어 본다.
요청 수는 파드 하나의 몫이므로 파드 수를 곱해 합계와 같은 눈금으로 맞춘다 (모델의 평소 값이 합계 기준이다).

이 파일은 MCP 의존성 없이 import 되도록 상수와 순수 함수만 둔다.
"""

from __future__ import annotations

from .features import PATH_FILTER

_REQ = f'http_requests_total{{namespace="demo", {PATH_FILTER}}}'
_MEM = 'container_memory_working_set_bytes{namespace="demo", container="app"}'
_LIMIT = 'max by (pod) (kube_pod_container_resource_limits{namespace="demo", container="app", resource="memory"})'

POD_QUERIES = {
    "rps": f"sum by (pod) (rate({_REQ}[2m]))",
    "error_ratio": (
        f'sum by (pod) (rate(http_requests_total{{namespace="demo", {PATH_FILTER}, status=~"5.."}}[2m]))'
        f" / sum by (pod) (rate({_REQ}[2m]))"
    ),
    "p95_seconds": (
        "histogram_quantile(0.95, sum by (le, pod) "
        f'(rate(http_request_duration_seconds_bucket{{namespace="demo", {PATH_FILTER}}}[2m])))'
    ),
    "mem_ratio": f"max by (pod) ({_MEM}) / {_LIMIT}",
    "mem_growth": (
        f"(max by (pod) (deriv({_MEM}[5m])) * 60 / {_LIMIT})"
        ' and on (pod) (time() - max by (pod) (kube_pod_container_state_started{namespace="demo", container="app"}) > 300)'
    ),
}
MAX_PODS = 20


IFOREST_MIN_Z = 2.0  # 모델의 iforest_min_z 와 같은 값


def pod_rows(series: dict[str, dict[str, float | None]]) -> list[tuple[str, dict]]:
    """질의 결과({특성: {파드: 값}})를 모델에 넣을 행으로. 요청을 받고 있는 파드만 본다 (격리됐거나 막 뜬 파드는 뺀다)."""
    pods = sorted(p for p, v in series.get("rps", {}).items() if v is not None and v > 0)[:MAX_PODS]
    n = len(pods)
    return [
        (pod, {"rps": series["rps"][pod] * n, **{f: series.get(f, {}).get(pod) for f in POD_QUERIES if f != "rps"}})
        for pod in pods
    ]


def pod_findings(rows: list[tuple[str, dict]], preds: list[dict]) -> dict | None:
    """이상으로 판정된 파드만 추린다. 파드가 둘 미만이면 합계와 같은 말이라 None."""
    if len(rows) < 2:
        return None
    bad = []
    for (pod, row), p in zip(rows, preds):
        # 요청 급감은 파드별로 보지 않는다 (파드 수를 곱한 값이라 분산이 고르지 않으면 가짜 급감이 된다)
        # iforest_anomaly 는 확정 전 값이다 (모델은 어느 특성이든 |z| >= 2 일 때만 인정한다). 요청이 적을 때 가장 자주 켜지므로
        # 그대로 믿으면 "급감이라 건너뛴다" 는 조건이 바로 그 경우에 뚫린다. 같은 확인을 여기서도 한다.
        iforest = bool(p.get("iforest_anomaly")) and abs(p.get("top_z") or 0) >= IFOREST_MIN_Z
        if not p.get("is_anomaly") or (p.get("traffic_drop") and not p.get("zscore_anomaly") and not iforest):
            continue
        signal = p["top_feature"]
        raw_key = "p95_seconds" if signal == "p95_log_ms" else signal
        value = row.get(raw_key)
        bad.append({"pod": pod, "main_signal": signal, "z": p["top_z"], "value": round(value, 4) if value is not None else None})
    bad.sort(key=lambda b: -b["z"])
    out = {"checked": len(rows), "anomalous": bad}
    if bad and len(bad) < len(rows):
        out["note"] = "일부 파드만 이상이다. 서비스 전체 문제가 아니라 이 파드의 문제일 수 있다"
    return out

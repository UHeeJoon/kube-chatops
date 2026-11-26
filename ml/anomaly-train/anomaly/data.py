"""학습 데이터: Prometheus 에서 1분 단위 특성과 정답(장애 주입 여부)을 읽는다.

특성 PromQL 은 demo 알림 규칙(PrometheusRule)과 같은 필터를 쓴다.
mcp-anomaly 가 서빙 때 같은 질의를 쓴다 (services/mcp-anomaly/app/features.py 와 맞출 것).
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone

import boto3
import httpx
import pandas as pd

PATH_FILTER = 'path!~"/chaos/.*|/metrics|/healthz.*"'

FEATURE_QUERIES = {
    "rps": f'sum(rate(http_requests_total{{namespace="demo", {PATH_FILTER}}}[2m]))',
    "error_ratio": (
        f'sum(rate(http_requests_total{{namespace="demo", {PATH_FILTER}, status=~"5.."}}[2m]))'
        f' / sum(rate(http_requests_total{{namespace="demo", {PATH_FILTER}}}[2m]))'
    ),
    "p95_seconds": (
        "histogram_quantile(0.95, sum by (le) "
        f'(rate(http_request_duration_seconds_bucket{{namespace="demo", {PATH_FILTER}}}[2m])))'
    ),
    "mem_ratio": (
        'max(max by (pod) (container_memory_working_set_bytes{namespace="demo", container="app"})'
        ' / max by (pod) (kube_pod_container_resource_limits{namespace="demo", container="app", resource="memory"}))'
    ),
}

# demo-app 이 장애 주입을 켜면 1, 끄면 0 (kind = latency | error | leak)
LABEL_QUERY = 'max by (kind) (demo_chaos_active{namespace="demo"})'

STEP_SECONDS = 60
CHUNK = timedelta(hours=12)  # query_range 는 시계열당 11,000 점까지라 나눠서 읽는다
# rate(...[2m]) 는 장애를 켜고 끈 뒤 2분 정도 늦게 따라온다. 그 구간은 정답이 애매해 학습·평가에서 뺀다.
AMBIGUOUS_MINUTES = 3


def prom_url() -> str:
    return os.environ.get("PROM_URL", "http://kube-prometheus-kube-prome-prometheus.monitoring.svc:9090")


def _query_range(client: httpx.Client, query: str, start: datetime, end: datetime) -> list[dict]:
    out: list[dict] = []
    t = start
    while t < end:
        t2 = min(t + CHUNK, end)
        r = client.get(
            f"{prom_url()}/api/v1/query_range",
            params={"query": query, "start": t.timestamp(), "end": t2.timestamp(), "step": STEP_SECONDS},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus 오류: {body}")
        out.extend(body["data"]["result"])
        t = t2 + timedelta(seconds=STEP_SECONDS)
    return out


def _to_series(results: list[dict], name: str) -> pd.Series:
    points = [(float(ts), float(v)) for r in results for ts, v in r["values"]]
    if not points:
        return pd.Series(dtype=float, name=name)
    s = pd.Series({pd.Timestamp(ts, unit="s", tz="UTC"): v for ts, v in points}, name=name)
    return s[~s.index.duplicated()].sort_index()


def fetch_raw(start: datetime, end: datetime) -> pd.DataFrame:
    """[start, end] 의 1분 단위 특성 원값 (열: RAW_COLUMNS, 인덱스: UTC 시각)."""
    with httpx.Client(timeout=60) as c:
        cols = {name: _to_series(_query_range(c, q, start, end), name) for name, q in FEATURE_QUERIES.items()}
    df = pd.concat(cols.values(), axis=1)
    df.index.name = "ts"
    return df


def fetch_labels(start: datetime, end: datetime, index: pd.Index) -> pd.DataFrame:
    with httpx.Client(timeout=60) as c:
        results = _query_range(c, LABEL_QUERY, start, end)
    # 구간(CHUNK)마다 같은 kind 가 따로 오므로 kind 별로 모아서 한 시계열로 만든다
    by_kind: dict[str, list[dict]] = {}
    for r in results:
        by_kind.setdefault(r["metric"].get("kind", "unknown"), []).append(r)
    raw = pd.DataFrame({kind: _to_series(rs, kind).reindex(index) for kind, rs in sorted(by_kind.items())}, index=index)
    kinds = raw.fillna(0) > 0
    label = kinds.any(axis=1) if len(kinds.columns) else pd.Series(False, index=index)
    kind_col = kinds.apply(lambda row: ",".join(k for k, on in row.items() if on), axis=1) if len(kinds.columns) else ""
    out = pd.DataFrame({"label": label.astype(int), "kind": kind_col}, index=index)
    # 정답 지표가 아예 없던 분(지표 도입 전, 수집 중단)은 장애 주입이 있었어도 알 수 없다 → 뺀다
    out["labeled"] = raw.notna().any(axis=1).values if len(raw.columns) else False
    # 정답이 바뀐 시각부터 AMBIGUOUS_MINUTES 분은 애매 구간
    changed = out["label"].diff().fillna(0) != 0
    ambiguous = changed.astype(int).rolling(AMBIGUOUS_MINUTES, min_periods=1).max() > 0
    out["ambiguous"] = ambiguous.values
    return out


def build_dataset(days: float) -> pd.DataFrame:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    raw = fetch_raw(start, end)
    # 트래픽 지표가 없는 분(수집 중단, 노드 장애)은 정상/이상을 말할 수 없어 뺀다
    raw = raw[raw["rps"].notna()]
    labels = fetch_labels(start, end, raw.index)
    df = raw.join(labels)
    # 정답이 없는 분을 정상으로 두면, 그때 넣은 장애가 "정상" 으로 학습된다 (11-25 실제로 겪음)
    return df[df.pop("labeled")].reset_index()


# ── MinIO (S3) 입출력. 단계 사이에 데이터를 넘길 때만 쓴다. 모델 아티팩트는 MLflow 가 관리한다. ──


def _s3():
    return boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio.storage.svc.cluster.local"))


def bucket() -> str:
    return os.environ.get("S3_BUCKET", "mlops")


def put_frame(df: pd.DataFrame, key: str) -> str:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    _s3().put_object(Bucket=bucket(), Key=key, Body=buf.getvalue())
    return key


def get_frame(key: str) -> pd.DataFrame:
    body = _s3().get_object(Bucket=bucket(), Key=key)["Body"].read()
    return pd.read_parquet(io.BytesIO(body))

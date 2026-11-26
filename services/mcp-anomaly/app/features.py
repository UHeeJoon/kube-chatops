"""모델 입력 특성 PromQL. 학습(ml/anomaly-train/anomaly/data.py)과 글자 하나까지 같아야 한다.

다르면 모델이 학습 때와 다른 값을 받는다. ml/anomaly-train/tests/test_feature_parity.py 가 CI 에서 비교한다.
이 파일은 MCP 의존성 없이 import 되도록 상수만 둔다.
"""

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

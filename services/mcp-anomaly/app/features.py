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
    # 메모리 증가 속도 (분당, 한도 대비). 누수를 메모리가 아직 낮을 때부터 잡는다.
    # 재시작 직후 5분은 새 컨테이너 메모리가 0 에서 차오르며 가짜 급증이 생기므로 값을 비운다 (→ 모델이 평소 값으로 봄).
    "mem_growth": (
        'max(max by (pod) (deriv(container_memory_working_set_bytes{namespace="demo", container="app"}[5m])) * 60'
        ' / max by (pod) (kube_pod_container_resource_limits{namespace="demo", container="app", resource="memory"})'
        ' and on (pod) (time() - max by (pod) (kube_pod_container_state_started{namespace="demo", container="app"}) > 300))'
    ),
}

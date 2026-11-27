"""mcp-prometheus: PromQL 의 지표 선택마다 허용 네임스페이스 조건이 있어야 한다."""

import asyncio

import pytest
from mcp_common import ToolInputError
from mcp_common.testing import Blocked, assert_tools_scoped

from app import main
from app.scope import check_query

UNSCOPED = {
    "query_instant": "네임스페이스 인자 대신 PromQL 을 받는다. scope.check_query 가 선택마다 검사한다 (아래 시험)",
    "query_range": "query_instant 와 같다",
}

ALLOWED = [
    'sum by (path, status) (rate(http_requests_total{namespace="demo"}[5m]))',
    'kube_pod_info{namespace="demo"}',
    'max by (pod) (container_memory_working_set_bytes{namespace="demo", container="app"}) / max by (pod) (kube_pod_container_resource_limits{namespace="demo", resource="memory"})',
    "node_memory_MemAvailable_bytes",
    "anomaly_score",
    "sum(demo_chaos_active) by (kind)",
    'ALERTS{namespace="demo", alertstate="firing"}',
    'hubble_flows_to_world_total{source="demo"}',
    '{__name__="kube_pod_info", namespace="demo"}',
    'histogram_quantile(0.95, sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m])))',
    'tetragon_policy_events_total{exported_namespace="demo"}',
]

DENIED = [
    "kube_pod_info",
    "count(kube_pod_info) by (namespace)",
    'kube_pod_info{namespace="sapari"}',
    'kube_pod_info{namespace=~"demo|sapari"}',
    'kube_pod_info{namespace=~"demo"}',  # 정규식은 일치 범위를 넓힐 수 있어 = 만 받는다
    'kube_pod_info{namespace!="x"}',
    'kube_pod_info{namespace="demo"} or kube_pod_info',
    'kube_pod_info{namespace="demo"} / on() group_left kube_secret_info',
    '{__name__=~".+"}',
    '{__name__=~"kube_secret.*"}',
    'kube_pod_info{namespace="demo" or namespace="sapari"}',
    'kube_pod_info{namespace="demo" or pod="x"}',
    'sum(rate(container_cpu_usage_seconds_total[5m])) + sum(kube_pod_info{namespace="demo"})',
    'label_replace(kube_secret_info, "namespace", "demo", "", "")',
    'kube_pod_info{pod="demo"}',  # 범위 라벨이 아닌 라벨의 값이 demo
    'absent(kube_secret_info{namespace="kube-system"})',
    'ALERTS',
    'kube_pod_info{namespace="demo"} offset 5m and on() vector(1) or up',
    "x" * 2001,
    "sum(",
]


@pytest.fixture(autouse=True)
def no_backend(monkeypatch):
    monkeypatch.setattr(main, "http", Blocked("http"))


@pytest.mark.parametrize("query", ALLOWED)
def test_scoped_queries_pass(query):
    check_query(query)


@pytest.mark.parametrize("query", DENIED)
def test_unscoped_queries_are_rejected(query):
    with pytest.raises(ToolInputError):
        check_query(query)


@pytest.mark.parametrize("query", DENIED[:6])
def test_tools_reject_before_backend(query):
    for tool in (main.query_instant, main.query_range):
        out = asyncio.run(tool(query=query))
        assert out.startswith("입력 오류"), out[:200]


def test_every_tool_is_scoped_or_reviewed():
    checked = asyncio.run(assert_tools_scoped(main, UNSCOPED))
    assert checked == ["list_firing_alerts"]

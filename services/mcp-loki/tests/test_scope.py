"""mcp-loki: 모든 도구가 허용 네임스페이스 밖을 거절하고, 입력이 LogQL 선택자를 벗어나지 못한다."""

import asyncio

import pytest
from mcp_common import ToolInputError
from mcp_common.testing import Blocked, assert_tools_scoped

from app import main

UNSCOPED = {
    "security_events": "네임스페이스를 받지 않는다. SECURITY_NAMESPACE(demo) 고정이고 결과도 그 값으로 다시 거른다 (아래 시험)",
    "waf_blocks": "네임스페이스를 받지 않는다. WAF_SELECTOR(demo 의 waf 컨테이너) 고정",
}


@pytest.fixture(autouse=True)
def no_backend(monkeypatch):
    monkeypatch.setattr(main, "http", Blocked("http"))


def test_every_tool_is_scoped_or_reviewed():
    checked = asyncio.run(assert_tools_scoped(main, UNSCOPED))
    assert checked == ["error_summary", "search_logs"]


def test_fixed_selectors_stay_in_demo():
    assert main.SECURITY_NAMESPACE == "demo"
    assert main.WAF_SELECTOR.startswith('{namespace="demo"')
    assert main.TETRAGON_SELECTOR == '{namespace="kube-system", container="export-stdout"}'


@pytest.mark.parametrize("service", ['demo-app"} or {namespace="kube-system', "demo-app.*", "a|b", "Demo", "-x", 'x",namespace="kube-system'])
def test_service_cannot_break_selector(service):
    with pytest.raises(ToolInputError):
        main._selector("demo", service)


def test_contains_is_quoted():
    """contains 는 줄 필터 문자열로만 들어간다: 따옴표를 넣어도 선택자나 새 단계를 만들지 못한다."""
    q = main._quote('x" | json | namespace="kube-system')
    assert q.startswith('"') and q.endswith('"') and '\\"' in q
    assert main._selector("demo", "demo-app") == '{namespace="demo", pod=~"demo-app-[a-z0-9]+(-[a-z0-9]+)?"}'


@pytest.mark.parametrize("pod", ['x"} |= "', "a b", "Pod", 'x","namespace":"kube-system'])
def test_security_events_pod_is_validated(pod):
    out = asyncio.run(main.security_events(pod=pod))
    assert out.startswith("입력 오류"), out[:200]

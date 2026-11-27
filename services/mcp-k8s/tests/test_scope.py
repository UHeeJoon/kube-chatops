"""mcp-k8s: 모든 도구가 허용 네임스페이스 밖을 거절한다 (백엔드 호출 없이)."""

import asyncio

import kubernetes.config as kube_config
import pytest

kube_config.load_incluster_config = lambda *a, **k: None  # 클러스터 없이 import

from mcp_common.testing import BackendCalled, Blocked, assert_tools_scoped  # noqa: E402

from app import main  # noqa: E402

UNSCOPED: dict[str, str] = {}  # 모든 도구가 namespace 를 받는다


@pytest.fixture(autouse=True)
def no_backend(monkeypatch):
    monkeypatch.setattr(main, "core", Blocked("core"))
    monkeypatch.setattr(main, "apps", Blocked("apps"))


def test_every_tool_rejects_outside_namespaces():
    checked = asyncio.run(assert_tools_scoped(main, UNSCOPED))
    assert checked == ["describe_deployment", "get_events", "get_pod_status", "rollout_history"]


def test_allowed_namespace_reaches_backend():
    """검사가 전부 막아 버리는 것이 아님을 확인: 허용된 값은 백엔드까지 간다."""
    with pytest.raises(BackendCalled):
        asyncio.run(main.get_pod_status.__wrapped__(namespace="demo"))


@pytest.mark.parametrize("name", ["x,involvedObject.namespace=kube-system", "a=b", "x y", "X" * 300])
def test_names_are_validated(name):
    """이름은 필드 선택자에 들어간다: 쉼표·등호로 조건을 덧붙일 수 없어야 한다."""
    for out in (asyncio.run(main.get_events(namespace="demo", object_name=name)),
                asyncio.run(main.describe_deployment(name=name)), asyncio.run(main.rollout_history(name=name))):
        assert out.startswith("입력 오류"), out[:200]


def test_usual_inputs_pass_validation():
    assert main._name("demo-app-749c444bf7-85wwd") and main._name("demo-app.17f3a")
    for ok in ("app.kubernetes.io/name=demo-app", "app=demo-app,tier!=db", "app=="):
        assert main.LABEL_SELECTOR.fullmatch(ok), ok


@pytest.mark.parametrize("selector", ["app=x,", "a" * 300, "app in (x); drop", "app=x\n"])
def test_label_selector_is_validated(selector):
    out = asyncio.run(main.get_pod_status(namespace="demo", label_selector=selector))
    assert out.startswith("입력 오류"), out[:200]

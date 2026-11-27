"""MCP 서버 공통 시험 도구: 모든 도구가 조회 범위를 지키는지 확인한다.

왜: 서버 넷 중 mcp-prometheus 만 네임스페이스 제한 없이 PromQL 을 넘기고 있었고 사흘 동안 몰랐다 (11-27).
"다른 서버가 지키니 이것도 지키겠지" 를 시험으로 바꾼다. 도구 목록은 서버에서 직접 읽으므로, 새 도구를 추가하면
namespace 인자를 받아 범위 밖을 거절하거나, 왜 범위 검사가 필요 없는지(또는 어떻게 따로 검사하는지)를 UNSCOPED 에 적어야 통과한다.
"""

from __future__ import annotations

import inspect
from types import ModuleType

OUTSIDE = ("kube-system", "sapari", "", "demo2", "Demo", "demo ", "*", ".*", "demo|kube-system", 'demo"} or {namespace="kube-system')


class BackendCalled(AssertionError):
    """범위 검사를 지나 백엔드까지 갔다."""


class Blocked:
    """백엔드 클라이언트 자리에 넣는다. 무엇을 부르든 실패한다."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __getattr__(self, name: str):
        raise BackendCalled(f"{self._what}.{name} 이 호출됐다: 범위 검사 전에 백엔드로 나갔다")

    def __call__(self, *args, **kwargs):
        raise BackendCalled(f"{self._what} 가 호출됐다: 범위 검사 전에 백엔드로 나갔다")


def _dummy(param: inspect.Parameter):
    return param.default if param.default is not inspect.Parameter.empty else "x"


async def tool_names(module: ModuleType) -> list[str]:
    listed = await module.server.list_tools()
    return sorted(t.name for t in getattr(listed, "tools", listed))


async def assert_tools_scoped(module: ModuleType, unscoped: dict[str, str]) -> list[str]:
    """등록된 도구를 모두 확인한다. 돌려주는 값은 namespace 로 검사한 도구 이름.

    - namespace 인자가 있는 도구: 범위 밖 값마다 "입력 오류" 로 거절해야 한다 (백엔드는 호출 전에 Blocked 로 바꿔 둘 것).
    - 없는 도구: unscoped 에 이유가 적혀 있어야 한다.
    """
    names = await tool_names(module)
    assert names, "등록된 도구가 없다"
    stale = set(unscoped) - set(names)
    assert not stale, f"UNSCOPED 에 없는 도구가 적혀 있다: {sorted(stale)}"
    checked = []
    for name in names:
        fn = getattr(module, name)
        assert hasattr(fn, "__wrapped__"), f"{name}: @instrumented 가 없다 (감사 로그와 오류 변환이 빠진다)"
        params = inspect.signature(fn).parameters
        if "namespace" not in params:
            assert name in unscoped, f"{name}: namespace 인자가 없는데 UNSCOPED 에 검토 이유도 없다"
            continue
        assert name not in unscoped, f"{name}: namespace 인자가 있으니 UNSCOPED 에서 빼라"
        kwargs = {k: _dummy(p) for k, p in params.items() if k != "namespace"}
        for ns in OUTSIDE:
            out = await fn(namespace=ns, **kwargs)
            assert out.startswith("입력 오류") and "조회할 수 없습니다" in out, f"{name}(namespace={ns!r}) 가 거절되지 않았다: {out[:200]}"
        checked.append(name)
    return checked

"""PromQL 조회 범위 검사.

PromQL 은 문자열이라 그대로 넘기면 어느 네임스페이스의 지표든 읽힌다. 질의를 파싱해 지표 선택(`metric{...}`)마다
허용된 네임스페이스로 좁혀져 있는지 확인하고, 아니면 Prometheus 에 보내지 않는다.

통과 조건 (선택마다 하나):
  - 범위 라벨 중 하나가 허용 네임스페이스와 `=` 로 일치 (정규식 `=~` 은 인정하지 않는다)
  - 지표 이름이 허용 접두사로 시작 (네임스페이스와 무관한 지표: 노드, 이상 탐지 등)
"""

from __future__ import annotations

import os

import promql_parser as pq

from mcp_common import ToolInputError, allowed_namespaces

# 값이 네임스페이스인 라벨. source/destination 은 Hubble 의 flows_to_world 등에서 워크로드의 네임스페이스로 쓰인다.
SCOPE_LABELS = ("namespace", "exported_namespace", "source_namespace", "destination_namespace", "source", "destination")
PREFIXES = tuple(p for p in os.environ.get("ALLOWED_METRIC_PREFIXES", "node_,kube_node_,anomaly_,demo_").split(",") if p)
MAX_QUERY_CHARS = 2000


def _scoped(matchers: list, allowed: list[str]) -> bool:
    return any(m.name in SCOPE_LABELS and m.op == pq.MatchOp.Equal and m.value in allowed for m in matchers)


def _name(selector, matchers: list) -> str | None:
    if selector.name:
        return selector.name
    return next((m.value for m in matchers if m.name == "__name__" and m.op == pq.MatchOp.Equal), None)


def check_query(query: str) -> None:
    """범위를 벗어난 질의면 ToolInputError. 고치는 방법을 오류 문장에 적어 모델이 다시 조회하게 한다."""
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputError(f"query 가 너무 깁니다 (최대 {MAX_QUERY_CHARS}자)")
    try:
        ast = pq.parse(query)
    except Exception as e:  # noqa: BLE001 — 파서 오류는 모두 입력 오류
        raise ToolInputError(f"PromQL 문법 오류: {e}") from e
    allowed = allowed_namespaces()
    selectors: list = []

    def visit(node) -> bool:
        if isinstance(node, pq.VectorSelector):
            selectors.append(node)
        return True

    pq.walk(ast, visit)
    for s in selectors:
        base = list(s.matchers.matchers)
        # `{a="1" or b="2"}` 는 갈래마다 따로 본다 (한 갈래라도 범위 밖이면 거부)
        branches = [base + list(alt) for alt in s.matchers.or_matchers] or [base]
        for matchers in branches:
            name = _name(s, matchers)
            if _scoped(matchers, allowed) or (name is not None and name.startswith(PREFIXES)):
                continue
            raise ToolInputError(
                f"조회 범위 밖: `{name or '{...}'}` 에 네임스페이스 조건이 없습니다. "
                f"선택마다 {' 또는 '.join(SCOPE_LABELS[:3])} 라벨을 `=` 로 지정하세요 (허용: {', '.join(allowed)}). "
                f'예: {name or "metric"}{{namespace="{allowed[0]}"}}. 네임스페이스와 무관하게 볼 수 있는 지표 접두사: {", ".join(PREFIXES)}'
            )

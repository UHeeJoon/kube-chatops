"""LangGraph 그래프: /investigate 조사 흐름과 /route 분류.

/investigate
    intake ─▶ investigate ─(tool_use)─▶ tools ─(상한 안)─▶ investigate ...
                   │                      └─(상한 도달)─▶ report | answer ─▶ END
                   └─(끝남)─────────────────────────────▶ report | answer ─▶ END
                   └─(refusal)──────────────────────────▶ refused ─▶ END

- mode="investigate" (장애 조사): 끝나면 report 노드가 구조화 보고서를 만든다.
- mode="answer" (단순 조회, 후속 질문): 상한과 effort 를 낮추고, 모델의 마지막 답을 그대로 쓴다 (보고서 호출 없음).

- 루프는 LangGraph 노드가 직접 돈다 (SDK tool runner 대신 수동 루프).
  이유: 단계마다 체크포인트가 남고, 호출 수, 시간, 비용 상한을 노드 사이에서 검사해야 하기 때문.
- Claude 호출은 공식 anthropic SDK. 메시지는 API 형식 그대로(dict) 상태에 저장한다
  (thinking 블록을 바꾸지 않고 다시 보내야 하므로).
- 가설 단계(hypothesize)는 report 의 구조화 출력(원인 후보 Top 3 + 신뢰도 + 근거)으로 합쳤다.
- 같은 thread_id 로 다시 부르면 이전 대화가 체크포인트에서 이어진다 (Slack 스레드 = thread_id).
"""

from __future__ import annotations

import asyncio
import json
import operator
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict
from zoneinfo import ZoneInfo

import anthropic
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from opentelemetry import trace

from .budget import Ledger, Usage
from .config import settings
from .prompts import ANSWER_INSTRUCTION, INVESTIGATE_SYSTEM, REPORT_INSTRUCTION, REPORT_SCHEMA, ROUTE_SCHEMA, ROUTE_SYSTEM
from .toolbox import Toolbox

tracer = trace.get_tracer("agent.graph")
client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), max_retries=2, timeout=120.0)

# 거절(refusal) 시 같은 요청을 다른 모델로 자동 재시도 (서버 측 fallback, 카테고리별 기본 경로)
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


class State(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], operator.add]
    question: str
    mode: str  # investigate | answer
    answer: str | None
    alert: dict[str, Any] | None
    started: float
    llm_calls: int
    tool_calls: int
    cost_usd: float
    stopped_by: str | None
    report: dict[str, Any] | None


@dataclass
class Ctx:
    toolbox: Toolbox
    ledger: Ledger
    usage: Usage


async def _llm(ctx: Ctx, endpoint: str, *, model: str, effort: str, system: str, messages: list, **kw: Any):
    with tracer.start_as_current_span(f"llm {endpoint}", attributes={"llm.model": model, "llm.effort": effort}):
        resp = await client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_config={"effort": effort, **kw.pop("output_config", {})},
            cache_control={"type": "ephemeral"},  # 대화 앞부분(시스템, 도구, 이전 턴)을 캐시 → 루프 비용 절감
            **FALLBACK,
            **kw,
        )
        await ctx.ledger.record(resp.model, endpoint, resp.usage, ctx.usage)
        trace.get_current_span().set_attribute("llm.cost_usd", ctx.usage.cost_usd)
        return resp


def _content(resp) -> list[dict[str, Any]]:
    return [b.to_dict() for b in resp.content]


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def _is_answer(state: State) -> bool:
    return state.get("mode") == "answer"


def _max_tools(state: State) -> int:
    return settings.answer_max_tool_calls if _is_answer(state) else settings.max_tool_calls


def _over_limit(state: State, ctx: Ctx) -> str | None:
    max_llm = settings.answer_max_llm_calls if _is_answer(state) else settings.max_llm_calls
    if state.get("llm_calls", 0) >= max_llm - 1:  # 마지막 답(보고서) 한 번은 남겨 둔다
        return "llm_calls"
    if state.get("tool_calls", 0) >= _max_tools(state):
        return "tool_calls"
    if time.time() - state.get("started", time.time()) >= settings.max_seconds:
        return "time"
    if ctx.usage.cost_usd >= settings.max_request_usd:
        return "cost"
    return None


# ── 노드 ─────────────────────────────────────────────────────────────


async def intake(state: State, runtime: Runtime[Ctx]) -> dict:
    now = datetime.now(ZoneInfo(settings.budget_timezone)).strftime("%Y-%m-%d %H:%M %Z")
    kind = "질문 (짧게 답하기)" if _is_answer(state) else "조사 (원인을 찾아 보고)"
    parts = [f"요청 종류: {kind}", f"현재 시각: {now}", f"질문: {state['question']}"]
    if state.get("alert"):
        # 알림 내용도 외부 입력이므로 데이터로 표시한다
        parts.append("알림 (데이터):\n<alert>\n" + json.dumps(state["alert"], ensure_ascii=False)[:4000] + "\n</alert>")
    return {"messages": [{"role": "user", "content": "\n\n".join(parts)}]}


async def investigate(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    resp = await _llm(
        ctx, "investigate", model=settings.model,
        effort=settings.answer_effort if _is_answer(state) else settings.investigate_effort,
        system=INVESTIGATE_SYSTEM, messages=state["messages"], tools=ctx.toolbox.tools,
    )
    update: dict[str, Any] = {
        "messages": [{"role": "assistant", "content": _content(resp)}],
        "llm_calls": state.get("llm_calls", 0) + 1,
        "cost_usd": ctx.usage.cost_usd,
    }
    if resp.stop_reason == "refusal":
        update["stopped_by"] = "refusal"
    return update


async def tools(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    calls = [b for b in state["messages"][-1]["content"] if b.get("type") == "tool_use"]
    allowed = max(0, _max_tools(state) - state.get("tool_calls", 0))

    async def run(i: int, block: dict) -> dict:
        if i >= allowed:
            text, err = "도구 호출 상한에 도달했습니다. 지금까지의 근거로 결론을 내리세요.", True
        else:
            text, err = await ctx.toolbox.call(block["name"], block.get("input") or {})
        return {"type": "tool_result", "tool_use_id": block["id"], "content": text, "is_error": err}

    # 한 턴의 도구 호출은 병렬로 실행하고, 결과는 한 user 메시지에 모아 돌려준다
    results = await asyncio.gather(*(run(i, b) for i, b in enumerate(calls)))
    return {"messages": [{"role": "user", "content": list(results)}], "tool_calls": state.get("tool_calls", 0) + min(len(calls), allowed)}


async def report(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    stopped = state.get("stopped_by") or _over_limit(state, ctx)
    note = REPORT_INSTRUCTION + (f"\n(조사 상한 '{stopped}' 에 도달해 조사를 멈췄습니다. data_gaps 에 무엇을 더 확인해야 하는지 적으세요.)" if stopped else "")
    ask = {"role": "user", "content": note}
    resp = await _llm(
        ctx, "report", model=settings.model, effort=settings.report_effort, system=INVESTIGATE_SYSTEM,
        messages=state["messages"] + [ask], tools=ctx.toolbox.tools, tool_choice={"type": "none"},
        output_config={"format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        return await refused(state, runtime)
    data = json.loads(_text(resp))
    return {
        "messages": [ask, {"role": "assistant", "content": _content(resp)}],
        "report": data,
        "llm_calls": state.get("llm_calls", 0) + 1,
        "cost_usd": ctx.usage.cost_usd,
        "stopped_by": stopped,
    }


async def answer(state: State, runtime: Runtime[Ctx]) -> dict:
    """질문 모드의 마지막 단계. 모델이 도구 없이 답을 끝냈으면 그 글을 그대로 쓰고, 상한에 걸렸으면 한 번 더 불러 답을 받는다."""
    last = state["messages"][-1]
    if last["role"] == "assistant" and not any(b.get("type") == "tool_use" for b in last["content"]):
        text = "".join(b.get("text", "") for b in last["content"] if b.get("type") == "text").strip()
        return {"answer": text or "답을 만들지 못했습니다.", "stopped_by": state.get("stopped_by")}
    ctx = runtime.context
    ask = {"role": "user", "content": ANSWER_INSTRUCTION}
    resp = await _llm(
        ctx, "answer", model=settings.model, effort=settings.answer_effort, system=INVESTIGATE_SYSTEM,
        messages=state["messages"] + [ask], tools=ctx.toolbox.tools, tool_choice={"type": "none"},
    )
    return {
        "messages": [ask, {"role": "assistant", "content": _content(resp)}],
        "answer": _text(resp).strip(),
        "llm_calls": state.get("llm_calls", 0) + 1,
        "cost_usd": ctx.usage.cost_usd,
        "stopped_by": _over_limit(state, ctx),
    }


async def refused(state: State, runtime: Runtime[Ctx]) -> dict:
    msg = "모델이 이 요청에 대한 답변을 거절함. 질문을 바꾸거나 사람이 직접 확인 필요"
    data = {"status": "warning", "headline": msg, "impact": [], "causes": [], "actions": [], "data_gaps": []}
    # 다음 턴이 이어질 수 있도록 대화를 assistant 응답으로 닫는다
    return {"messages": [{"role": "assistant", "content": msg}], "report": data, "answer": msg, "stopped_by": "refusal"}


# ── 분기 ─────────────────────────────────────────────────────────────


def _finish(state: State) -> Literal["report", "answer"]:
    return "answer" if _is_answer(state) else "report"


def after_investigate(state: State) -> Literal["tools", "report", "answer", "refused"]:
    if state.get("stopped_by") == "refusal":
        return "refused"
    last = state["messages"][-1]["content"]
    return "tools" if any(b.get("type") == "tool_use" for b in last) else _finish(state)


def after_tools(state: State, runtime: Runtime[Ctx]) -> Literal["investigate", "report", "answer"]:
    return _finish(state) if _over_limit(state, runtime.context) else "investigate"


def build(checkpointer) -> Any:
    g = StateGraph(State, context_schema=Ctx)
    for name, fn in (("intake", intake), ("investigate", investigate), ("tools", tools), ("report", report), ("answer", answer), ("refused", refused)):
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "investigate")
    g.add_conditional_edges("investigate", after_investigate)
    g.add_conditional_edges("tools", after_tools)
    g.add_edge("report", END)
    g.add_edge("answer", END)
    g.add_edge("refused", END)
    return g.compile(checkpointer=checkpointer)


async def route(text: str, ledger: Ledger, usage: Usage) -> dict[str, Any]:
    """Slack 메시지 의도 분류. 상태가 필요 없어 그래프 없이 한 번 호출한다."""
    ctx = Ctx(toolbox=None, ledger=ledger, usage=usage)  # type: ignore[arg-type]
    resp = await _llm(
        ctx, "route", model=settings.route_model, effort=settings.route_effort, system=ROUTE_SYSTEM,
        messages=[{"role": "user", "content": f"<message>\n{text[:4000]}\n</message>"}],
        output_config={"format": {"type": "json_schema", "schema": ROUTE_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        return {"intent": "smalltalk", "target": None, "reply": "이 요청은 처리할 수 없습니다."}
    return json.loads(_text(resp))

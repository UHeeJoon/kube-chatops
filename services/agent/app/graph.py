"""LangGraph 그래프: /investigate 조사 흐름과 /route 분류.

/investigate
    intake ─▶ investigate ─(도구 호출)─▶ tools ─▶ investigate ...
                   │                       └─ 상한 도달: 결과 뒤에 "이제 끝내라" 안내를 붙이고 한 번 더 investigate
                   └─(submit_report 또는 글로 답)─▶ finish ─▶ END
                   └─(refusal)─────────────────────▶ refused ─▶ END

- mode="investigate" (장애 조사): 모델이 조사 중에 submit_report 도구(strict 스키마)로 보고서를 낸다.
- mode="answer" (단순 조회, 후속 질문): 도구 상한을 낮추고, 모델이 글로 낸 답을 그대로 쓴다.

비용 (프롬프트 캐시) 규칙. 캐시는 앞부분이 1바이트라도 같아야 맞는다.
- 모든 조사·답 호출은 같은 모델, 같은 도구 목록(이름순 + submit_report), 같은 시스템 프롬프트, 같은 effort 로 부른다.
  보고서를 따로 부르면서 tool_choice, effort, 출력 형식을 바꾸면 대화 캐시가 통째로 깨져 다시 쓰게 된다 (측정: 조사 비용의 44%).
- 시스템 프롬프트 끝에 캐시 지점을 고정해서 서로 다른 질문끼리도 도구 + 시스템(약 8.8천 토큰)을 공유한다. 이 구간은 1시간 캐시다
  (SYSTEM_CACHE_TTL. 알림과 질문이 5분~1시간 간격으로 오는 일이 많아, 5분 캐시로는 조사마다 다시 썼다).
  대화 끝부분은 요청 최상위 cache_control(자동)이 따라간다.
- 바뀌는 값(시각, 모드, 질문, 알림)은 user 메시지에만 넣는다.

- 루프는 LangGraph 노드가 직접 돈다 (SDK tool runner 대신 수동 루프).
  이유: 단계마다 체크포인트가 남고, 호출 수, 시간, 비용 상한을 노드 사이에서 검사해야 하기 때문.
- 메시지는 API 형식 그대로(dict) 상태에 저장한다 (thinking 블록을 바꾸지 않고 다시 보내야 하므로).
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
from prometheus_client import Counter

from .budget import Ledger, Usage
from .config import settings
from .safety import clean, guard_tool_text, verify_references
from .prompts import FINISH_NOTE, INVESTIGATE_SYSTEM, REPORT_TOOL, ROUTE_SCHEMA, ROUTE_SYSTEM
from .toolbox import Toolbox
from .prefetch import prefetch as _prefetch
from .models import choose as _choose, sticky as _sticky

MODEL_PICK = Counter("agent_model_pick_total", "요청마다 고른 모델. tier: answer | light | heavy | thread(스레드가 쓰던 것)", ["tier", "model", "effort"])
LIGHT_ALERTS = frozenset(a.strip() for a in settings.light_alerts.split(",") if a.strip())
PREFETCH = Counter("agent_prefetch_total", "알림 조사의 선조회 결과 (런북을 붙였나, 확인 항목을 미리 실행했나)", ["alertname", "runbook", "checks"])
UNVERIFIED_REFS = Counter("agent_unverified_references_total", "보고서의 참고 항목 가운데 조회 기록에 없던 것")

tracer = trace.get_tracer("agent.graph")
client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), max_retries=2, timeout=120.0)

# 거절(refusal) 시 같은 요청을 다른 모델로 자동 재시도 (서버 측 fallback, 카테고리별 기본 경로).
# Haiku 에는 서버 측 fallback 이 없어서 붙이지 않는다.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

# 시스템 프롬프트 끝에 캐시 지점 고정 (도구 + 시스템을 모든 요청이 공유)
# 도구 정의와 시스템 프롬프트는 요청마다 같다 → 여기까지를 길게(기본 1시간) 캐시한다. 대화 쪽 자동 캐시(5분)보다 앞에 있어야 한다.
SYSTEM_CACHE = {"type": "ephemeral", "ttl": "1h"} if settings.system_cache_ttl == "1h" else {"type": "ephemeral"}
SYSTEM_BLOCKS = [{"type": "text", "text": INVESTIGATE_SYSTEM, "cache_control": SYSTEM_CACHE}]
REPORT = REPORT_TOOL["name"]


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
    finishing: bool  # 상한 도달 안내를 붙였음 (다음 호출이 마지막)
    stopped_by: str | None
    report: dict[str, Any] | None
    # 이 스레드가 읽은 글 가운데 지시처럼 보이는 문장이 있던 곳 (알림, 도구 이름). 요청이 바뀌어도 지우지 않는다:
    # 그 글은 대화에 남아 다음 턴의 모델도 읽으므로, 후속 조사의 조치 제안에도 승인 버튼을 붙이면 안 된다.
    tainted: Annotated[list[str], operator.add]
    model: str  # 이 스레드가 쓰는 모델과 effort. 첫 턴에 정하고 바꾸지 않는다 (thinking 블록, 대화 캐시)
    effort: str


@dataclass
class Ctx:
    toolbox: Toolbox
    ledger: Ledger
    usage: Usage


async def _llm(ctx: Ctx, endpoint: str, *, model: str, effort: str, system: Any, messages: list, **kw: Any):
    with tracer.start_as_current_span(f"llm {endpoint}", attributes={"llm.model": model, "llm.effort": effort}):
        resp = await client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_config={"effort": effort, **kw.pop("output_config", {})},
            cache_control={"type": "ephemeral"},  # 대화 끝부분 자동 캐시 (시스템 끝 지점과 함께 2개)
            **({} if "haiku" in model else FALLBACK),
            **kw,
        )
        await ctx.ledger.record(resp.model, endpoint, resp.usage, ctx.usage)
        span = trace.get_current_span()
        span.set_attribute("llm.cost_usd", ctx.usage.cost_usd)
        span.set_attribute("llm.cache_read", resp.usage.cache_read_input_tokens or 0)
        span.set_attribute("llm.cache_write", resp.usage.cache_creation_input_tokens or 0)
        return resp


def _content(resp) -> list[dict[str, Any]]:
    return [b.to_dict() for b in resp.content]


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def _tool_uses(msg: dict[str, Any]) -> list[dict[str, Any]]:
    c = msg.get("content")
    return [b for b in c if b.get("type") == "tool_use"] if isinstance(c, list) else []


def _is_answer(state: State) -> bool:
    return state.get("mode") == "answer"


def _max_tools(state: State) -> int:
    return settings.answer_max_tool_calls if _is_answer(state) else settings.max_tool_calls


def _over_limit(state: State, ctx: Ctx) -> str | None:
    max_llm = settings.answer_max_llm_calls if _is_answer(state) else settings.max_llm_calls
    if state.get("llm_calls", 0) >= max_llm - 1:  # 마지막 호출(보고서 또는 답) 한 번은 남겨 둔다
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
    kind = "질문 (짧게 답하기)" if _is_answer(state) else "조사 (원인을 찾아 submit_report 로 보고)"
    parts = [f"요청 종류: {kind}", f"현재 시각: {now}", f"질문: {state['question']}"]
    if _is_answer(state):  # "오늘 비용 얼마 썼어" 에 답할 수단이 없어 제공자 콘솔을 보라고 답했다 (11-28)
        try:
            parts.insert(2, f"오늘 LLM 비용: ${await runtime.context.ledger.spent_today():.2f} (하루 한도 ${settings.daily_budget_usd:.2f})")
        except Exception:  # noqa: BLE001  비용 줄이 없다고 답을 막지는 않는다
            pass
    # 앞의 실행이 도구 호출 도중에 끊겼으면(파드 재시작, 시간 초과) 대화가 결과 없는 tool_use 로 끝나 있다. 그 위에 새 메시지를 붙이면
    # API 가 거절해 이 스레드는 다시 쓸 수 없게 된다. 실행되지 않았다는 결과를 채워 대화를 이어 갈 수 있게 한다.
    dangling = _tool_uses(state["messages"][-1]) if state.get("messages") and state["messages"][-1].get("role") == "assistant" else []
    repair = [{"type": "tool_result", "tool_use_id": b["id"], "content": "실행되지 않았습니다 (앞의 조사가 중간에 끊겼습니다).", "is_error": True} for b in dangling]
    picked = _choose(state.get("mode") or "investigate", state.get("alert"), heavy=(settings.model, settings.investigate_effort),
                     light=(settings.light_model, settings.light_effort), answer=(settings.answer_model, settings.answer_effort), light_alerts=LIGHT_ALERTS)
    model, effort, tier = _sticky(state.get("model"), state.get("effort"), bool(state.get("messages")), picked, (settings.model, settings.investigate_effort))
    MODEL_PICK.labels(tier, model, effort).inc()
    if state.get("alert"):
        # 알림 내용도 외부 입력이므로 데이터로 표시한다 (라벨·주석에 공격자가 만든 글이 들어올 수 있다: 요청 경로, 파드 이름)
        body, alert_hits = guard_tool_text(json.dumps(state["alert"], ensure_ascii=False, sort_keys=True)[:4000])
        parts.append("알림 (데이터):\n<alert>\n" + body + "\n</alert>")
        seen = len(runtime.context.toolbox.suspicious)
        fetched = await _prefetch(runtime.context.toolbox, state["alert"])
        tainted = (["alert"] if alert_hits else []) + runtime.context.toolbox.suspicious[seen:]
        # 런북 빈틈: 알림이 왔는데 붙일 런북이 없었다 (런북이 없는 알림이 무엇인지 지표로 본다)
        name = str(((state["alert"].get("alerts") or [{}])[0].get("labels") or {}).get("alertname") or "unknown")[:60]
        PREFETCH.labels(name, "runbook" if any("<runbook>" in p for p in fetched) else "none",
                        "yes" if any("<checks>" in p for p in fetched) else "no").inc()
        parts += fetched
    else:
        tainted = []
    text = "\n\n".join(parts)
    return {"messages": [{"role": "user", "content": repair + [{"type": "text", "text": text}] if repair else text}], "model": model, "effort": effort, "tainted": tainted}


async def investigate(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    resp = await _llm(
        ctx, "answer" if _is_answer(state) else "investigate", model=state.get("model") or settings.model, effort=state.get("effort") or settings.investigate_effort,
        system=SYSTEM_BLOCKS, messages=state["messages"], tools=ctx.toolbox.tools + [REPORT_TOOL],
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
    calls = _tool_uses(state["messages"][-1])
    allowed = max(0, _max_tools(state) - state.get("tool_calls", 0))
    seen = len(ctx.toolbox.suspicious)

    async def run(i: int, block: dict) -> dict:
        if i >= allowed:
            text, err = "도구 호출 상한에 도달해 실행하지 않았습니다.", True
        else:
            text, err = await ctx.toolbox.call(block["name"], block.get("input") or {})
        return {"type": "tool_result", "tool_use_id": block["id"], "content": text, "is_error": err}

    # 한 턴의 도구 호출은 병렬로 실행하고, 결과는 한 user 메시지에 모아 돌려준다
    results: list[dict[str, Any]] = list(await asyncio.gather(*(run(i, b) for i, b in enumerate(calls))))
    update: dict[str, Any] = {"tool_calls": state.get("tool_calls", 0) + min(len(calls), allowed), "tainted": ctx.toolbox.suspicious[seen:]}
    stopped = _over_limit({**state, **update}, ctx)
    if stopped:
        # 결과 뒤에 안내 글을 붙인다. 앞부분은 그대로라 캐시가 깨지지 않는다.
        results.append({"type": "text", "text": FINISH_NOTE})
        update.update(finishing=True, stopped_by=stopped)
    update["messages"] = [{"role": "user", "content": results}]
    return update


async def finish(state: State, runtime: Runtime[Ctx]) -> dict:
    """마지막 응답에서 보고서(submit_report) 또는 답(글)을 꺼낸다. LLM 을 더 부르지 않는다."""
    last = state["messages"][-1]
    uses = _tool_uses(last)
    report = next((b.get("input") for b in uses if b["name"] == REPORT), None)
    text = "".join(b.get("text", "") for b in last["content"] if b.get("type") == "text").strip() if isinstance(last["content"], list) else ""
    # Slack 으로 나가는 글: 전체 호출, 멘션, 글자와 주소가 다른 링크를 풀어 쓴다
    report, text = clean(report), clean(text)
    if isinstance(report, dict):
        # 참고 항목이 이번 대화에서 실제로 받은 런북·사건인지 대조한다. 아니면 지우지 않고 표시한다
        report["references"], unverified = verify_references(report.get("references"), state["messages"])
        if unverified:
            UNVERIFIED_REFS.inc(unverified)
    out: dict[str, Any] = {"report": report, "answer": None if report else (text or "답을 만들지 못했습니다."),
                           "stopped_by": state.get("stopped_by")}
    if uses:
        # 다음 턴(같은 스레드 후속 질문)이 이어질 수 있게 tool_use 마다 결과를 주고 assistant 로 닫는다
        results = [{"type": "tool_result", "tool_use_id": b["id"],
                    "content": "보고서를 전달했습니다." if b["name"] == REPORT else "실행하지 않았습니다 (조사 종료)."} for b in uses]
        out["messages"] = [{"role": "user", "content": results},
                           {"role": "assistant", "content": [{"type": "text", "text": "보고를 마쳤습니다."}]}]
    return out


async def refused(state: State, runtime: Runtime[Ctx]) -> dict:
    msg = "모델이 이 요청에 대한 답변을 거절함. 질문을 바꾸거나 사람이 직접 확인 필요"
    data = {"status": "warning", "headline": msg, "impact": [], "timeline": [], "causes": [], "actions": [], "data_gaps": [], "references": []}
    # 다음 턴이 이어질 수 있도록 대화를 assistant 응답으로 닫는다
    return {"messages": [{"role": "assistant", "content": msg}], "report": data, "answer": msg, "stopped_by": "refusal"}


# ── 분기 ─────────────────────────────────────────────────────────────


def after_investigate(state: State) -> Literal["tools", "finish", "refused"]:
    if state.get("stopped_by") == "refusal":
        return "refused"
    uses = _tool_uses(state["messages"][-1])
    if not uses or state.get("finishing") or any(b["name"] == REPORT for b in uses):
        return "finish"
    return "tools"


def build(checkpointer) -> Any:
    g = StateGraph(State, context_schema=Ctx)
    for name, fn in (("intake", intake), ("investigate", investigate), ("tools", tools), ("finish", finish), ("refused", refused)):
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "investigate")
    g.add_conditional_edges("investigate", after_investigate)
    g.add_edge("tools", "investigate")
    g.add_edge("finish", END)
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

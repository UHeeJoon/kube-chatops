"""LLM 사용량과 비용 장부.

- 응답마다 usage(입력, 출력, 캐시 쓰기, 캐시 읽기 토큰)로 비용을 계산해 Postgres `agent_spend` 에 날짜별로 더한다.
- 하루 누적이 DAILY_BUDGET_USD 를 넘으면 새 요청을 거절한다 (요청 하나는 MAX_REQUEST_USD 로 따로 묶여 있어 초과 폭이 제한된다).
- 같은 값을 Prometheus 메트릭으로도 노출한다. 라벨 이름에 endpoint 는 쓰지 않는다 (ServiceMonitor 가 붙이는 endpoint 라벨과 겹쳐 덮어써진다).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from prometheus_client import Counter, Gauge
from psycopg_pool import AsyncConnectionPool

from .config import settings

log = logging.getLogger("agent.cost")

# 1M 토큰당 USD (입력, 출력, 캐시 읽기 배수). 캐시 쓰기는 입력의 1.25배(5분 TTL) 또는 2배(1시간 TTL).
# 캐시 읽기 배수는 모델마다 다르다 (Opus 5.5, Sonnet 5.5 는 0.05배). 확인하지 못한 값은 0.1배로 보수적으로 잡는다.
# 표에 없는 모델은 가장 비싼 값으로 계산해 예산을 보수적으로 잡는다.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-haiku-5-5": (0.10, 0.50, 0.1),
}
FALLBACK_PRICE = (10.0, 50.0, 0.1)

TOKENS = Counter("agent_llm_tokens_total", "LLM 토큰 수. kind: input(캐시 안 됨), cache_write, cache_read, output", ["model", "stage", "kind"])
COST = Counter("agent_llm_cost_usd_total", "LLM 비용 (USD, 추정). stage: route, investigate, report, answer", ["model", "stage"])
SPENT_TODAY = Gauge("agent_budget_spent_today_usd", "오늘 누적 비용 (USD)")
BUDGET_LIMIT = Gauge("agent_budget_daily_limit_usd", "하루 예산 (USD)")
BUDGET_LIMIT.set(settings.daily_budget_usd)

SCHEMA = """
create table if not exists agent_spend (
  day date primary key,
  cost_usd numeric(12, 6) not null default 0,
  input_tokens bigint not null default 0,
  output_tokens bigint not null default 0,
  llm_calls integer not null default 0
)
"""
TODAY = "(now() at time zone %(tz)s)::date"


@dataclass
class Usage:
    """요청 하나의 누적 사용량."""

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)


def cost_of(model: str, stage: str, usage) -> tuple[float, int, int]:
    p_in, p_out, read_mult = PRICES.get(model, FALLBACK_PRICE)
    cache_write = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    fresh = usage.input_tokens or 0
    out = usage.output_tokens or 0
    # 캐시 쓰기는 유효 시간에 따라 값이 다르다: 5분 1.25배, 1시간 2배. 나눠 준 값이 없으면 전부 5분으로 본다.
    detail = getattr(usage, "cache_creation", None)
    write_1h = min(cache_write, int(getattr(detail, "ephemeral_1h_input_tokens", 0) or 0)) if detail is not None else 0
    cost = (fresh * p_in + (cache_write - write_1h) * p_in * 1.25 + write_1h * p_in * 2.0 + cache_read * p_in * read_mult + out * p_out) / 1_000_000
    for kind, n in (("input", fresh), ("cache_write", cache_write), ("cache_read", cache_read), ("output", out)):
        TOKENS.labels(model, stage, kind).inc(n)
    # 호출 하나마다 한 줄. 캐시가 어느 단계에서 깨지는지 Loki 에서 바로 본다.
    log.info("llm_call stage=%s model=%s input=%d cache_write=%d cache_read=%d output=%d cost_usd=%.5f write_1h=%d",
             stage, model, fresh, cache_write, cache_read, out, cost, write_1h)
    return cost, fresh + cache_write + cache_read, out


class Ledger:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)
        await self.spent_today()

    async def spent_today(self) -> float:
        async with self.pool.connection() as conn:
            row = await (await conn.execute(f"select cost_usd from agent_spend where day = {TODAY}", {"tz": settings.budget_timezone})).fetchone()
        spent = float(row["cost_usd"]) if row else 0.0
        SPENT_TODAY.set(spent)
        return spent

    async def record(self, model: str, endpoint: str, response_usage, acc: Usage) -> float:
        cost, tin, tout = cost_of(model, endpoint, response_usage)
        acc.cost_usd += cost
        acc.input_tokens += tin
        acc.output_tokens += tout
        acc.llm_calls += 1
        acc.by_model[model] = acc.by_model.get(model, 0.0) + cost
        COST.labels(model, endpoint).inc(cost)
        async with self.pool.connection() as conn:
            await conn.execute(
                f"""insert into agent_spend (day, cost_usd, input_tokens, output_tokens, llm_calls)
                    values ({TODAY}, %(c)s, %(i)s, %(o)s, 1)
                    on conflict (day) do update set
                      cost_usd = agent_spend.cost_usd + excluded.cost_usd,
                      input_tokens = agent_spend.input_tokens + excluded.input_tokens,
                      output_tokens = agent_spend.output_tokens + excluded.output_tokens,
                      llm_calls = agent_spend.llm_calls + 1""",
                {"tz": settings.budget_timezone, "c": cost, "i": tin, "o": tout},
            )
        await self.spent_today()
        return cost


# 라벨 조합을 0 으로 미리 만든다. 카운터가 처음 생길 때의 값은 increase()/rate() 에 잡히지 않아서
# (첫 샘플이 이미 0 이 아님) 대시보드의 24시간 비용이 실제보다 작게 나온다.
for _model in PRICES:
    for _stage in ("route", "investigate", "report", "answer"):
        COST.labels(_model, _stage)
        for _kind in ("input", "cache_write", "cache_read", "output"):
            TOKENS.labels(_model, _stage, _kind)

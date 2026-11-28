"""비용 계산: 모델별 값, 날짜가 붙은 모델 이름, 캐시 쓰기 배수."""

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _load():
    try:
        from app import budget
        return budget
    except Exception:  # noqa: BLE001 — 설정에 필요한 환경이 없는 곳(CI 의 가벼운 환경)에서는 건너뛴다
        return None


def test_dated_model_name_uses_the_base_model_price():
    budget = _load()
    if budget is None:
        import pytest
        pytest.skip("app.budget 을 불러올 수 없음")
    assert budget.price_of("claude-opus-5-5-20260101") == budget.PRICES["claude-opus-5-5"]
    assert budget.price_of("claude-sonnet-5-5") == budget.PRICES["claude-sonnet-5-5"]
    assert budget.price_of("some-unknown-model") == budget.FALLBACK_PRICE
    u = types.SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=2000, cache_read_input_tokens=10000,
                              cache_creation=types.SimpleNamespace(ephemeral_1h_input_tokens=2000))
    cost, _, _ = budget.cost_of("claude-sonnet-5-5", "answer", u)
    # 입력 1000×2 + 캐시 쓰기(1시간) 2000×2×2 + 캐시 읽기 10000×2×0.05 + 출력 100×10, 100만 토큰당
    assert abs(cost - (2000 + 8000 + 1000 + 1000) / 1_000_000) < 1e-9

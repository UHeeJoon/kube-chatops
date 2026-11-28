"""일의 종류에 따라 모델과 effort 를 고른다. 의존성 없이 import 되도록 순수 함수만 둔다.

| 일 | 모델 | 이유 |
|---|---|---|
| 분류 (/route) | Haiku / low | 짧은 분류 한 번 |
| 질문 (단순 조회, 정리) | Sonnet / low | 도구 한두 번과 짧은 답. 판단할 것이 적다 |
| 가벼운 알림 조사 | Sonnet / low | 런북의 확인 조회만으로 원인이 드러나는 알림. 11-28 비교에서 Opus 와 같은 원인·조치를 냈다 |
| 그 밖의 조사 | Opus / medium | 보안 사건(공격자가 만든 글을 읽는다), 비교해 보지 않은 알림, 알림 없이 사람이 시킨 조사 |

한 스레드는 처음 고른 모델을 끝까지 쓴다. 대화에 thinking 블록이 그대로 저장돼 있어 모델을 바꾸면 이어 보낼 수 없고,
effort 만 바꿔도 그 스레드의 대화 캐시가 깨진다.
"""

from __future__ import annotations


def alert_name(alert: dict | None) -> str:
    try:
        return str(((alert or {}).get("alerts") or [{}])[0].get("labels", {}).get("alertname") or "")
    except (AttributeError, IndexError, TypeError):
        return ""


def choose(mode: str, alert: dict | None, *, heavy: tuple[str, str], light: tuple[str, str], answer: tuple[str, str],
           light_alerts: frozenset[str]) -> tuple[str, str, str]:
    """(모델, effort, 등급). 등급은 지표와 감사 기록용: answer | light | heavy."""
    if mode == "answer":
        return (*answer, "answer")
    # 알림 이름은 Alertmanager 가 준 값이지만 목록에 정확히 있는 이름만 가볍게 본다. 모르는 이름은 무거운 쪽이다.
    if alert_name(alert) in light_alerts:
        return (*light, "light")
    return (*heavy, "heavy")


def sticky(stored_model: str | None, stored_effort: str | None, has_history: bool, picked: tuple[str, str, str],
           legacy: tuple[str, str]) -> tuple[str, str, str]:
    """스레드에 이미 정해진 모델이 있으면 그것을 쓴다. 이 기능 이전에 시작된 스레드(기록은 있는데 모델이 없음)는 예전 설정을 쓴다."""
    if stored_model:
        return stored_model, stored_effort or legacy[1], "thread"
    if has_history:
        return (*legacy, "thread")
    return picked


def thread_context(values: dict | None, limit: int = 500) -> str | None:
    """스레드의 바로 앞 질문과 답을 분류기에 줄 짧은 글로. 기록이 없는 스레드(새 대화)면 None.

    분류기는 메시지 한 줄만 봤다. 그래서 스레드 안의 "왜?", "그 전 거는?" 이 앞 대화 없이 분류돼
    되묻는 말이 새 조사(보고서, 사건 기록)로 갔다 (11-29).
    """
    values = values or {}
    if not values.get("messages"):
        return None
    report = values.get("report") or {}
    last = values.get("answer") or (report.get("headline") if isinstance(report, dict) else None) or ""
    kind = "조사 보고" if report else "질문 답"
    clip = lambda t: " ".join(str(t or "").split())[:limit]  # noqa: E731
    return f"앞의 요청: {clip(values.get('question'))}\n앞의 응답 ({kind}): {clip(last)}"

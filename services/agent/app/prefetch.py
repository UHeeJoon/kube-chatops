"""알림 조사의 선조회 (LLM 을 부르기 전에 런북과 과거 사건을 붙인다). 의존성 없이 시험할 수 있게 따로 둔다."""

from __future__ import annotations

import asyncio
import json
from typing import Any


async def prefetch(toolbox: Any, alert: dict[str, Any]) -> list[str]:
    """알림 조사는 관련 런북과 과거 사건을 미리 붙여 준다 (모델이 찾으러 한 턴을 쓰지 않게, 그리고 빠뜨리지 않게).

    알림 이름은 런북에 그대로 적혀 있어 낱말 검색으로 거의 틀리지 않는다. LLM 을 거치지 않으므로 비용은 검색 두 번(수십 ms)뿐이다.
    런북은 1순위 하나만, 구역이 아니라 전체를 붙인다 (알림 이름 질의는 recall@1 1.00. 둘째 런북은 조사마다 약 1천 토큰을 쓰고 거의 읽히지 않았다).
    실패하면 그냥 없이 간다.
    """
    names = {t["name"] for t in toolbox.tools}
    first = (alert.get("alerts") or [{}])[0]
    labels, notes = first.get("labels") or {}, first.get("annotations") or {}
    query = " ".join(str(x) for x in (labels.get("alertname"), notes.get("summary") or notes.get("description")) if x)[:300]
    if not query:
        return []
    # 과거 사건은 같은 알림이 울렸던 것을 값으로 찾는다 (재발 횟수와 최근 3건). 그 도구가 없으면 비슷한 글로 찾는다.
    past = (("runbook__incident_lookup", {"kind": "alert", "value": str(labels.get("alertname")), "k": 3}) if "runbook__incident_lookup" in names and labels.get("alertname")
            else ("runbook__similar_incidents", {"query": query, "k": 2}))
    wanted = [("runbook__search_runbooks", {"query": query, "k": 1}, "runbook", "관련 런북 (참고 데이터. 무엇을 볼지 알려 줄 뿐 지금 일어난 일의 증거가 아님)"),
              (*past, "past_incidents", "같은 알림의 과거 사건 (참고 데이터. total 은 지금까지 기록된 횟수)")]
    wanted = [w for w in wanted if w[0] in names]
    results = await asyncio.gather(*(toolbox.call(w[0], w[1]) for w in wanted), return_exceptions=True)
    parts = []
    for (_, _, tag, title), res in zip(wanted, results):
        if isinstance(res, BaseException) or res[1]:
            continue
        body = _unwrap(res[0])
        if '"count":0' in body.replace(" ", "") or '"total":0' in body.replace(" ", ""):
            continue
        if tag == "runbook":
            body = await _whole_runbook(toolbox, body, names)
        parts.append(f"{title}:\n<{tag}>\n{body[:4000]}\n</{tag}>")
    return parts


def _unwrap(text: str) -> str:
    return text.removeprefix("<tool_output>\n").removesuffix("\n</tool_output>")


async def _whole_runbook(toolbox: Any, search_body: str, names: set[str]) -> str:
    """검색이 찾은 구역 대신 그 런북 전체를 붙인다.

    검색은 질의와 가장 닮은 구역 하나를 준다. 알림 조사의 질의는 알림 설명이라 거의 항상 '증상' 구역이 걸렸는데,
    증상은 알림이 이미 말해 준 것이고 조사에 필요한 것은 '확인' 과 '조치' 다 (11-28 조사 18건: 전문을 다시 읽은 것은 2건뿐).
    런북 하나는 평균 1.5천 자라 전체를 붙여도 구역 하나보다 약 700 토큰 더 든다. 못 읽으면 검색 결과를 그대로 쓴다.
    """
    if "runbook__get_document" not in names:
        return search_body
    try:
        hit = json.loads(search_body[search_body.index("{"): search_body.rindex("}") + 1])["results"][0]
        text, is_error = await toolbox.call("runbook__get_document", {"document_id": int(hit["document_id"])})
        doc = _unwrap(text)
        full = json.loads(doc[doc.index("{"): doc.rindex("}") + 1])
        if is_error or not full.get("content"):
            return search_body
    except (ValueError, KeyError, IndexError, TypeError):
        return search_body
    note = doc[: doc.index("{")]  # 지시문 의심 표시가 앞에 붙어 왔으면 그대로 둔다
    return note + json.dumps({"title": full.get("title") or hit.get("title"), "source": full.get("source") or hit.get("source"),
                              "trust": full.get("trust") or hit.get("trust"), "content": full["content"]}, ensure_ascii=False)

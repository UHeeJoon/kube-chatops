"""알림 조사의 선조회 (LLM 을 부르기 전에 런북과 과거 사건을 붙인다). 의존성 없이 시험할 수 있게 따로 둔다."""

from __future__ import annotations

import asyncio
import json
import re
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
        checks: list[tuple[str, dict]] = []
        if tag == "runbook":
            body = await _whole_runbook(toolbox, body, names)
            body, checks = split_checks(body, names)
        parts.append(f"{title}:\n<{tag}>\n{body[:4000]}\n</{tag}>")
        if checks:
            parts.append(await _run_checks(toolbox, checks))
    return parts


# 검색 서버는 문서를 구역으로 나눠 저장했다가 다시 이어 붙여 준다. 그때 코드 블록 앞에 구역 제목("## 확인")이 한 번 더 들어온다
# (11-28: 그래서 안내 문장 바로 뒤에 블록이 온다고 가정한 첫 정규식은 운영에서 한 건도 못 찾았다). 안내 문장과 되풀이된 제목은 있으면 함께 뗀다.
CHECKS_BLOCK = re.compile(r"(?:\n[^\n]*미리 실행하는 조회[^\n]*\n+)?(?:## [^\n]*\n)?```checks\n(.*?)```\n?", re.S)
MAX_CHECKS = 5
CHECK_RESULT_CHARS = 1500


def parse_checks(text: str) -> list[tuple[str, dict]]:
    """```checks 블록의 줄들 → [(도구 이름, 인자)]. 형식이 틀린 줄은 버린다."""
    out: list[tuple[str, dict]] = []
    for line in text.splitlines():
        tool, _, raw = line.strip().partition(" ")
        if not tool:
            continue
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            continue
        if isinstance(args, dict):
            out.append((tool, args))
    return out


def split_checks(body: str, names: set[str]) -> tuple[str, list[tuple[str, dict]]]:
    """런북 글에서 '미리 실행하는 조회' 블록을 떼어 낸다 → (블록을 뺀 글, 실행할 조회).

    런북은 사람이 검토해 Git 으로 들어온 글이고 서명된 이미지에서만 읽으므로 실행 목록의 출처로 쓴다 (사건 기록에서는 읽지 않는다).
    그래도 실행은 이 에이전트가 가진 조회 도구로만, MAX_CHECKS 개까지만 한다. 런북·사건 검색 도구는 여기서 부르지 않는다.
    body 는 JSON 문자열(content 안에 줄바꿈이 \\n 으로 들어 있음)이라 풀어서 찾고, 뺀 글로 다시 만든다.
    """
    try:
        start = body.index("{")
        doc = json.loads(body[start: body.rindex("}") + 1])
        content = doc["content"]
    except (ValueError, KeyError, TypeError):
        return body, []
    found = CHECKS_BLOCK.search(content)
    if not found:
        return body, []
    checks = [(t, a) for t, a in parse_checks(found.group(1)) if t in names and not t.startswith("runbook__")][:MAX_CHECKS]
    doc["content"] = content[: found.start()] + "\n" + content[found.end():]
    return body[:start] + json.dumps(doc, ensure_ascii=False), checks


async def _run_checks(toolbox: Any, checks: list[tuple[str, dict]]) -> str:
    results = await asyncio.gather(*(toolbox.call(t, a) for t, a in checks), return_exceptions=True)
    rows = []
    for (tool, args), res in zip(checks, results):
        text = f"조회 실패: {type(res).__name__}" if isinstance(res, BaseException) else _unwrap(res[0])[:CHECK_RESULT_CHARS]
        rows.append(f"[{tool} {json.dumps(args, ensure_ascii=False)}]\n{text}")
    return ("런북의 확인 항목을 방금 실행한 결과 (지금 상태의 조회 결과. 같은 조회를 다시 부르지 않습니다):\n<checks>\n"
            + "\n\n".join(rows) + "\n</checks>")


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

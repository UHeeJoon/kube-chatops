"""알림 조사의 선조회: 검색이 찾은 구역이 아니라 런북 전체를 붙인다."""

import asyncio
import json

from app.prefetch import prefetch as _prefetch

ALERT = {"alerts": [{"labels": {"alertname": "DemoHighLatency"}, "annotations": {"summary": "p95 응답 시간 증가"}}]}
SEARCH = {"count": 1, "results": [{"document_id": 4, "section": "증상", "title": "응답 지연 급증 (demo-app)", "source": "runbooks/high-latency.md",
                                   "trust": "reviewed", "text": "- p95 가 오른다"}]}
DOC = {"id": 4, "title": "응답 지연 급증 (demo-app)", "source": "runbooks/high-latency.md", "trust": "reviewed",
       "content": "## 증상\n- p95 가 오른다\n## 확인\n- by (pod) 로 나눠 본다\n## 조치\n- 한 파드만 느리면 restart"}


class FakeToolbox:
    def __init__(self, document=DOC, document_error=False, tools=("runbook__search_runbooks", "runbook__get_document", "runbook__incident_lookup")):
        self.tools = [{"name": n} for n in tools]
        self.document, self.document_error, self.calls = document, document_error, []

    async def call(self, name, args):
        self.calls.append(name)
        if name == "runbook__search_runbooks":
            return f"<tool_output>\n{json.dumps(SEARCH, ensure_ascii=False)}\n</tool_output>", False
        if name == "runbook__get_document":
            if self.document is None:
                return "<tool_output>\n입력 오류: 문서가 없습니다\n</tool_output>", False
            return f"<tool_output>\n{json.dumps(self.document, ensure_ascii=False)}\n</tool_output>", self.document_error
        return '<tool_output>\n{"total":0,"incidents":[]}\n</tool_output>', False


def run(tb):
    return asyncio.run(_prefetch(tb, ALERT))


def test_whole_runbook_is_attached():
    parts = run(FakeToolbox())
    assert len(parts) == 1  # 과거 사건은 0건이라 붙지 않는다
    assert "## 확인" in parts[0] and "## 조치" in parts[0]
    assert "응답 지연 급증 (demo-app)" in parts[0] and "runbooks/high-latency.md" in parts[0] and "reviewed" in parts[0]


def test_falls_back_to_section_when_document_fails():
    for tb in (FakeToolbox(document=None), FakeToolbox(document_error=True), FakeToolbox(tools=("runbook__search_runbooks",))):
        parts = run(tb)
        assert len(parts) == 1 and "p95 가 오른다" in parts[0] and "## 조치" not in parts[0]

"""알림 조사의 선조회: 검색이 찾은 구역이 아니라 런북 전체를 붙인다."""

import asyncio
import json

from pathlib import Path

from app.prefetch import MAX_CHECKS, parse_checks, split_checks
from app.prefetch import prefetch as _prefetch

ALERT = {"alerts": [{"labels": {"alertname": "DemoHighLatency"}, "annotations": {"summary": "p95 응답 시간 증가"}}]}
SEARCH = {"count": 1, "results": [{"document_id": 4, "section": "증상", "title": "응답 지연 급증 (demo-app)", "source": "runbooks/high-latency.md",
                                   "trust": "reviewed", "text": "- p95 가 오른다"}]}
DOC = {"id": 4, "title": "응답 지연 급증 (demo-app)", "source": "runbooks/high-latency.md", "trust": "reviewed",
       "content": "관련 알림: `DemoHighLatency`.\n\n## 증상\n- p95 가 오른다\n## 확인\n- by (pod) 로 나눠 본다\n## 조치\n- 한 파드만 느리면 restart"}


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


CHECKS_DOC = {**DOC, "content": DOC["content"].replace("## 조치", "에이전트가 조사를 시작할 때 미리 실행하는 조회 (도구 이름과 인자, 한 줄에 하나):\n\n```checks\n"
                                                      'prometheus__query_instant {"query": "up{namespace=\\"demo\\"}"}\nk8s__get_pod_status {"namespace": "demo"}\n'
                                                      'runbook__get_document {"document_id": 1}\nk8s__delete_pod {"name": "x"}\n깨진 줄 {{{\n```\n\n## 조치')}
TOOLS = ("runbook__search_runbooks", "runbook__get_document", "runbook__incident_lookup", "prometheus__query_instant", "k8s__get_pod_status")


def test_runbook_checks_are_run_and_attached():
    tb = FakeToolbox(document=CHECKS_DOC, tools=TOOLS)
    parts = run(tb)
    assert len(parts) == 2 and parts[1].startswith("런북의 확인 항목을 방금 실행한 결과")
    # 가진 조회 도구만 실행한다: 런북 도구, 없는 도구, 깨진 줄은 건너뛴다
    assert sorted(c for c in tb.calls if not c.startswith("runbook__")) == ["k8s__get_pod_status", "prometheus__query_instant"]
    assert tb.calls.count("runbook__get_document") == 1
    assert "```checks" not in parts[0] and "## 조치" in parts[0] and "## 확인" in parts[0]


def test_checks_are_capped():
    body = json.dumps({"content": "미리 실행하는 조회:\n\n```checks\n" + "t {}\n" * 9 + "```\n"})
    assert len(split_checks(body, {"t"})[1]) == MAX_CHECKS


def test_every_runbook_checks_block_is_well_formed():
    known = {"prometheus__query_instant", "prometheus__query_range", "prometheus__list_firing_alerts", "k8s__get_pod_status", "k8s__get_events",
             "k8s__describe_deployment", "k8s__rollout_history", "loki__search_logs", "loki__error_summary", "loki__security_events", "loki__waf_blocks",
             "anomaly__detect_anomalies"}
    runbooks = Path(__file__).resolve().parents[2] / "mcp-runbook" / "runbooks"
    with_checks = 0
    for path in sorted(runbooks.glob("*.md")):
        text = path.read_text()
        if "```checks" not in text:
            continue
        with_checks += 1
        body, checks = split_checks(json.dumps({"content": text}), known)
        lines = [ln for ln in text.split("```checks\n")[1].split("```")[0].splitlines() if ln.strip()]
        assert len(checks) == len(lines) <= MAX_CHECKS, path.name  # 모든 줄이 아는 도구이고 JSON 인자다
        assert "```checks" not in body and len(parse_checks("\n".join(lines))) == len(lines)
    assert with_checks >= 5


def test_checks_are_found_in_the_shape_the_search_server_returns():
    # 운영의 get_document 응답: 안내 문장과 블록 사이에 구역 제목이 되풀이된다
    content = ("## 확인\n- p95 를 본다\n\n에이전트가 조사를 시작할 때 미리 실행하는 조회 (도구 이름과 인자, 한 줄에 하나):\n\n## 확인\n"
               '```checks\nprometheus__query_instant {"query": "up"}\n```\n\n## 원인 후보\n- CPU 부족')
    body, checks = split_checks(json.dumps({"content": content}, ensure_ascii=False), {"prometheus__query_instant"})
    assert checks == [("prometheus__query_instant", {"query": "up"})]
    left = json.loads(body)["content"]
    assert "checks" not in left and "미리 실행" not in left and left.count("## 확인") == 1 and "## 원인 후보" in left


def test_runbook_that_does_not_name_the_alert_is_attached_without_running_its_checks():
    """검색은 맞는 런북이 없어도 가장 가까운 것을 준다. 이 알림을 적어 두지 않은 런북의 확인 조회는 실행하지 않는다."""
    other = {**CHECKS_DOC, "content": CHECKS_DOC["content"].replace("DemoHighLatency", "DemoSomethingElse")}
    tb = FakeToolbox(document=other, tools=TOOLS)
    parts = run(tb)
    assert len(parts) == 1 and parts[0].startswith("가장 비슷한 런북 (이 알림을 맡은 런북은 없음")
    assert [c for c in tb.calls if not c.startswith("runbook__")] == []
    # 사람이 검토한 런북이 아니면(사건 기록) 알림 이름이 있어도 실행하지 않는다
    tb = FakeToolbox(document={**CHECKS_DOC, "trust": "generated", "source": "incident/3/report/1"}, tools=TOOLS)
    assert len(run(tb)) == 1 and [c for c in tb.calls if not c.startswith("runbook__")] == []

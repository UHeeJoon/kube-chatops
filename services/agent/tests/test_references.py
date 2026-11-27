"""보고서 인용 검증 (safety.verify_references). 의존성 없이 돈다."""

from app.safety import UNVERIFIED, seen_sources, verify_references

PREFETCH = ('요청 종류: 조사\n\n관련 런북:\n<runbook>\n{"count":2,"results":[{"document_id":3,"title":"웹 공격 차단과 요청 수 제한 (WAF)",'
            '"source":"runbooks/waf-attack.md","section":"확인"},{"document_id":9,"title":"메모리 누수와 OOM 재시작 (demo-app)","source":"runbooks/memory-leak-oom.md"}]}\n</runbook>\n\n'
            '같은 알림의 과거 사건:\n<past_incidents>\n{"total":1,"incidents":[{"title":"사건 #2: demo-app p95 2.2s","source":"incident/2/report/1"}]}\n</past_incidents>')
TOOL = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
        "content": '<tool_output>\n{"results": [{"title": "잘못된 배포 (CrashLoop, ImagePull)", "source": "runbooks/bad-deploy.md"}, {"title": "사건 #7: 메모리 급증", "source": "incident/7/report/1"}]}\n</tool_output>'}]}
MESSAGES = [{"role": "user", "content": PREFETCH},
            {"role": "assistant", "content": [{"type": "text", "text": '"title":"지어낸 런북" runbooks/fake.md 사건 #99'}]},  # 모델이 쓴 글은 근거가 아니다
            TOOL]


def test_seen_sources():
    seen = seen_sources(MESSAGES)
    assert seen["files"] == {"waf-attack.md", "memory-leak-oom.md", "bad-deploy.md"}
    assert seen["incidents"] == {"2", "7"}
    assert len(seen["titles"]) == 3


def test_real_references_pass():
    refs = ["런북: 웹 공격 차단과 요청 수 제한 (WAF) (runbooks/waf-attack.md)", "런북: 메모리 누수와 OOM 재시작 (demo-app)",
            "사건 #2 (원인 다름: 그때는 leak 주입 OOM)", "사건 #7", "runbooks/bad-deploy.md", "런북: 잘못된 배포(CrashLoop, ImagePull)"]
    out, flagged = verify_references(refs, MESSAGES)
    assert (out, flagged) == (refs, 0)


def test_invented_references_are_marked():
    refs = ["런북: 디스크 가득 참 대응", "사건 #99", "사건 #2, 사건 #5", "런북: 지어낸 런북", "runbooks/fake.md"]
    out, flagged = verify_references(refs, MESSAGES)
    assert flagged == 5 and all(r.endswith(UNVERIFIED) for r in out)


def test_edge_cases():
    assert verify_references([], MESSAGES) == ([], 0)
    assert verify_references(None, MESSAGES) == (None, 0)
    assert verify_references(["", 3, "사건 #2"], MESSAGES) == (["사건 #2"], 0)
    assert verify_references(["사건 #2"], []) == (["사건 #2" + UNVERIFIED], 1)

from app.models import alert_name, choose, sticky

KW = dict(heavy=("opus", "medium"), light=("sonnet", "low"), answer=("sonnet", "low"),
          light_alerts=frozenset({"DemoHighLatency", "DemoHighErrorRate", "DemoSinglePodAnomaly"}))


def alert(name):
    return {"alerts": [{"labels": {"alertname": name}}]}


def test_questions_use_the_answer_model():
    assert choose("answer", None, **KW) == ("sonnet", "low", "answer")
    assert choose("answer", alert("DemoWebshellBlocked"), **KW)[2] == "answer"


def test_only_listed_alerts_are_light():
    assert choose("investigate", alert("DemoHighLatency"), **KW) == ("sonnet", "low", "light")
    for name in ("DemoWebshellBlocked", "DemoWafAttackBlocked", "SomethingNew", "DemoHighLatencyX", ""):
        assert choose("investigate", alert(name), **KW) == ("opus", "medium", "heavy")


def test_investigation_without_an_alert_or_with_a_broken_one_is_heavy():
    for bad in (None, {}, {"alerts": []}, {"alerts": ["x"]}, {"alerts": [{"labels": None}]}):
        assert alert_name(bad) == ""
        assert choose("investigate", bad, **KW)[2] == "heavy"


def test_a_thread_keeps_the_model_it_started_with():
    picked = ("sonnet", "low", "answer")
    assert sticky(None, None, False, picked, ("opus", "medium")) == picked
    assert sticky("opus", "medium", True, picked, ("opus", "medium")) == ("opus", "medium", "thread")
    # 이 기능 이전에 시작된 스레드: 저장된 모델이 없지만 대화 기록이 있다 → 예전 설정
    assert sticky(None, None, True, picked, ("opus", "medium")) == ("opus", "medium", "thread")


def test_thread_context_is_none_for_a_new_thread():
    from app.models import thread_context

    assert thread_context({}) is None
    assert thread_context({"question": "x", "messages": []}) is None


def test_thread_context_carries_the_last_question_and_answer():
    from app.models import thread_context

    got = thread_context({"messages": [{}], "question": "파드 뭐 있어?", "answer": "7개\n모두 Running", "report": None})
    assert "앞의 요청: 파드 뭐 있어?" in got and "질문 답" in got and "7개 모두 Running" in got
    got = thread_context({"messages": [{}], "question": "왜 느려?", "answer": None, "report": {"headline": "DB 지연"}})
    assert "조사 보고" in got and "DB 지연" in got
    assert len(thread_context({"messages": [{}], "question": "q", "answer": "가" * 5000}).split("\n")[1]) < 560

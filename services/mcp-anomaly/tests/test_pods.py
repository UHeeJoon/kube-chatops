"""파드별 채점의 행 만들기와 요약 (모델 없이)."""

from app.pods import POD_QUERIES, pod_findings, pod_rows

SERIES = {
    "rps": {"a": 2.5, "b": 2.4, "quarantined": None, "idle": 0.0},
    "error_ratio": {"a": 0.0, "b": 0.16},
    "p95_seconds": {"a": 0.0095, "b": 0.0096},
    "mem_ratio": {"a": 0.138, "b": 0.139, "quarantined": 0.02},
    "mem_growth": {"a": 0.0001},
}
OK = {"is_anomaly": False, "top_feature": "rps", "top_z": 0.3}


def test_rows_cover_serving_pods_and_scale_rps_to_the_service_total():
    rows = pod_rows(SERIES)
    assert [p for p, _ in rows] == ["a", "b"]
    assert rows[0][1]["rps"] == 5.0 and rows[1][1]["rps"] == 4.8
    assert set(rows[0][1]) == set(POD_QUERIES)
    assert rows[1][1]["mem_growth"] is None  # 값이 없는 특성은 비워 보낸다 (모델이 평소 값으로 본다)


def test_only_the_bad_pod_is_reported_with_its_raw_value():
    rows = pod_rows(SERIES)
    out = pod_findings(rows, [OK, {"is_anomaly": True, "zscore_anomaly": True, "top_feature": "error_ratio", "top_z": 16.0}])
    assert out["checked"] == 2
    assert out["anomalous"] == [{"pod": "b", "main_signal": "error_ratio", "z": 16.0, "value": 0.16}]
    assert "일부 파드만" in out["note"]


def test_all_pods_bad_is_not_called_a_single_pod_problem():
    rows = pod_rows(SERIES)
    bad = {"is_anomaly": True, "zscore_anomaly": True, "top_feature": "p95_log_ms", "top_z": 50.0}
    out = pod_findings(rows, [bad, bad])
    assert len(out["anomalous"]) == 2 and "note" not in out
    assert out["anomalous"][0]["value"] == 0.0095  # p95_log_ms 는 원래 값(초)으로 보여 준다


def test_traffic_drop_alone_is_not_a_pod_finding_and_single_pod_is_skipped():
    rows = pod_rows(SERIES)
    drop = {"is_anomaly": True, "traffic_drop": True, "zscore_anomaly": False, "iforest_anomaly": False, "top_feature": "rps", "top_z": 1.0}
    assert pod_findings(rows, [drop, OK])["anomalous"] == []
    assert pod_findings(rows[:1], [OK]) is None


def test_unconfirmed_iforest_does_not_turn_a_traffic_drop_into_a_pod_finding():
    """요청이 한쪽으로 쏠려 급감으로 보이는 파드: 확정 전 iforest 가 켜져 있어도 어느 특성도 2 이상 벗어나지 않았으면 건너뛴다."""
    rows = pod_rows(SERIES)
    weak = {"is_anomaly": True, "traffic_drop": True, "zscore_anomaly": False, "iforest_anomaly": True, "top_feature": "rps", "top_z": 1.3}
    assert pod_findings(rows, [OK, weak])["anomalous"] == []
    strong = {**weak, "top_feature": "error_ratio", "top_z": 2.6}
    assert [b["pod"] for b in pod_findings(rows, [OK, strong])["anomalous"]] == ["b"]

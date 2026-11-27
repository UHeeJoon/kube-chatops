"""오프라인 테스트: 합성 데이터로 학습·점수 계산·분할 규칙을 확인한다 (Prometheus, MLflow 불필요)."""

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from anomaly import steps
from anomaly.detector_model import FEATURES, RAW_COLUMNS, score_frame, to_features


def synthetic(minutes: int = 600, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-11-25", periods=minutes, freq="min", tz="UTC")
    df = pd.DataFrame(
        {
            "ts": ts,
            "rps": 5 + rng.normal(0, 0.5, minutes),
            "error_ratio": np.zeros(minutes),  # 평소 오류 0 (실제 demo 와 같음)
            "p95_seconds": 0.0095 + rng.normal(0, 0.0003, minutes),
            "mem_ratio": 0.138 + rng.normal(0, 0.001, minutes),
            "mem_growth": np.abs(rng.normal(0, 0.0002, minutes)),
            "label": 0,
            "kind": "",
            "ambiguous": False,
        }
    )
    # 뒤쪽에 지연 장애 20분, 오류 장애 20분
    df.loc[500:519, ["p95_seconds", "label", "kind"]] = [0.8, 1, "latency"]
    df.loc[550:569, ["error_ratio", "label", "kind"]] = [0.3, 1, "error"]
    return df


def test_split_keeps_anomalies_out_of_training():
    train, test = steps.split(synthetic(), 0.7)
    assert train["label"].sum() == 0
    assert test["label"].sum() == 40
    assert train["ts"].max() < test.loc[test["label"] == 0, "ts"].min()


def test_split_drops_ambiguous_minutes():
    df = synthetic()
    df.loc[500:502, "ambiguous"] = True
    train, test = steps.split(df, 0.7)
    assert test["label"].sum() == 37


def test_error_spike_detected_even_when_error_was_constant_in_training():
    """학습 기간에 오류율이 늘 0 이면 IsolationForest 는 오류율을 못 본다 → z-score 가 잡아야 한다."""
    train, test = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    scored = score_frame(bundle, test[RAW_COLUMNS])
    errors = (test["kind"] == "error").to_numpy()
    assert scored.loc[errors, "is_anomaly"].all()
    assert (scored.loc[errors, "top_feature"] == "error_ratio").all()
    latency = (test["kind"] == "latency").to_numpy()
    assert scored.loc[latency, "is_anomaly"].all()


def test_few_false_alarms_on_normal_minutes():
    train, test = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    scored = score_frame(bundle, test[RAW_COLUMNS])
    normal = (test["label"] == 0).to_numpy()
    assert scored.loc[normal, "is_anomaly"].mean() < 0.05


def test_missing_values_do_not_crash():
    train, _ = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    raw = pd.DataFrame([{c: None for c in RAW_COLUMNS}])
    out = score_frame(bundle, raw)
    assert len(out) == 1 and out["score"].notna().all()


def test_missing_measurement_is_not_an_anomaly():
    """메모리·지연 수집이 비면 평소 값으로 본다 (0 으로 채우면 '메모리 0%' 이상으로 잡힌다)."""
    train, _ = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    raw = pd.DataFrame([{**{c: None for c in RAW_COLUMNS}, "rps": 5.0}])
    assert not score_frame(bundle, raw)["is_anomaly"].iloc[0]


def test_per_feature_z_explains_the_decision():
    """대시보드용 특성별 z: 지연 장애에서는 p95 z 가 크게 양수, 나머지는 작다."""
    train, test = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    scored = score_frame(bundle, test[RAW_COLUMNS])
    latency = (test["kind"] == "latency").to_numpy()
    assert (scored.loc[latency, "z_p95_log_ms"] > 4).all()
    assert (scored.loc[latency, "z_error_ratio"].abs() < 4).all()


def test_parse_failures_handles_argo_formats():
    """Argo 는 실패가 없으면 '"null"', 있으면 JSON 배열(문자열로 한 번 더 감싸일 수 있음)을 넘긴다."""
    import json

    assert steps.parse_failures('"null"') == []
    assert steps.parse_failures("null") == []
    assert steps.parse_failures("") == []
    one = [{"displayName": "train", "message": "OOMKilled"}]
    assert steps.parse_failures(json.dumps(one)) == [{"step": "train", "message": "OOMKilled"}]
    assert steps.parse_failures(json.dumps(json.dumps(one))) == [{"step": "train", "message": "OOMKilled"}]
    assert steps.parse_failures("not json") == [{"message": "not json"}]


def test_memory_growth_catches_leak_before_memory_is_high():
    """누수 초반: 메모리는 아직 평소 범위(+2%p)지만 분당 3% 씩 늘고 있으면 이상 (11-26 실측 누수 3~8%/분)."""
    train, _ = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    raw = pd.DataFrame([{"rps": 5.0, "error_ratio": 0.0, "p95_seconds": 0.0095, "mem_ratio": 0.158, "mem_growth": 0.03}])
    out = score_frame(bundle, raw).iloc[0]
    assert out["is_anomaly"] and out["top_feature"] == "mem_growth"
    # 메모리 해제(음수 증가)는 이상이 아니다
    raw.loc[0, ["mem_ratio", "mem_growth"]] = [0.138, -0.05]
    assert not score_frame(bundle, raw)["is_anomaly"].iloc[0]


def test_traffic_drop_detected_although_z_lower_bound_is_negative():
    """요청 수 표준편차가 커서 z 로는 0 도 못 잡는다 → rps_low 규칙이 잡는다."""
    df = synthetic()
    df["rps"] = 5 * (1 + 0.5 * np.sin(np.arange(len(df)) / 60))  # 2.5~7.5 로 크게 오르내림
    train, _ = steps.split(df, 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    raw = pd.DataFrame([{"rps": 0.1, "error_ratio": 0.0, "p95_seconds": 0.0095, "mem_ratio": 0.138, "mem_growth": 0.0}])
    out = score_frame(bundle, raw).iloc[0]
    assert out["traffic_drop"] and out["is_anomaly"] and out["top_feature"] == "rps"
    raw.loc[0, "rps"] = 3.0  # 평소 낮은 시간대는 정상
    assert not score_frame(bundle, raw)["is_anomaly"].iloc[0]


def _bundle_where_iforest_fires_on(rows: list[bool], z_limit: float = 4.0) -> tuple[dict, pd.DataFrame]:
    """IsolationForest 만 원하는 행에서 이상으로 답하게 만든 가짜 bundle (z-score·요청 급감은 조용함)."""

    class FakeForest:
        def score_samples(self, z):
            return -np.array([1.0 if r else 0.0 for r in rows])

    raw = pd.DataFrame({"rps": 10.0, "error_ratio": 0.0, "p95_seconds": 0.05, "mem_ratio": 0.3, "mem_growth": 0.0},
                       index=range(len(rows)))
    scaler = StandardScaler().fit(to_features(raw).to_numpy())
    scaler.scale_ = np.ones(len(FEATURES))
    bundle = {"scaler": scaler, "forest": FakeForest(), "threshold": 0.5, "z_limit": z_limit, "rps_low": 1.0,
              "features": list(FEATURES), "iforest_needs_neighbor": True}
    return bundle, raw


def test_isolated_iforest_minute_is_not_an_anomaly():
    # 11-27: 정상 구간 오탐이 전부 IsolationForest 단독의 1분짜리였다
    bundle, raw = _bundle_where_iforest_fires_on([False, True, False, False, True, True, False])
    scored = score_frame(bundle, raw)
    assert scored["iforest_anomaly"].tolist() == [False, True, False, False, True, True, False]  # 원래 판정은 그대로 보인다
    assert scored["is_anomaly"].tolist() == [False, False, False, False, True, True, False]


def test_last_minute_is_confirmed_by_the_previous_one_only():
    # 실시간: 마지막 분은 뒤가 없다. 앞 1분이 이상이면 확정, 아니면 다음 분까지 기다린다
    bundle, raw = _bundle_where_iforest_fires_on([False, False, True])
    assert not score_frame(bundle, raw)["is_anomaly"].iloc[-1]
    bundle, raw = _bundle_where_iforest_fires_on([False, True, True])
    assert score_frame(bundle, raw)["is_anomaly"].iloc[-1]


def test_minutes_across_a_gap_are_not_neighbors():
    # 평가 데이터는 애매 구간이 빠져 있다. 10분 떨어진 두 행을 이웃으로 보면 안 된다
    bundle, raw = _bundle_where_iforest_fires_on([True, True])
    assert score_frame(bundle, raw, minute=np.array([100, 101]))["is_anomaly"].all()
    assert not score_frame(bundle, raw, minute=np.array([100, 110]))["is_anomaly"].any()


def test_strong_signals_are_still_immediate():
    # z-score 로 잡히는 급등은 1분짜리라도 바로 이상이다
    bundle, raw = _bundle_where_iforest_fires_on([False, False, False], z_limit=0.5)  # 가짜 scaler 는 z = 원값 차이
    raw.loc[1, "error_ratio"] = 0.9
    assert score_frame(bundle, raw)["is_anomaly"].tolist() == [False, True, False]


def test_old_bundle_without_the_flag_keeps_old_behaviour():
    # champion(이전 모델)을 새 코드로 평가할 때 판정이 바뀌면 비교가 틀어진다
    bundle, raw = _bundle_where_iforest_fires_on([False, True, False])
    del bundle["iforest_needs_neighbor"]
    assert score_frame(bundle, raw)["is_anomaly"].tolist() == [False, True, False]


def test_iforest_alone_needs_a_feature_that_moved():
    # 11-27: 오탐이 전부 IsolationForest 단독 + 최대 z 1.4 이하였다. z 가 뒷받침할 때만 인정한다
    bundle, raw = _bundle_where_iforest_fires_on([True, True, True], z_limit=4.0)
    del bundle["iforest_needs_neighbor"]
    bundle["iforest_min_z"] = 2.0
    raw.loc[1, "mem_ratio"] = 0.3 + 2.5  # 가짜 scaler 는 z = 원값 차이 → z 2.5 (한계 4 에는 못 미침)
    scored = score_frame(bundle, raw)
    assert scored["iforest_anomaly"].all()
    assert scored["is_anomaly"].tolist() == [False, True, False]


def test_new_models_require_support_for_iforest():
    train, _ = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    assert bundle["iforest_min_z"] == 2.0 and "iforest_needs_neighbor" not in bundle


def test_cv_false_alarms_uses_all_normal_minutes():
    df = synthetic()
    normal = df[(df["label"] == 0) & ~df["ambiguous"]]
    out = steps.cv_false_alarms(normal, quantile=0.999, z_limit=4.0)
    assert out["cv_normal_days"] == round(len(normal) / 1440, 2)
    assert out["cv_false_alarm_minutes"] <= len(normal) * 0.05


def test_false_alarms_report_sample_size_and_events():
    y = pd.Series([0, 0, 0, 0, 1, 0])
    pred = pd.Series([True, True, False, True, True, True])
    out = steps.false_alarms(y, pred, minute=np.array([1, 2, 3, 4, 5, 20]))
    assert out["false_alarm_minutes"] == 4
    assert out["false_alarm_events"] == 3  # 1~2분은 한 건, 4분, 20분 (5분은 장애라 오탐 아님, 20분은 떨어져 있음)
    assert out["normal_days"] == round(5 / 1440, 2)


def test_older_model_with_fewer_features_still_scores():
    """evaluate 단계는 새 코드로 이전 챔피언(v5, 특성 4개)도 점수 낸다 (11-27 v6 학습에서 겪음: X has 5 features)."""
    train, test = steps.split(synthetic(), 0.7)
    four = to_features(train[RAW_COLUMNS])[FEATURES[:4]]
    scaler = StandardScaler().fit(four)
    v5 = {**steps.fit_bundle(train, quantile=0.999, z_limit=4.0), "scaler": scaler,
          "forest": IsolationForest(random_state=0).fit(scaler.transform(four))}
    del v5["features"]  # v5 번들에는 특성 목록이 없다
    out = score_frame(v5, test[RAW_COLUMNS])
    assert len(out) == len(test) and "z_mem_growth" not in out.columns


def test_unlabeled_incident_minutes_are_not_used():
    df = synthetic()
    df["unlabeled_incident"] = False
    # 주입 표시 없이 메모리가 치솟은 20분 (실제 장애). 정상으로 두면 평소 값이 오염된다
    df.loc[100:119, "mem_ratio"] = 0.9
    df.loc[100:119, "unlabeled_incident"] = True
    train, test = steps.split(df, 0.7)
    assert train["mem_ratio"].max() < 0.2 and test["mem_ratio"].max() < 0.2
    assert len(steps.usable(df)) == len(df) - 20


def test_usable_accepts_datasets_without_the_column():
    df = synthetic()
    df.loc[5:7, "ambiguous"] = True
    assert len(steps.usable(df)) == len(df) - 3


def test_widen_marks_neighbouring_minutes():
    from anomaly.data import widen

    flags = pd.Series([False] * 10)
    flags[5] = True
    assert widen(flags, 2).tolist() == [False, False, False, True, True, True, True, True, False, False]


def test_episodes_count_incidents_and_delay():
    df = synthetic()
    _, test = steps.split(df, 0.7)
    minute = steps.minute_numbers(test)
    pred = pd.Series(False, index=test.index)
    latency = test.index[test["kind"] == "latency"]
    pred[latency[4:]] = True  # 지연 장애는 4분 늦게 잡고, 오류 장애는 놓친다
    out = steps.episodes(test, pred, minute)
    assert out["episodes"] == 2 and out["episodes_detected"] == 1 and out["episodes_missed_kinds"] == "error"
    assert out["detect_late_minutes_median"] == 4.0
    scored = score_frame(steps.fit_bundle(steps.split(df, 0.7)[0], 0.999, 4.0), test[RAW_COLUMNS], minute)
    real = steps.episodes(test, scored["is_anomaly"], minute)
    assert real["episodes_detected"] == 2 and real["detect_late_minutes_max"] == 0.0

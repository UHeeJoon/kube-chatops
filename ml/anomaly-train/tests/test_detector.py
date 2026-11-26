"""오프라인 테스트: 합성 데이터로 학습·점수 계산·분할 규칙을 확인한다 (Prometheus, MLflow 불필요)."""

import numpy as np
import pandas as pd

from anomaly import steps
from anomaly.detector_model import RAW_COLUMNS, score_frame


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
    raw = pd.DataFrame([{"rps": None, "error_ratio": None, "p95_seconds": None, "mem_ratio": None}])
    out = score_frame(bundle, raw)
    assert len(out) == 1 and out["score"].notna().all()


def test_missing_measurement_is_not_an_anomaly():
    """메모리·지연 수집이 비면 평소 값으로 본다 (0 으로 채우면 '메모리 0%' 이상으로 잡힌다)."""
    train, _ = steps.split(synthetic(), 0.7)
    bundle = steps.fit_bundle(train, quantile=0.999, z_limit=4.0)
    raw = pd.DataFrame([{"rps": 5.0, "error_ratio": None, "p95_seconds": None, "mem_ratio": None}])
    assert not score_frame(bundle, raw)["is_anomaly"].iloc[0]

"""서빙 계약 시험: 학습 코드가 만든 모델을 서빙 코드가 읽고, mcp-anomaly 가 보내는 모양의 요청에 답하는지.

세 조각(학습, 서빙, 탐지기)이 따로 배포된다. 11-27 에 모델만 새 특성(mem_growth)을 쓰게 바뀌고 서빙이 입력 열을
4개로 고정해 둔 탓에, 승격 직후 모든 예측이 500 이었다. 조각마다의 단위 시험은 통과했다. 이 시험은 셋을 이어서 돌린다.
"""

import importlib.util
import sys
from pathlib import Path

import joblib
import mlflow
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ml" / "anomaly-train"))
sys.path.insert(0, str(ROOT / "ml" / "anomaly-serve"))

from anomaly import steps  # noqa: E402
from anomaly.detector_model import RAW_COLUMNS  # noqa: E402
from server import AnomalyModel  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FEATURE_QUERIES = _load(ROOT / "services" / "mcp-anomaly" / "app" / "features.py", "mcp_features").FEATURE_QUERIES
# mcp-anomaly 가 응답에서 읽는 필드 (services/mcp-anomaly/app/main.py 의 segments, detect_anomalies, monitor_once)
READ_BY_MCP = ["is_anomaly", "iforest_anomaly", "zscore_anomaly", "traffic_drop", "top_feature", "top_z", "score"]


@pytest.fixture(scope="module")
def served(tmp_path_factory) -> AnomalyModel:
    rng = np.random.default_rng(0)
    n = 600
    train = pd.DataFrame({"rps": 5 + rng.normal(0, 0.5, n), "error_ratio": np.zeros(n), "p95_seconds": 0.0095 + rng.normal(0, 0.0003, n),
                          "mem_ratio": 0.138 + rng.normal(0, 0.001, n), "mem_growth": np.abs(rng.normal(0, 0.0002, n))})
    tmp = tmp_path_factory.mktemp("model")
    joblib.dump(steps.fit_bundle(train, 0.999, 4.0), tmp / "detector.joblib")
    # 학습 단계(steps.train)와 같은 방식으로 저장한다: 모델 코드 파일 + bundle + 입력 예시(→ 입력 스키마)
    mlflow.pyfunc.save_model(path=str(tmp / "served"), python_model=steps.MODEL_CODE, artifacts={"detector": str(tmp / "detector.joblib")},
                             input_example=train[RAW_COLUMNS].head(3), pip_requirements=steps._pip_requirements())
    model = AnomalyModel("demo-anomaly", str(tmp / "served"))
    model.load()
    return model


def row(**over):
    base = {"rps": 5.0, "error_ratio": 0.0, "p95_seconds": 0.0095, "mem_ratio": 0.138, "mem_growth": 0.0001}
    return {k: {**base, **over}.get(k) for k in FEATURE_QUERIES}


def test_serving_asks_for_every_feature_the_model_was_trained_on(served):
    assert set(served.columns) == set(RAW_COLUMNS) == set(FEATURE_QUERIES)


def test_request_shaped_like_mcp_anomaly_is_answered(served):
    # 탐지기는 Prometheus 에 값이 없는 분을 null 로 보낸다 (5xx 가 없으면 error_ratio 가 비고, 재시작 직후에는 mem_growth 가 빈다)
    rows = [row(), row(error_ratio=None, mem_growth=None), row(p95_seconds=0.8), row(error_ratio=0.3), row(rps=0.0)]
    out = served.predict({"instances": rows})
    preds = out["predictions"]
    assert len(preds) == len(rows) and out["model_version"]
    for p in preds:
        assert all(k in p for k in READ_BY_MCP)
        assert any(k.startswith("z_") for k in p)
    assert [p["is_anomaly"] for p in preds] == [False, False, True, True, True]
    assert preds[2]["top_feature"] == "p95_log_ms" and preds[3]["top_feature"] == "error_ratio" and preds[4]["traffic_drop"]


def test_integer_values_and_json_bytes_are_accepted(served):
    out = served.predict(b'{"instances": [{"rps": 5, "error_ratio": 0, "p95_seconds": 0.0095, "mem_ratio": 0.138}]}')
    assert out["predictions"][0]["is_anomaly"] is False


def test_bad_requests_are_rejected(served):
    for bad in ({}, {"instances": []}, {"instances": [{"rps": 1}]}, {"instances": [row()] * 1441}):
        with pytest.raises(ValueError):
            served.predict(bad)

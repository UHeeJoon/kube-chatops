"""demo 서비스 이상 탐지 모델 (MLflow pyfunc, models-from-code).

이 파일은 모델과 함께 MLflow 에 저장되고, 서빙(KServe)에서도 이 파일 그대로 불려 온다.
특성 계산을 모델 안에 두어 학습과 서빙이 항상 같은 계산을 쓴다.

입력: 1분 단위 행. 열은 RAW_COLUMNS (Prometheus 에서 읽은 값 그대로).
출력: 행마다 이상 점수(클수록 이상), 이상 여부, 가장 크게 벗어난 특성과 그 z-score.
"""

from __future__ import annotations

import joblib
import mlflow
import numpy as np
import pandas as pd
from mlflow.pyfunc import PythonModel

RAW_COLUMNS = ["rps", "error_ratio", "p95_seconds", "mem_ratio"]
FEATURES = ["rps", "error_ratio", "p95_log_ms", "mem_ratio"]


def to_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Prometheus 원값 → 모델 특성.

    빈 값의 뜻이 특성마다 다르다.
    - rps, error_ratio: 비면 '요청 없음', '5xx 없음' (5xx 시계열이 아예 없으면 나눗셈 결과가 빈다) → 0
    - p95, mem_ratio: 비면 '측정 못 함' (수집 중단) → 비워 두고, 점수 계산 때 평소 값(z=0)으로 본다.
      0 으로 채우면 '메모리 0%' 로 읽혀 수집이 끊긴 순간이 이상으로 잡힌다 (11-26 실제로 겪음).
    """
    num = {c: pd.to_numeric(raw[c], errors="coerce") for c in RAW_COLUMNS}
    return pd.DataFrame(
        {
            "rps": num["rps"].fillna(0.0).clip(lower=0),
            "error_ratio": num["error_ratio"].fillna(0.0).clip(0, 1),
            # 지연은 꼬리가 길어 로그로 줄인다 (10ms → 2.4, 800ms → 6.7)
            "p95_log_ms": np.log1p(num["p95_seconds"].clip(lower=0) * 1000),
            "mem_ratio": num["mem_ratio"].clip(lower=0),
        },
        index=raw.index,
    )[FEATURES]


def standardize(scaler, x: pd.DataFrame) -> np.ndarray:
    """z-score. 측정 못 한 값(NaN)은 평소 값(z=0)으로 본다."""
    return np.nan_to_num(scaler.transform(x.to_numpy()), nan=0.0)


def score_frame(bundle: dict, raw: pd.DataFrame) -> pd.DataFrame:
    """bundle(scaler, forest, threshold, z_limit) 로 점수를 낸다. 학습 평가와 서빙이 같이 쓴다."""
    z = standardize(bundle["scaler"], to_features(raw))
    score = -bundle["forest"].score_samples(z)  # IsolationForest: 낮을수록 이상 → 부호를 뒤집는다
    abs_z = np.abs(z)
    top = abs_z.argmax(axis=1)
    iforest = score >= bundle["threshold"]
    zscore = (abs_z >= bundle["z_limit"]).any(axis=1)
    return pd.DataFrame(
        {
            "score": score.round(4),
            # 혼합 규칙. IsolationForest 는 학습 기간에 한 번도 변하지 않은 특성(예: 장애 없던 기간의 오류율)으로는
            # 나무를 나누지 못해 그 특성의 급등을 못 본다. 그 빈틈을 특성별 z-score 가 메운다.
            "is_anomaly": iforest | zscore,
            "iforest_anomaly": iforest,
            "zscore_anomaly": zscore,
            "top_feature": [FEATURES[i] for i in top],
            "top_z": abs_z.max(axis=1).round(2),
        },
        index=raw.index,
    )


class Detector(PythonModel):
    def load_context(self, context):
        self.bundle = joblib.load(context.artifacts["detector"])

    def predict(self, context, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
        return score_frame(self.bundle, model_input)


mlflow.models.set_model(Detector())

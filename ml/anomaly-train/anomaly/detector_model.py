"""demo 서비스 이상 탐지 모델 (MLflow pyfunc, models-from-code).

이 파일은 모델과 함께 MLflow 에 저장되고, 서빙(KServe)에서도 이 파일 그대로 불려 온다.
특성 계산을 모델 안에 두어 학습과 서빙이 항상 같은 계산을 쓴다.

입력: 1분 단위 행. 열은 RAW_COLUMNS (Prometheus 에서 읽은 값 그대로).
출력: 행마다 이상 점수(클수록 이상), 이상 여부, 가장 크게 벗어난 특성과 그 z-score, 특성별 z-score(z_<특성>, 부호 있음),
      요청 급감 여부(traffic_drop).

행은 시간 순서의 연속된 분으로 넣는다 (v7 은 IsolationForest 단독 판정에 이웃한 분을 본다).
"""

from __future__ import annotations

import joblib
import mlflow
import numpy as np
import pandas as pd
from mlflow.pyfunc import PythonModel

RAW_COLUMNS = ["rps", "error_ratio", "p95_seconds", "mem_ratio", "mem_growth"]
FEATURES = ["rps", "error_ratio", "p95_log_ms", "mem_ratio", "mem_growth"]


def to_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Prometheus 원값 → 모델 특성.

    빈 값의 뜻이 특성마다 다르다.
    - rps, error_ratio: 비면 '요청 없음', '5xx 없음' (5xx 시계열이 아예 없으면 나눗셈 결과가 빈다) → 0
    - p95, mem_ratio, mem_growth: 비면 '측정 못 함' (수집 중단, 재시작 직후) → 비워 두고, 점수 계산 때 평소 값(z=0)으로 본다.
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
            # 줄어드는 것(메모리 해제)은 이상이 아니다. 늘어나는 쪽만 본다
            "mem_growth": num["mem_growth"].clip(lower=0),
        },
        index=raw.index,
    )[FEATURES]


def standardize(scaler, x: pd.DataFrame) -> np.ndarray:
    """z-score. 측정 못 한 값(NaN)은 평소 값(z=0)으로 본다."""
    return np.nan_to_num(scaler.transform(x.to_numpy()), nan=0.0)


def confirm_with_neighbor(weak: np.ndarray, any_rule: np.ndarray, minute: np.ndarray | None) -> np.ndarray:
    """약한 판정(weak)은 바로 앞이나 뒤 1분도 이상(any_rule)일 때만 인정한다.

    minute(분 번호)를 주면 실제로 1분 차이인 행만 이웃으로 본다 (평가 데이터는 중간에 빠진 분이 있다).
    안 주면 행이 연속된 분이라고 본다 (서빙). 마지막 행은 뒤가 없으므로 앞 1분만 본다 = 실시간에서는 1분 늦게 확정된다.
    """
    if len(weak) < 2:
        return np.zeros(len(weak), dtype=bool)
    adjacent = np.ones(len(weak) - 1, dtype=bool) if minute is None else (np.diff(np.asarray(minute, dtype=float)) == 1)
    prev = np.r_[False, any_rule[:-1] & adjacent]
    nxt = np.r_[any_rule[1:] & adjacent, False]
    return weak & (prev | nxt)


def score_frame(bundle: dict, raw: pd.DataFrame, minute: np.ndarray | None = None) -> pd.DataFrame:
    """bundle(scaler, forest, threshold, z_limit, rps_low) 로 점수를 낸다. 학습 평가와 서빙이 같이 쓴다."""
    # 모델마다 학습한 특성 수가 다르다 (v5 까지 4개, v6 부터 mem_growth 포함 5개). 새 코드로 이전 챔피언을
    # 평가할 때(evaluate 단계의 비교) 그 모델이 아는 특성만 넣는다. 특성은 뒤에만 덧붙이므로 앞에서부터 자르면 맞다.
    names = bundle.get("features") or FEATURES[: bundle["scaler"].n_features_in_]
    feats = to_features(raw)
    z = standardize(bundle["scaler"], feats[names])
    score = -bundle["forest"].score_samples(z)  # IsolationForest: 낮을수록 이상 → 부호를 뒤집는다
    abs_z = np.abs(z)
    top = abs_z.argmax(axis=1)
    iforest = score >= bundle["threshold"]
    zscore = (abs_z >= bundle["z_limit"]).any(axis=1)
    # 요청 급감: 요청 수는 평소에도 시간대에 따라 크게 오르내려(표준편차가 큼) z-score 의 아래 경계가 음수다.
    # 그래서 0 으로 떨어져도 z 로는 못 잡는다. 학습 기간의 낮은 쪽(1% 지점)의 절반 아래면 이상으로 본다.
    rps_low = bundle.get("rps_low")  # v5 이전 모델에는 없다
    drop = (feats["rps"].to_numpy() < rps_low) if rps_low is not None else np.zeros(len(feats), dtype=bool)
    # IsolationForest 단독 판정은 근거가 약하다: 어느 특성도 크게 벗어나지 않았는데 조합이 드물다는 뜻이다.
    # 11-27 분석 (시간 순 분할 12회, 정상 5,538분): 오탐 102분이 전부 IsolationForest 단독이었고 최대 z 가 1.4 이하였다
    # (요청이 평소보다 조금 적은 시간대). 같은 방식으로 잡은 진짜 장애는 분할마다 1~2분뿐.
    # - v8~: 어느 특성이든 |z| >= iforest_min_z(2) 로 뒷받침될 때만 인정 → 오탐 0분, 재현율 -0.8%p.
    #   "조합도 드물고 한 특성도 꽤 벗어났다" 일 때만 쓴다 (z 한계 4 에 못 미치는 2~4 구간을 조기에 잡는 역할).
    # - v7: 이웃한 분도 이상일 때만 인정 (오탐이 절반만 줄어 v8 에서 위 방식으로 바꿈. v7 평가·서빙을 위해 남겨 둔다).
    # - v6 이전(키 없음): 전부 즉시.
    iforest_ok = iforest
    if bundle.get("iforest_min_z") is not None:
        iforest_ok = iforest_ok & (abs_z.max(axis=1) >= bundle["iforest_min_z"])
    if bundle.get("iforest_needs_neighbor"):
        iforest_ok = confirm_with_neighbor(iforest_ok, iforest | zscore | drop, minute)
    top_names = [names[i] for i in top]
    top_names = ["rps" if d and not zz else n for n, d, zz in zip(top_names, drop, zscore)]
    return pd.DataFrame(
        {
            "score": score.round(4),
            # 혼합 규칙. IsolationForest 는 학습 기간에 한 번도 변하지 않은 특성(예: 장애 없던 기간의 오류율)으로는
            # 나무를 나누지 못해 그 특성의 급등을 못 본다. 그 빈틈을 특성별 z-score 가 메운다.
            "is_anomaly": iforest_ok | zscore | drop,
            "iforest_anomaly": iforest,
            "zscore_anomaly": zscore,
            "traffic_drop": drop,
            "top_feature": top_names,
            "top_z": abs_z.max(axis=1).round(2),
            # 특성별 z (부호: + 평소보다 큼, - 작음). 대시보드에서 "왜 이상인지" 를 보여 주는 데 쓴다
            **{f"z_{f}": z[:, i].round(2) for i, f in enumerate(names)},
        },
        index=raw.index,
    )


class Detector(PythonModel):
    def load_context(self, context):
        self.bundle = joblib.load(context.artifacts["detector"])

    def predict(self, context, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
        return score_frame(self.bundle, model_input)


mlflow.models.set_model(Detector())

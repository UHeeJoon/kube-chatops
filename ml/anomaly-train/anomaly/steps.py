"""파이프라인 단계: extract → train → evaluate → register.

각 단계는 Argo Workflows 의 한 스텝이다. 단계 사이에는 MinIO 경로와 MLflow run id 만 넘긴다.
결과 값은 /tmp/outputs/<이름> 파일에 써서 Argo 출력 파라미터로 쓴다.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import joblib
import mlflow
import numpy as np
import pandas as pd
from mlflow import MlflowClient
from sklearn.ensemble import IsolationForest
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.preprocessing import StandardScaler

from . import data
from .detector_model import FEATURES, RAW_COLUMNS, score_frame, standardize, to_features

MODEL_NAME = os.environ.get("MODEL_NAME", "demo-anomaly")
EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT", "demo-anomaly")
MODEL_CODE = str(Path(__file__).with_name("detector_model.py"))
# MLflow 서버가 아티팩트를 프록시하는 위치 (mlflow server --artifacts-destination 과 같아야 함)
ARTIFACTS_DESTINATION = os.environ.get("MLFLOW_ARTIFACTS_DESTINATION", "s3://mlops/mlflow")
OUTPUTS = Path(os.environ.get("OUTPUTS_DIR", "/tmp/outputs"))
# FEATURES 순서: rps, error_ratio, p95_log_ms, mem_ratio, mem_growth.
# mem_growth 하한 0.005 = 분당 한도의 0.5% → z=4 경계가 분당 2% (평소 최대 0.3%, 누수 3~8%. 11-26 실측)
SCALE_FLOOR = [0.5, 0.01, 0.1, 0.01, 0.005]
RPS_LOW_QUANTILE = 0.01  # 요청 급감 경계 = 학습 기간 요청 수 1% 지점의 절반
RPS_LOW_FACTOR = 0.5
# 부분 급감 경계 = 정상일 때 "한 주기 전 대비 비율" 의 낮은 쪽(0.1% 지점) × 0.8. 실측 0.88 → 0.70.
# 비율이 정상에서도 이 값 아래로 내려가면 부하에 그 주기가 없는 것이므로 규칙을 끈다.
DROP_RATIO_QUANTILE = 0.001
DROP_RATIO_FACTOR = 0.8
DROP_RATIO_MIN_STABLE = 0.6
DROP_RATIO_MIN_ROWS = 300
MIN_EVAL_ANOMALY_MINUTES = 20  # 평가 데이터에 장애 구간이 이보다 적으면 F1 을 믿을 수 없어 통과시키지 않는다
FINAL_RECALL_TOLERANCE = 0.05  # 서빙할 모델(정상 구간 전체로 학습)의 장애 탐지율이 평가용 모델보다 이만큼 넘게 낮으면 통과시키지 않는다
IFOREST_MIN_Z = 2.0  # IsolationForest 판정은 어느 특성이든 |z| 가 이 값 이상일 때만 인정 (z 한계 4 의 절반)
CV_BLOCKS = 5


def emit(name: str, value) -> None:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    (OUTPUTS / name).write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    print(f"[output] {name} = {value}")


# ── extract ──


def extract(days: float, prefix: str, history_days: int = 28) -> str:
    df = data.build_dataset(days)
    if df.empty:
        raise SystemExit("데이터가 없다 (Prometheus 질의 결과 0행)")
    fresh_rows = len(df)
    if history_days > 0:
        # Prometheus 보존 기간(7일) 너머의 데이터를 쓴다. 쌓기에 실패해도 이번 학습은 새로 읽은 것으로 진행한다.
        try:
            df = data.accumulate(df, history_days)
            print(f"쌓인 데이터 {len(df)}행 (이번에 읽은 것 {fresh_rows}행)")
        except Exception as e:  # noqa: BLE001
            print(f"데이터 쌓기 실패, 이번에 읽은 것만 쓴다: {type(e).__name__}: {e}")
    df = data.add_prev_cycle(df)
    key = data.put_frame(df, f"{prefix}/dataset.parquet")
    summary = {
        "rows": len(df),
        "fresh_rows": fresh_rows,
        "anomaly_minutes": int(df["label"].sum()),
        "ambiguous_minutes": int(df["ambiguous"].sum()),
        "unlabeled_incident_minutes": int(df["unlabeled_incident"].sum()),
        "start": str(df["ts"].min()),
        "end": str(df["ts"].max()),
        "kinds": df.loc[df["label"] == 1, "kind"].value_counts().to_dict(),
    }
    print(json.dumps(summary, ensure_ascii=False))
    emit("dataset", key)
    return key


# ── train ──


def usable(df: pd.DataFrame) -> pd.DataFrame:
    """정답을 믿을 수 있는 분만: 애매 구간(주입을 켜고 끈 직후)과 주입 표시 없는 장애 구간을 뺀다."""
    drop = df["ambiguous"].astype(bool)
    if "unlabeled_incident" in df:  # 11-28 이전에 만든 데이터에는 없다
        drop = drop | df["unlabeled_incident"].astype(bool)
    return df[~drop]


def split(df: pd.DataFrame, train_frac: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """정상 구간 앞쪽 train_frac 로 학습, 나머지(정상 뒤쪽 + 모든 장애 구간)로 평가.

    IsolationForest 는 정답을 쓰지 않는 모델이라 장애 구간은 학습에 넣지 않고 전부 평가에 쓴다.
    시간 순으로 자르므로 평가 데이터가 학습 데이터보다 미래다.
    """
    df = usable(df).sort_values("ts").reset_index(drop=True)
    normal = df[df["label"] == 0]
    if len(normal) < 60:
        raise SystemExit(f"정상 구간이 너무 짧다 ({len(normal)}분)")
    cutoff = normal["ts"].iloc[int(len(normal) * train_frac)]
    train = normal[normal["ts"] < cutoff]
    test = df[(df["ts"] >= cutoff) | (df["label"] == 1)]
    return train, test


def fit_bundle(train: pd.DataFrame, quantile: float, z_limit: float, seed: int = 42) -> dict:
    x = to_features(train.reindex(columns=RAW_COLUMNS))
    scaler = StandardScaler().fit(x.to_numpy())  # NaN 은 평균·분산 계산에서 빠진다
    # 거의 변하지 않는 특성(예: 장애가 없던 기간의 error_ratio = 0)은 표준편차가 0 이다.
    # scikit-learn 은 이때 scale_ 을 1 로 바꿔 두므로(z = 원값) scale_ 이 아니라 분산에서 다시 계산하고,
    # 특성별 하한(오류율 1%p, 지연 log 0.1, 메모리 1%p, rps 0.5)을 둔다.
    scaler.scale_ = np.maximum(np.sqrt(scaler.var_), SCALE_FLOOR)
    z = standardize(scaler, x)
    forest = IsolationForest(n_estimators=200, random_state=seed).fit(z)
    threshold = float(np.quantile(-forest.score_samples(z), quantile))
    rps_low = float(np.quantile(x["rps"].dropna(), RPS_LOW_QUANTILE) * RPS_LOW_FACTOR)
    return {"scaler": scaler, "forest": forest, "threshold": threshold, "z_limit": z_limit, "rps_low": rps_low,
            "features": list(FEATURES), "iforest_min_z": IFOREST_MIN_Z, "rps_drop_ratio": drop_ratio(train, rps_low)}


def drop_ratio(train: pd.DataFrame, rps_low: float) -> float | None:
    """정상 구간에서 "한 주기 전 대비 요청 수 비율" 이 얼마나 낮아지는지 보고 부분 급감 경계를 정한다. 정할 수 없으면 None."""
    if "rps_prev_cycle" not in train:
        return None
    prev = pd.to_numeric(train["rps_prev_cycle"], errors="coerce")
    ratio = (train["rps"] / prev)[prev > rps_low].dropna()
    if len(ratio) < DROP_RATIO_MIN_ROWS:
        return None
    low = float(np.quantile(ratio, DROP_RATIO_QUANTILE))
    return round(low * DROP_RATIO_FACTOR, 3) if low >= DROP_RATIO_MIN_STABLE else None


def cv_false_alarms(normal: pd.DataFrame, quantile: float, z_limit: float) -> dict:
    """정상 구간 전체로 본 오탐: 시간 블록으로 나눠, 각 블록을 나머지 블록으로 학습한 모델로 채점한다.

    평가 데이터의 정상 구간은 뒤쪽 30% 뿐이라 짧고(0.3일) 장애 직후에 몰려 있다. 거기서 센 오탐은 데이터 창이
    몇 분만 움직여도 2분 ↔ 9분으로 흔들렸다 (11-27). 정상 구간 전체를 쓰면 표본이 3배쯤 된다.
    """
    normal = normal.sort_values("ts").reset_index(drop=True)
    minutes = events = 0
    for block in np.array_split(normal.index.to_numpy(), CV_BLOCKS):
        held = normal.loc[block]
        scored = score_frame(fit_bundle(normal.drop(block), quantile, z_limit), held.reindex(columns=RAW_COLUMNS), minute_numbers(held))
        fa = false_alarms(held["label"], scored["is_anomaly"], minute_numbers(held))
        minutes += fa["false_alarm_minutes"]
        events += fa["false_alarm_events"]
    return {"cv_false_alarm_minutes": minutes, "cv_false_alarm_events": events, "cv_normal_days": round(len(normal) / 1440, 2)}


def train(dataset_key: str, prefix: str, train_frac: float, quantile: float, z_limit: float) -> str:
    df = data.get_frame(dataset_key)
    train_df, test_df = split(df, train_frac)
    bundle = fit_bundle(train_df, quantile, z_limit)
    test_key = data.put_frame(test_df, f"{prefix}/test.parquet")

    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name=os.environ.get("ARGO_WORKFLOW_NAME") or None) as run:
        mlflow.log_params(
            {
                "train_frac": train_frac,
                "threshold_quantile": quantile,
                "z_limit": z_limit,
                "train_rows": len(train_df),
                "test_rows": len(test_df),
                "test_anomaly_rows": int(test_df["label"].sum()),
                "unlabeled_incident_rows": int(df["unlabeled_incident"].sum()) if "unlabeled_incident" in df else 0,
                "data_start": str(df["ts"].min()),
                "data_end": str(df["ts"].max()),
                "dataset": f"s3://{data.bucket()}/{dataset_key}",
            }
        )
        mlflow.log_metric("threshold", bundle["threshold"])
        mlflow.log_metric("rps_low", bundle["rps_low"])
        if bundle["rps_drop_ratio"] is not None:
            mlflow.log_metric("rps_drop_ratio", bundle["rps_drop_ratio"])
        normal_all = usable(df)[lambda d: d["label"] == 0]
        cv = cv_false_alarms(normal_all, quantile, z_limit)
        mlflow.log_metrics(cv)
        print(json.dumps(cv))
        # 특성별 평소 값과 z=4 경계 (MLflow 화면에서 "무엇을 기준으로 이상인가" 를 볼 수 있게)
        sc = bundle["scaler"]
        mlflow.log_metrics({f"normal_mean_{f}": float(m) for f, m in zip(FEATURES, sc.mean_)})
        mlflow.log_metrics({f"normal_scale_{f}": float(v) for f, v in zip(FEATURES, sc.scale_)})
        # 입력 예시가 모델의 입력 스키마가 된다 (서빙이 이 열들을 요청에서 읽는다). 빈 값이 없는 행을 쓴다
        example = train_df.reindex(columns=RAW_COLUMNS).astype("float64").fillna(train_df.reindex(columns=RAW_COLUMNS).astype("float64").median()).fillna(0.0).head(3).reset_index(drop=True)
        # 모델을 둘 남긴다.
        #   model_eval: 정상 구간 앞쪽 70% 로 학습. 평가(뒤쪽 30% + 장애 구간)는 이 모델로 한다 (본 적 없는 데이터로 재야 하므로).
        #   model     : 정상 구간 전체로 같은 방법으로 다시 학습. 등록·서빙하는 것은 이 모델이다.
        # 앞쪽 70% 모델을 그대로 서빙하면 가장 최근 30% 를 모르는 모델이 나간다. 평소 기준이 최근에 바뀌어 재학습한 경우
        # (AnomalyBaselineDrift) 새 기준은 전부 평가 쪽에만 들어가 재학습해도 고쳐지지 않았다 (11-28 검토).
        final = fit_bundle(normal_all, quantile, z_limit)
        mlflow.log_param("final_train_rows", len(normal_all))
        # 부분 급감 규칙이 켜졌는지 (데이터가 모자라거나 정상 비율이 불안정하면 꺼진다). 꺼져도 다른 지표에는 드러나지 않는다
        mlflow.log_metric("rps_drop_ratio_enabled", int(final["rps_drop_ratio"] is not None))
        mlflow.log_metrics({f"final_normal_mean_{f}": float(m) for f, m in zip(FEATURES, final["scaler"].mean_)})
        mlflow.set_tag("eval_model_id", _log_bundle(bundle, "model_eval", example))
        mlflow.set_tag("model_id", _log_bundle(final, "model", example))
        mlflow.set_tag("test_set", f"s3://{data.bucket()}/{test_key}")
        run_id = run.info.run_id
    emit("run_id", run_id)
    emit("test_set", test_key)
    return run_id


def _log_bundle(bundle: dict, name: str, example: pd.DataFrame) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "detector.joblib"
        joblib.dump(bundle, path)
        info = mlflow.pyfunc.log_model(name=name, python_model=MODEL_CODE, artifacts={"detector": str(path)},
                                       input_example=example, pip_requirements=_pip_requirements())
    return info.model_id


def _pip_requirements() -> list[str]:
    from importlib.metadata import version

    return [f"{p}=={version(p)}" for p in ("scikit-learn", "pandas", "numpy", "joblib")]


# ── evaluate ──


def _metrics(y: pd.Series, pred: pd.Series) -> dict:
    return {
        "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y, pred, zero_division=0)), 4),
    }


def _bundle_of(model_uri: str) -> dict:
    """서빙과 같은 방식(pyfunc)으로 모델을 불러 그 안의 bundle 을 꺼낸다."""
    return mlflow.pyfunc.load_model(model_uri).unwrap_python_model().bundle


def minute_numbers(frame: pd.DataFrame) -> np.ndarray | None:
    """행의 분 번호. 평가 데이터는 애매 구간·학습 구간이 빠져 있어, 이웃한 분인지 판단할 때 쓴다."""
    if "ts" not in frame:
        return None
    return (pd.to_datetime(frame["ts"], utc=True).astype("int64") // 60_000_000_000).to_numpy()


def false_alarms(y: pd.Series, pred: pd.Series, minute: np.ndarray | None) -> dict:
    """정상 구간의 오탐. 분 수와 건수(연속된 분은 한 건), 그리고 표본 크기(정상 며칠치인지)를 같이 낸다.

    하루 환산값만 보면 표본이 작을 때 크게 흔들린다 (v6: 정상 0.4일에서 10분 → "하루 39건").
    """
    fp = ((y == 0) & pred.astype(bool)).to_numpy()
    step = np.r_[np.inf, np.diff(minute)] if minute is not None else np.ones(len(fp))
    events = int((fp & ~(np.r_[False, fp[:-1]] & (step == 1))).sum())
    days = float((y == 0).sum()) / 1440
    return {
        "false_alarm_minutes": int(fp.sum()),
        "false_alarm_events": events,
        "normal_days": round(days, 2),
        "false_alarms_per_day": round(float(fp.sum()) / max(days, 1e-9), 2),
    }


def episodes(test: pd.DataFrame, pred: pd.Series, minute: np.ndarray | None) -> dict:
    """장애를 건(연속된 분)으로 센다: 몇 건 중 몇 건을 잡았나, 잡기까지 몇 분 걸렸나.

    분 단위 재현율은 긴 장애 하나가 점수를 좌우한다. 운영에서 중요한 것은 "장애를 놓치지 않았나" 와 "얼마나 빨리" 다.
    평가 데이터는 장애 시작 뒤 애매 구간(AMBIGUOUS_MINUTES 분)이 빠져 있으므로, 지연은 "애매 구간이 끝난 뒤 몇 분 더" 다
    (0 이면 장애 시작 약 3분 안에 잡은 것). 같은 종류의 장애가 5분 넘게 끊기면 다른 건으로 본다.
    """
    if minute is None or not len(test):
        return {}
    frame = pd.DataFrame({"minute": minute, "kind": test["kind"].astype(str).to_numpy(), "label": test["label"].to_numpy(),
                          "pred": pred.astype(bool).to_numpy()})
    frame = frame[frame["label"] == 1].sort_values("minute")
    found: list[tuple[str, bool, float | None]] = []
    for kind, rows in frame.groupby("kind"):
        group = (rows["minute"].diff() > 5).cumsum()
        for _, ep in rows.groupby(group):
            hit = ep.loc[ep["pred"], "minute"]
            found.append((kind, len(hit) > 0, float(hit.iloc[0] - ep["minute"].iloc[0]) if len(hit) else None))
    if not found:
        return {}
    delays = [d for _, ok, d in found if ok]
    out = {"episodes": len(found), "episodes_detected": sum(ok for _, ok, _ in found),
           "episodes_missed_kinds": ",".join(sorted({k for k, ok, _ in found if not ok}))}
    if delays:
        out |= {"detect_late_minutes_median": float(np.median(delays)), "detect_late_minutes_max": float(max(delays))}
    return out


REPLAY_HOURS = 24


def compare_replay(frame: pd.DataFrame, new: pd.Series, old: pd.Series) -> dict:
    """같은 구간을 두 모델로 채점했을 때 판정이 갈린 분. frame 에는 label(주입), unlabeled_incident 가 있다.

    갈린 분이 장애 중이었으면 그쪽 모델이 더 잡은 것이고, 아무 일도 없던 때였으면 그쪽 모델의 오탐 후보다.
    """
    new, old = new.astype(bool).to_numpy(), old.astype(bool).to_numpy()
    trouble = (frame["label"].to_numpy() == 1) | frame["ambiguous"].to_numpy(dtype=bool) | frame.get("unlabeled_incident", pd.Series(False, index=frame.index)).to_numpy(dtype=bool)
    only_new, only_old = new & ~old, old & ~new
    return {
        "replay_minutes": int(len(frame)),
        "replay_new_anomaly_minutes": int(new.sum()),
        "replay_champion_anomaly_minutes": int(old.sum()),
        "replay_only_new_in_trouble": int((only_new & trouble).sum()),
        "replay_only_new_in_quiet": int((only_new & ~trouble).sum()),
        "replay_only_champion_in_trouble": int((only_old & trouble).sum()),
        "replay_only_champion_in_quiet": int((only_old & ~trouble).sum()),
    }


def replay(new_bundle: dict, champion_bundle: dict) -> dict:
    """최근 REPLAY_HOURS 시간의 실제 지표를 새 모델과 지금 서빙 모델로 다시 채점해 비교한다 (승격 전에 실제 트래픽에서의 차이를 본다).

    평가 데이터는 학습 창의 뒤쪽 30% 와 장애 구간뿐이다. 여기서는 가장 최근 하루를 빠짐없이, 서빙과 같은 방식(연속된 분)으로 본다.
    """
    frame = data.build_dataset(REPLAY_HOURS / 24)
    if frame.empty:
        return {}
    frame = data.add_prev_cycle(frame)
    minute = minute_numbers(frame)
    return compare_replay(frame, score_frame(new_bundle, frame.reindex(columns=RAW_COLUMNS), minute)["is_anomaly"],
                          score_frame(champion_bundle, frame.reindex(columns=RAW_COLUMNS), minute)["is_anomaly"])


def evaluate(run_id: str, test_key: str, min_f1: float) -> bool:
    client = MlflowClient()
    tags = client.get_run(run_id).data.tags
    model_id = tags["model_id"]  # 서빙할 모델 (정상 구간 전체로 학습)
    eval_id = tags.get("eval_model_id") or model_id  # 평가용 (앞쪽 70%). 예전 run 에는 없다
    test = data.get_frame(test_key)
    y = test["label"]

    minute = minute_numbers(test)
    new_bundle = _bundle_of(f"models:/{eval_id}")
    final_bundle = _bundle_of(f"models:/{model_id}") if eval_id != model_id else new_bundle
    scored = score_frame(new_bundle, test.reindex(columns=RAW_COLUMNS), minute)
    model_m = _metrics(y, scored["is_anomaly"])
    base_m = _metrics(y, scored["zscore_anomaly"])  # 기준선: 특성별 z-score 만
    iforest_m = _metrics(y, scored["iforest_anomaly"])  # 참고: IsolationForest 만
    by_kind = {}
    for kind in sorted({k for ks in test["kind"].dropna() for k in str(ks).split(",") if k}):
        mask = test["kind"].astype(str).str.contains(kind)
        by_kind[f"recall_{kind}"] = round(float(scored.loc[mask, "is_anomaly"].mean()), 4)
    fa = false_alarms(y, scored["is_anomaly"], minute)
    eps = episodes(test, scored["is_anomaly"], minute)

    # 지금 champion 이 있으면 같은 평가 데이터로 비교한다 (나빠지는 모델은 올리지 않는다)
    champion_f1 = None
    champion_fa = None
    # "champion 이 아직 없다" 와 "champion 을 확인하지 못했다" 를 구분한다. 뒤의 것(MLflow 장애, 아티팩트 내려받기 실패)을
    # 첫 모델로 처리하면 지금 모델과 비교하지 않고 최소 기준만으로 통과한다. 그런 경우는 이 단계를 실패시킨다.
    try:
        champ = client.get_model_version_by_alias(MODEL_NAME, "champion")
    except mlflow.exceptions.MlflowException as e:
        if e.error_code not in ("RESOURCE_DOES_NOT_EXIST", "INVALID_PARAMETER_VALUE"):
            raise
        champ = None
    if champ is None:
        print("champion 없음 (첫 모델)")
        champion_bundle = None
    else:
        champion_bundle = _bundle_of(f"models:/{MODEL_NAME}@champion")
        champ_scored = score_frame(champion_bundle, test.reindex(columns=RAW_COLUMNS), minute)
        champion_f1 = _metrics(y, champ_scored["is_anomaly"])["f1"]
        champion_fa = false_alarms(y, champ_scored["is_anomaly"], minute)["false_alarm_minutes"]
        print(f"champion v{champ.version} f1={champion_f1} false_alarm_minutes={champion_fa}")
    shadow: dict = {}
    if champion_bundle is not None:
        try:
            shadow = replay(final_bundle, champion_bundle)  # 실제로 서빙할 모델로 비교한다
        except Exception as e:  # noqa: BLE001 — 비교는 참고 자료다. 실패해도 평가 결과는 낸다
            print(f"최근 {REPLAY_HOURS}시간 재채점 실패: {type(e).__name__}: {e}")

    # 통과 조건: 최소 F1, 기준선보다 크게 나쁘지 않음(0.02 이내), champion 보다 나빠지지 않음(0.01 이내),
    # 같은 평가 데이터에서 champion 보다 오탐이 늘지 않음 (1분까지는 표본 흔들림으로 봄).
    # 오탐이 늘어난 모델은 F1 이 좋아도 올리지 않는다 (11-27 결정: v6 는 F1 0.917 인데 오탐 때문에 승격 보류).
    # 서빙할 모델(정상 구간 전체로 다시 학습)은 평가 데이터의 정상 구간을 이미 봤으므로 오탐은 여기서 잴 수 없다.
    # 대신 장애를 놓치지 않는지만 본다: 최근 데이터를 더 넣어 기준이 넓어지면 장애 탐지율이 떨어질 수 있다.
    final_recall = _metrics(y, score_frame(final_bundle, test.reindex(columns=RAW_COLUMNS), minute)["is_anomaly"])["recall"]
    # 지금 모델에 있던 규칙이 새 모델에서 조용히 사라지면 안 된다 (부분 급감 규칙은 학습 데이터에 따라 꺼질 수 있다)
    lost_rules = [k for k in ("rps_drop_ratio",) if champion_bundle is not None and champion_bundle.get(k) is not None and final_bundle.get(k) is None]
    if lost_rules:
        print(f"지금 모델에 있던 규칙이 새 모델에 없다: {lost_rules}")
    # 평가할 장애가 없으면 F1 은 0 이라 어차피 떨어지지만, 이유가 "모델이 나쁘다" 가 아니라 "잴 것이 없다" 임을 남긴다
    if int(y.sum()) < MIN_EVAL_ANOMALY_MINUTES:
        print(f"평가 데이터의 장애 구간이 {int(y.sum())}분뿐이다 (최소 {MIN_EVAL_ANOMALY_MINUTES}분). 장애 주입 기록이 있는 기간을 포함해 다시 학습해야 한다")
    passed = (
        not lost_rules
        and int(y.sum()) >= MIN_EVAL_ANOMALY_MINUTES
        and final_recall >= model_m["recall"] - FINAL_RECALL_TOLERANCE
        and model_m["f1"] >= min_f1
        and model_m["f1"] >= base_m["f1"] - 0.02
        and (champion_f1 is None or model_m["f1"] >= champion_f1 - 0.01)
        and (champion_fa is None or fa["false_alarm_minutes"] <= champion_fa + 1)
    )
    result = {
        "model": model_m,
        "baseline_zscore": base_m,
        "iforest_only": iforest_m,
        "champion_f1": champion_f1,
        "final_recall": final_recall,
        "lost_rules": lost_rules,
        "eval_anomaly_minutes": int(y.sum()),
        **fa,
        "champion_false_alarm_minutes": champion_fa,
        **by_kind,
        **eps,
        **shadow,
        "passed": passed,
    }
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics({f"test_{k}": v for k, v in model_m.items()})
        mlflow.log_metrics({f"baseline_{k}": v for k, v in base_m.items()})
        mlflow.log_metrics({f"iforest_{k}": v for k, v in iforest_m.items()})
        mlflow.log_metric("final_recall", final_recall)
        mlflow.log_metrics({**fa, **by_kind, **shadow, **{k: v for k, v in eps.items() if not isinstance(v, str)}})
        if champion_fa is not None:
            mlflow.log_metric("champion_false_alarm_minutes", champion_fa)
        if champion_f1 is not None:
            mlflow.log_metric("champion_f1", champion_f1)
        mlflow.set_tag("evaluation", "passed" if passed else "failed")
        mlflow.log_dict(result, "evaluation.json")
    print(json.dumps(result, ensure_ascii=False))
    emit("passed", "true" if passed else "false")
    emit("metrics", result)
    return passed


# ── register ──


def storage_uri(model_id: str) -> str:
    """MLflow 아티팩트 위치(mlflow-artifacts:/...) → KServe 가 읽을 S3 경로."""
    loc = MlflowClient().get_logged_model(model_id).artifact_location
    if loc.startswith("mlflow-artifacts:/"):
        return ARTIFACTS_DESTINATION.rstrip("/") + "/" + loc.removeprefix("mlflow-artifacts:/").lstrip("/")
    return loc


def register(run_id: str) -> str:
    client = MlflowClient()
    run = client.get_run(run_id)
    model_id = run.data.tags["model_id"]
    # 이 단계가 재시도되면(노드 오류) 같은 모델이 두 번 등록된다. 이미 등록된 버전이 있으면 그것을 쓴다
    existing = [v for v in client.search_model_versions(f"name = '{MODEL_NAME}' and run_id = '{run_id}'")]
    mv = existing[0] if existing else mlflow.register_model(f"models:/{model_id}", MODEL_NAME)
    uri = storage_uri(model_id)
    client.set_model_version_tag(MODEL_NAME, mv.version, "storage_uri", uri)
    client.set_model_version_tag(MODEL_NAME, mv.version, "test_f1", run.data.metrics.get("test_f1", ""))
    client.set_registered_model_alias(MODEL_NAME, "challenger", mv.version)
    emit("version", str(mv.version))
    emit("storage_uri", uri)
    return uri


# ── notify (Argo onExit) ──


def summary(workflow: str) -> dict:
    """워크플로 이름(= MLflow run 이름)으로 이번 학습 결과와 지금 champion 을 모은다."""
    client = MlflowClient()
    out: dict = {"workflow": workflow}
    exp = client.get_experiment_by_name(EXPERIMENT)
    runs = client.search_runs([exp.experiment_id], f"attributes.run_name = '{workflow}'", max_results=1) if exp else []
    if runs:
        run = runs[0]
        m, p = run.data.metrics, run.data.params
        out |= {
            "run_id": run.info.run_id,
            "model_id": run.data.tags.get("model_id"),
            "evaluation": run.data.tags.get("evaluation"),
            "f1": m.get("test_f1"),
            "precision": m.get("test_precision"),
            "recall": m.get("test_recall"),
            "baseline_f1": m.get("baseline_f1"),
            "iforest_f1": m.get("iforest_f1"),
            "false_alarms_per_day": m.get("false_alarms_per_day"),
            "false_alarm_minutes": m.get("false_alarm_minutes"),
            "false_alarm_events": m.get("false_alarm_events"),
            "normal_days": m.get("normal_days"),
            "champion_false_alarm_minutes": m.get("champion_false_alarm_minutes"),
            "cv_false_alarm_minutes": m.get("cv_false_alarm_minutes"),
            "cv_false_alarm_events": m.get("cv_false_alarm_events"),
            "cv_normal_days": m.get("cv_normal_days"),
            "episodes": m.get("episodes"),
            "episodes_detected": m.get("episodes_detected"),
            "detect_late_minutes_median": m.get("detect_late_minutes_median"),
            "detect_late_minutes_max": m.get("detect_late_minutes_max"),
            "replay": {k.removeprefix("replay_"): m.get(k) for k in m if k.startswith("replay_")} or None,
            "recall_by_kind": {k.removeprefix("recall_"): v for k, v in m.items() if k.startswith("recall_")},
            "data": {k: p.get(k) for k in ("data_start", "data_end", "train_rows", "test_rows", "test_anomaly_rows")},
        }
        versions = client.search_model_versions(f"name = '{MODEL_NAME}' and run_id = '{run.info.run_id}'")
        if versions:
            mv = versions[0]
            out |= {"version": mv.version, "storage_uri": mv.tags.get("storage_uri")}
    try:
        champ = client.get_model_version_by_alias(MODEL_NAME, "champion")
        champ_run = client.get_run(champ.run_id)
        out["champion"] = {"version": champ.version, "f1": champ_run.data.metrics.get("test_f1")}
    except mlflow.exceptions.MlflowException:
        out["champion"] = None
    return out


def parse_failures(raw: str) -> list[dict]:
    """Argo {{workflow.failures}} → [{step, message}]. 실패가 없으면 '"null"' 처럼 따옴표가 한 번 더 붙어 온다."""
    value: object = raw
    for _ in range(2):  # JSON 문자열 안에 JSON 이 든 경우까지
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    if value in (None, "", "null", []):
        return []
    if isinstance(value, list):
        return [
            {"step": f.get("displayName"), "message": str(f.get("message") or "")[:200]}
            for f in value if isinstance(f, dict)
        ][:3]
    return [{"message": str(value)[:300]}]


def notify(status: str, failures: str = "") -> None:
    """학습이 끝나면(성공·실패 모두) n8n WF-04 로 결과를 보낸다 → Slack 에 결과와 승격 버튼.

    알림 실패가 학습 결과를 바꾸면 안 되므로 여기서 나는 오류는 기록만 하고 0 으로 끝낸다.
    """
    import httpx

    workflow = os.environ.get("ARGO_WORKFLOW_NAME", "local")
    payload: dict = {"type": "training_finished", "workflow": workflow, "status": status,
                     "slack": {"channel": os.environ.get("SLACK_CHANNEL") or None,
                               "thread_ts": os.environ.get("SLACK_THREAD") or None}}
    parsed = parse_failures(failures)
    if parsed:
        payload["failures"] = parsed
    try:
        payload |= summary(workflow)
    except Exception as e:  # noqa: BLE001 — MLflow 가 죽어도 실패 알림은 가야 한다
        payload["summary_error"] = f"{type(e).__name__}: {e}"[:300]
    print(json.dumps(payload, ensure_ascii=False))
    url, token = os.environ.get("N8N_WEBHOOK_URL"), os.environ.get("N8N_WEBHOOK_TOKEN")
    if not url or not token:
        print("N8N_WEBHOOK_URL/TOKEN 없음, 알림 생략")
        return
    try:
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        print(f"n8n 응답 {r.status_code}")
        if r.status_code >= 400:
            # 학습 결과는 바꾸지 않지만(0 으로 끝낸다) 결과 카드가 Slack 에 가지 않았다는 것은 눈에 띄어야 한다
            print(f"경고: 결과 알림이 전달되지 않았습니다 (n8n {r.status_code}: {r.text[:200]}). Slack 에 결과 카드가 없습니다")
    except httpx.HTTPError as e:
        print(f"n8n 알림 실패: {e}")

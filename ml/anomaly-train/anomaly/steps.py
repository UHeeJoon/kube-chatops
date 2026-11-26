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
from .detector_model import RAW_COLUMNS, score_frame, standardize, to_features

MODEL_NAME = os.environ.get("MODEL_NAME", "demo-anomaly")
EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT", "demo-anomaly")
MODEL_CODE = str(Path(__file__).with_name("detector_model.py"))
# MLflow 서버가 아티팩트를 프록시하는 위치 (mlflow server --artifacts-destination 과 같아야 함)
ARTIFACTS_DESTINATION = os.environ.get("MLFLOW_ARTIFACTS_DESTINATION", "s3://mlops/mlflow")
OUTPUTS = Path(os.environ.get("OUTPUTS_DIR", "/tmp/outputs"))
SCALE_FLOOR = [0.5, 0.01, 0.1, 0.01]  # FEATURES 순서: rps, error_ratio, p95_log_ms, mem_ratio


def emit(name: str, value) -> None:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    (OUTPUTS / name).write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    print(f"[output] {name} = {value}")


# ── extract ──


def extract(days: float, prefix: str) -> str:
    df = data.build_dataset(days)
    if df.empty:
        raise SystemExit("데이터가 없다 (Prometheus 질의 결과 0행)")
    key = data.put_frame(df, f"{prefix}/dataset.parquet")
    summary = {
        "rows": len(df),
        "anomaly_minutes": int(df["label"].sum()),
        "ambiguous_minutes": int(df["ambiguous"].sum()),
        "start": str(df["ts"].min()),
        "end": str(df["ts"].max()),
        "kinds": df.loc[df["label"] == 1, "kind"].value_counts().to_dict(),
    }
    print(json.dumps(summary, ensure_ascii=False))
    emit("dataset", key)
    return key


# ── train ──


def split(df: pd.DataFrame, train_frac: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """정상 구간 앞쪽 train_frac 로 학습, 나머지(정상 뒤쪽 + 모든 장애 구간)로 평가.

    IsolationForest 는 정답을 쓰지 않는 모델이라 장애 구간은 학습에 넣지 않고 전부 평가에 쓴다.
    시간 순으로 자르므로 평가 데이터가 학습 데이터보다 미래다.
    """
    df = df[~df["ambiguous"]].sort_values("ts").reset_index(drop=True)
    normal = df[df["label"] == 0]
    if len(normal) < 60:
        raise SystemExit(f"정상 구간이 너무 짧다 ({len(normal)}분)")
    cutoff = normal["ts"].iloc[int(len(normal) * train_frac)]
    train = normal[normal["ts"] < cutoff]
    test = df[(df["ts"] >= cutoff) | (df["label"] == 1)]
    return train, test


def fit_bundle(train: pd.DataFrame, quantile: float, z_limit: float, seed: int = 42) -> dict:
    x = to_features(train[RAW_COLUMNS])
    scaler = StandardScaler().fit(x.to_numpy())  # NaN 은 평균·분산 계산에서 빠진다
    # 거의 변하지 않는 특성(예: 장애가 없던 기간의 error_ratio = 0)은 표준편차가 0 이다.
    # scikit-learn 은 이때 scale_ 을 1 로 바꿔 두므로(z = 원값) scale_ 이 아니라 분산에서 다시 계산하고,
    # 특성별 하한(오류율 1%p, 지연 log 0.1, 메모리 1%p, rps 0.5)을 둔다.
    scaler.scale_ = np.maximum(np.sqrt(scaler.var_), SCALE_FLOOR)
    z = standardize(scaler, x)
    forest = IsolationForest(n_estimators=200, random_state=seed).fit(z)
    threshold = float(np.quantile(-forest.score_samples(z), quantile))
    return {"scaler": scaler, "forest": forest, "threshold": threshold, "z_limit": z_limit}


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
                "data_start": str(df["ts"].min()),
                "data_end": str(df["ts"].max()),
                "dataset": f"s3://{data.bucket()}/{dataset_key}",
            }
        )
        mlflow.log_metric("threshold", bundle["threshold"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "detector.joblib"
            joblib.dump(bundle, path)
            example = train_df[RAW_COLUMNS].head(3).reset_index(drop=True)
            info = mlflow.pyfunc.log_model(
                name="model",
                python_model=MODEL_CODE,
                artifacts={"detector": str(path)},
                input_example=example,
                pip_requirements=_pip_requirements(),
            )
        mlflow.set_tag("model_id", info.model_id)
        mlflow.set_tag("test_set", f"s3://{data.bucket()}/{test_key}")
        run_id = run.info.run_id
    emit("run_id", run_id)
    emit("test_set", test_key)
    return run_id


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


def evaluate(run_id: str, test_key: str, min_f1: float) -> bool:
    client = MlflowClient()
    model_id = client.get_run(run_id).data.tags["model_id"]
    test = data.get_frame(test_key)
    y = test["label"]

    scored = score_frame(_bundle_of(f"models:/{model_id}"), test[RAW_COLUMNS])
    model_m = _metrics(y, scored["is_anomaly"])
    base_m = _metrics(y, scored["zscore_anomaly"])  # 기준선: 특성별 z-score 만
    iforest_m = _metrics(y, scored["iforest_anomaly"])  # 참고: IsolationForest 만
    by_kind = {}
    for kind in sorted({k for ks in test["kind"].dropna() for k in str(ks).split(",") if k}):
        mask = test["kind"].astype(str).str.contains(kind)
        by_kind[f"recall_{kind}"] = round(float(scored.loc[mask, "is_anomaly"].mean()), 4)
    false_alarms_per_day = float(scored.loc[y == 0, "is_anomaly"].sum()) / max((y == 0).sum() / 1440, 1e-9)

    # 지금 champion 이 있으면 같은 평가 데이터로 비교한다 (나빠지는 모델은 올리지 않는다)
    champion_f1 = None
    try:
        champ = client.get_model_version_by_alias(MODEL_NAME, "champion")
        champ_scored = score_frame(_bundle_of(f"models:/{MODEL_NAME}@champion"), test[RAW_COLUMNS])
        champion_f1 = _metrics(y, champ_scored["is_anomaly"])["f1"]
        print(f"champion v{champ.version} f1={champion_f1}")
    except mlflow.exceptions.MlflowException:
        print("champion 없음 (첫 모델)")

    # 통과 조건: 최소 F1, 기준선보다 크게 나쁘지 않음(0.02 이내), champion 보다 나빠지지 않음(0.01 이내)
    passed = (
        model_m["f1"] >= min_f1
        and model_m["f1"] >= base_m["f1"] - 0.02
        and (champion_f1 is None or model_m["f1"] >= champion_f1 - 0.01)
    )
    result = {
        "model": model_m,
        "baseline_zscore": base_m,
        "iforest_only": iforest_m,
        "champion_f1": champion_f1,
        "false_alarms_per_day": round(false_alarms_per_day, 2),
        **by_kind,
        "passed": passed,
    }
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics({f"test_{k}": v for k, v in model_m.items()})
        mlflow.log_metrics({f"baseline_{k}": v for k, v in base_m.items()})
        mlflow.log_metrics({f"iforest_{k}": v for k, v in iforest_m.items()})
        mlflow.log_metrics({"false_alarms_per_day": result["false_alarms_per_day"], **by_kind})
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
    mv = mlflow.register_model(f"models:/{model_id}", MODEL_NAME)
    uri = storage_uri(model_id)
    client.set_model_version_tag(MODEL_NAME, mv.version, "storage_uri", uri)
    client.set_model_version_tag(MODEL_NAME, mv.version, "test_f1", run.data.metrics.get("test_f1", ""))
    client.set_registered_model_alias(MODEL_NAME, "challenger", mv.version)
    emit("version", str(mv.version))
    emit("storage_uri", uri)
    return uri

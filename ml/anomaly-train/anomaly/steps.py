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
IFOREST_MIN_Z = 2.0  # IsolationForest 판정은 어느 특성이든 |z| 가 이 값 이상일 때만 인정 (z 한계 4 의 절반)
CV_BLOCKS = 5


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
    x = to_features(train[RAW_COLUMNS])
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
            "features": list(FEATURES), "iforest_min_z": IFOREST_MIN_Z}


def cv_false_alarms(normal: pd.DataFrame, quantile: float, z_limit: float) -> dict:
    """정상 구간 전체로 본 오탐: 시간 블록으로 나눠, 각 블록을 나머지 블록으로 학습한 모델로 채점한다.

    평가 데이터의 정상 구간은 뒤쪽 30% 뿐이라 짧고(0.3일) 장애 직후에 몰려 있다. 거기서 센 오탐은 데이터 창이
    몇 분만 움직여도 2분 ↔ 9분으로 흔들렸다 (11-27). 정상 구간 전체를 쓰면 표본이 3배쯤 된다.
    """
    normal = normal.sort_values("ts").reset_index(drop=True)
    minutes = events = 0
    for block in np.array_split(normal.index.to_numpy(), CV_BLOCKS):
        held = normal.loc[block]
        scored = score_frame(fit_bundle(normal.drop(block), quantile, z_limit), held[RAW_COLUMNS], minute_numbers(held))
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
        normal_all = usable(df)[lambda d: d["label"] == 0]
        cv = cv_false_alarms(normal_all, quantile, z_limit)
        mlflow.log_metrics(cv)
        print(json.dumps(cv))
        # 특성별 평소 값과 z=4 경계 (MLflow 화면에서 "무엇을 기준으로 이상인가" 를 볼 수 있게)
        sc = bundle["scaler"]
        mlflow.log_metrics({f"normal_mean_{f}": float(m) for f, m in zip(FEATURES, sc.mean_)})
        mlflow.log_metrics({f"normal_scale_{f}": float(v) for f, v in zip(FEATURES, sc.scale_)})
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


def evaluate(run_id: str, test_key: str, min_f1: float) -> bool:
    client = MlflowClient()
    model_id = client.get_run(run_id).data.tags["model_id"]
    test = data.get_frame(test_key)
    y = test["label"]

    minute = minute_numbers(test)
    scored = score_frame(_bundle_of(f"models:/{model_id}"), test[RAW_COLUMNS], minute)
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
    try:
        champ = client.get_model_version_by_alias(MODEL_NAME, "champion")
        champ_scored = score_frame(_bundle_of(f"models:/{MODEL_NAME}@champion"), test[RAW_COLUMNS], minute)
        champion_f1 = _metrics(y, champ_scored["is_anomaly"])["f1"]
        champion_fa = false_alarms(y, champ_scored["is_anomaly"], minute)["false_alarm_minutes"]
        print(f"champion v{champ.version} f1={champion_f1} false_alarm_minutes={champion_fa}")
    except mlflow.exceptions.MlflowException:
        print("champion 없음 (첫 모델)")

    # 통과 조건: 최소 F1, 기준선보다 크게 나쁘지 않음(0.02 이내), champion 보다 나빠지지 않음(0.01 이내),
    # 같은 평가 데이터에서 champion 보다 오탐이 늘지 않음 (1분까지는 표본 흔들림으로 봄).
    # 오탐이 늘어난 모델은 F1 이 좋아도 올리지 않는다 (11-27 결정: v6 는 F1 0.917 인데 오탐 때문에 승격 보류).
    passed = (
        model_m["f1"] >= min_f1
        and model_m["f1"] >= base_m["f1"] - 0.02
        and (champion_f1 is None or model_m["f1"] >= champion_f1 - 0.01)
        and (champion_fa is None or fa["false_alarm_minutes"] <= champion_fa + 1)
    )
    result = {
        "model": model_m,
        "baseline_zscore": base_m,
        "iforest_only": iforest_m,
        "champion_f1": champion_f1,
        **fa,
        "champion_false_alarm_minutes": champion_fa,
        **by_kind,
        **eps,
        "passed": passed,
    }
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics({f"test_{k}": v for k, v in model_m.items()})
        mlflow.log_metrics({f"baseline_{k}": v for k, v in base_m.items()})
        mlflow.log_metrics({f"iforest_{k}": v for k, v in iforest_m.items()})
        mlflow.log_metrics({**fa, **by_kind, **{k: v for k, v in eps.items() if not isinstance(v, str)}})
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
    mv = mlflow.register_model(f"models:/{model_id}", MODEL_NAME)
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
    except httpx.HTTPError as e:
        print(f"n8n 알림 실패: {e}")

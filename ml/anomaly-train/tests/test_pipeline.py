"""학습 → 평가 → 등록 단계를 실제 MLflow(임시 파일 저장소)로 이어서 돌린다.

단계 함수는 MLflow·MinIO·Prometheus 를 부르기 때문에 단위 시험이 없었고, 그래서 정의하기 전의 변수를 쓰는 실수가
이미지로 만들어져 배포된 뒤에야(11-28) lint 에서 드러났다. 저장소와 Prometheus 만 가짜로 바꾸고 나머지는 그대로 실행한다.
"""

import json

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from anomaly import data, steps
from test_detector import synthetic


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    # SQLite 저장소 (MLflow 3 은 파일 저장소를 받지 않는다). 학습 이미지는 mlflow-skinny 라 sqlalchemy·alembic 이 없으므로
    # 시험 환경에만 따로 설치한다 (.github/workflows/test.yml)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path}/mlflow.db")
    mlflow.set_registry_uri(f"sqlite:///{tmp_path}/mlflow.db")
    frames: dict = {}
    monkeypatch.setattr(data, "put_frame", lambda df, key: frames.__setitem__(key, df.copy()) or key)
    monkeypatch.setattr(data, "get_frame", lambda key: frames[key].copy())
    monkeypatch.setattr(data, "bucket", lambda: "test")
    monkeypatch.setattr(steps, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(steps, "replay", lambda new, champion: {})  # 최근 24시간 재채점은 Prometheus 가 필요하다
    monkeypatch.setattr(steps, "storage_uri", lambda model_id: f"s3://test/{model_id}")
    frames["data/dataset.parquet"] = synthetic()
    yield tmp_path
    mlflow.set_tracking_uri(None)
    mlflow.set_registry_uri(None)


def test_train_evaluate_register_run_end_to_end(pipeline):
    run_id = steps.train("data/dataset.parquet", "data", 0.7, 0.999, 4.0)
    tags = MlflowClient().get_run(run_id).data.tags
    # 평가용(앞쪽 70%)과 서빙용(정상 구간 전체) 모델이 따로 남는다
    assert tags["eval_model_id"] != tags["model_id"]
    assert MlflowClient().get_run(run_id).data.params["final_train_rows"] > MlflowClient().get_run(run_id).data.params["train_rows"]

    # champion 이 없을 때: 첫 모델로 통과
    assert steps.evaluate(run_id, "data/test.parquet", 0.6) is True
    result = json.loads((pipeline / "outputs" / "metrics").read_text())
    assert result["champion_f1"] is None and result["final_recall"] >= result["model"]["recall"] - steps.FINAL_RECALL_TOLERANCE
    assert result["eval_anomaly_minutes"] >= steps.MIN_EVAL_ANOMALY_MINUTES and result["lost_rules"] == []

    steps.register(run_id)
    client = MlflowClient()
    versions = client.search_model_versions(f"name = '{steps.MODEL_NAME}'")
    assert len(versions) == 1
    # 등록 단계가 재시도돼도 버전이 늘지 않는다
    steps.register(run_id)
    assert len(client.search_model_versions(f"name = '{steps.MODEL_NAME}'")) == 1

    # champion 이 있을 때: 같은 평가 데이터로 비교한다 (같은 모델이므로 통과)
    client.set_registered_model_alias(steps.MODEL_NAME, "champion", versions[0].version)
    assert steps.evaluate(run_id, "data/test.parquet", 0.6) is True
    assert json.loads((pipeline / "outputs" / "metrics").read_text())["champion_f1"] is not None


def test_evaluation_without_enough_anomalies_does_not_pass(pipeline):
    df = synthetic()
    df.loc[df["label"] == 1, ["label", "kind", "p95_seconds", "error_ratio"]] = [0, "", 0.0095, 0.0]
    df.loc[500:504, ["p95_seconds", "label", "kind"]] = [0.8, 1, "latency"]  # 장애 5분뿐
    data.put_frame(df, "data/few.parquet")
    run_id = steps.train("data/few.parquet", "few", 0.7, 0.999, 4.0)
    assert steps.evaluate(run_id, "few/test.parquet", 0.6) is False

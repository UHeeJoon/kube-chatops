"""KServe 커스텀 예측 서버: MLflow pyfunc 이상 탐지 모델을 V1 프로토콜로 서빙한다.

모델 파일은 KServe storage-initializer 가 STORAGE_URI(MinIO) 에서 /mnt/models 로 내려받는다.
특성 계산은 모델(detector_model.py, MLflow 아티팩트) 안에 있어 여기서는 행을 그대로 넘긴다.

요청: POST /v1/models/<이름>:predict  {"instances": [{"rps":..,"error_ratio":..,"p95_seconds":..,"mem_ratio":..}, ...]}
응답: {"predictions": [{"score":..,"is_anomaly":..,"iforest_anomaly":..,"zscore_anomaly":..,"top_feature":..,"top_z":..}, ...],
       "model_version": "<MLflow model id>"}
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import mlflow.pyfunc
import pandas as pd
from kserve import Model, ModelServer, model_server

log = logging.getLogger("anomaly-serve")
REQUIRED = ["rps", "error_ratio", "p95_seconds", "mem_ratio"]
MAX_ROWS = 1440  # 하루치(1분 단위)까지만 받는다


class AnomalyModel(Model):
    def __init__(self, name: str, model_dir: str):
        super().__init__(name)
        self.model_dir = model_dir
        self.model = None
        self.model_version = "unknown"

    def load(self) -> bool:
        self.model = mlflow.pyfunc.load_model(self.model_dir)
        self.model_version = self.model.metadata.model_id or self.model.metadata.run_id or "unknown"
        log.info("모델 로드 완료: %s (%s)", self.name, self.model_version)
        self.ready = True
        return self.ready

    def predict(self, payload: dict, headers=None, response_headers=None) -> dict:
        if isinstance(payload, (bytes, str)):  # Content-Type 없이 온 요청
            payload = json.loads(payload)
        rows = payload.get("instances") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError('"instances" 는 비어 있지 않은 배열이어야 한다')
        if len(rows) > MAX_ROWS:
            raise ValueError(f"한 번에 {MAX_ROWS}행까지 받는다 (받은 행 {len(rows)})")
        df = pd.DataFrame(rows)
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            raise ValueError(f"빠진 열: {missing}")
        # 모델 입력 스키마는 float. 정수(5)나 null 이 와도 받도록 숫자로 바꾼다 (null → NaN → 모델이 0 으로 채움)
        x = df[REQUIRED].apply(pd.to_numeric, errors="coerce").astype("float64")
        out = self.model.predict(x)
        # numpy bool/float 을 JSON 기본 타입으로
        return {"predictions": json.loads(out.to_json(orient="records")), "model_version": self.model_version}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(parents=[model_server.parser])
    parser.add_argument("--model_dir", default=os.environ.get("MODEL_DIR", "/mnt/models"))
    args, _ = parser.parse_known_args()
    model = AnomalyModel(args.model_name, args.model_dir)
    model.load()
    ModelServer().start([model])

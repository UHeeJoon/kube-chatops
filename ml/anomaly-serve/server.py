"""KServe 커스텀 예측 서버: MLflow pyfunc 이상 탐지 모델을 V1 프로토콜로 서빙한다.

모델 파일은 KServe storage-initializer 가 STORAGE_URI(MinIO) 에서 /mnt/models 로 내려받는다.
특성 계산은 모델(detector_model.py, MLflow 아티팩트) 안에 있어 여기서는 행을 그대로 넘긴다.

요청: POST /v1/models/<이름>:predict  {"instances": [{"rps":..,"error_ratio":..,"p95_seconds":..,"mem_ratio":..}, ...]}
응답: {"predictions": [{"score":..,"is_anomaly":..,"iforest_anomaly":..,"zscore_anomaly":..,"top_feature":..,"top_z":..,
                        "z_<특성>":..(모델 버전에 따라 없을 수 있음)}, ...],
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
        self.columns = list(REQUIRED)

    def load(self) -> bool:
        self.model = mlflow.pyfunc.load_model(self.model_dir)
        self.model_version = self.model.metadata.model_id or self.model.metadata.run_id or "unknown"
        # 모델마다 입력 열이 다르다 (v5 까지 4개, v6 부터 mem_growth 포함 5개). 모델의 입력 스키마를 따른다.
        # 11-27: 열을 4개로 고정해 두어 v8 승격 직후 모든 예측이 500 이었다 ("Model is missing inputs ['mem_growth']").
        schema = self.model.metadata.get_input_schema()
        names = [str(n) for n in schema.input_names()] if schema is not None else []
        self.columns = REQUIRED + [c for c in names if c not in REQUIRED]
        log.info("모델 로드 완료: %s (%s), 입력 열 %s", self.name, self.model_version, self.columns)
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
        # 필수 4개 외의 열(mem_growth 등)은 요청에 없으면 NaN 으로 둔다 (모델이 평소 값으로 본다)
        x = df.reindex(columns=self.columns).apply(pd.to_numeric, errors="coerce").astype("float64")
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

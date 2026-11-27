"""python -m anomaly <단계> [옵션]   (Argo Workflows 스텝에서 호출)"""

from __future__ import annotations

import argparse
import os

from . import steps


def main() -> None:
    p = argparse.ArgumentParser(prog="anomaly")
    sub = p.add_subparsers(dest="step", required=True)

    prefix_default = f"data/{os.environ.get('ARGO_WORKFLOW_NAME', 'local')}"

    e = sub.add_parser("extract", help="Prometheus → MinIO parquet")
    e.add_argument("--days", type=float, default=7.0)
    e.add_argument("--prefix", default=prefix_default)
    e.add_argument("--history-days", type=int, default=28, help="MinIO 에 쌓아 둔 데이터를 최근 며칠까지 쓸지 (0 이면 쌓지 않음)")

    t = sub.add_parser("train", help="IsolationForest 학습 → MLflow run")
    t.add_argument("--dataset", required=True)
    t.add_argument("--prefix", default=prefix_default)
    t.add_argument("--train-frac", type=float, default=0.7)
    t.add_argument("--quantile", type=float, default=0.999)
    t.add_argument("--z-limit", type=float, default=4.0)

    v = sub.add_parser("evaluate", help="평가 데이터로 precision/recall/F1, 기준선·champion 비교")
    v.add_argument("--run-id", required=True)
    v.add_argument("--test-set", required=True)
    v.add_argument("--min-f1", type=float, default=0.6)

    r = sub.add_parser("register", help="모델 레지스트리 등록, alias challenger")
    r.add_argument("--run-id", required=True)

    n = sub.add_parser("notify", help="학습 결과를 n8n(Slack) 으로 보낸다 (Argo onExit)")
    n.add_argument("--status", required=True)
    n.add_argument("--failures", default="")

    a = p.parse_args()
    if a.step == "extract":
        steps.extract(a.days, a.prefix, a.history_days)
    elif a.step == "train":
        steps.train(a.dataset, a.prefix, a.train_frac, a.quantile, a.z_limit)
    elif a.step == "evaluate":
        steps.evaluate(a.run_id, a.test_set, a.min_f1)
    elif a.step == "register":
        steps.register(a.run_id)
    elif a.step == "notify":
        steps.notify(a.status, a.failures)


if __name__ == "__main__":
    main()

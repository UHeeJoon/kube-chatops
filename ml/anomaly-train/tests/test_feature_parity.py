"""학습과 서빙(mcp-anomaly)이 같은 PromQL 로 특성을 읽는지 확인한다."""

import importlib.util
from pathlib import Path

from anomaly import data

MCP_FEATURES = Path(__file__).resolve().parents[3] / "services" / "mcp-anomaly" / "app" / "features.py"


def test_feature_queries_match_mcp_anomaly():
    spec = importlib.util.spec_from_file_location("mcp_features", MCP_FEATURES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.FEATURE_QUERIES == data.FEATURE_QUERIES

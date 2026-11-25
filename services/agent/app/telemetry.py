"""OpenTelemetry → Tempo (OTLP gRPC). 엔드포인트가 없으면 트레이싱을 끈다 (로컬 개발).

전역 TracerProvider 를 등록하면 anthropic SDK 가 Claude 호출 span 을 자동으로 만들고,
FastAPI 계측이 요청 span 을 만든다. 그래프 노드와 MCP 호출 span 은 코드에서 연다.
SDK 기본값은 프롬프트, 응답 본문을 span 에 넣지 않는다 (로그, 질문 내용이 Tempo 에 남지 않음).
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import settings


def setup_tracing() -> TracerProvider | None:
    if not settings.otel_exporter_otlp_endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": "agent", "service.namespace": "aiops"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    return provider

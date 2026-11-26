"""demo-app 에 일정한 부하를 보낸다.

이상 탐지 모델의 학습 데이터가 되므로 낮과 밤처럼 완만하게 변하는 부하를 만든다.

- 연결을 재사용하지 않는다: 연결 하나는 Service 뒤 파드 하나에 붙는다. 재사용하면 모든 요청이
  한 파드로만 가서 다른 파드는 장애를 넣어도 지표가 변하지 않는다 (11-26 누수 데이터 수집 때 겪음).
- 요청 급감 장애: POST :9101/chaos/drop?factor=0.05 → 부하를 factor 배로 줄인다. POST :9101/chaos/reset 로 해제.
  켜져 있는 동안 demo_chaos_active{kind="traffic_drop"} = 1 (이상 탐지 모델의 정답 라벨).
"""

import asyncio
import math
import os
import random
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from prometheus_client import Counter, Gauge, start_http_server

TARGET = os.getenv("TARGET_URL", "http://demo-app.demo.svc:8080")
BASE_RPS = float(os.getenv("BASE_RPS", "5"))
PERIOD_S = float(os.getenv("PERIOD_SECONDS", "3600"))  # 부하 주기
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "9101"))
SENT = Counter("loadgen_requests_total", "보낸 요청 수", ["result"])
CHAOS = Gauge("demo_chaos_active", "장애 주입 중이면 1", ["kind"])
CHAOS.labels("traffic_drop").set(0)
state = {"factor": 1.0}


def current_rps(now: float) -> float:
    wave = 1 + 0.5 * math.sin(2 * math.pi * now / PERIOD_S)
    return max(0.05, BASE_RPS * wave * random.uniform(0.9, 1.1) * state["factor"])


async def one(client: httpx.AsyncClient) -> None:
    try:
        r = await client.get(f"{TARGET}/api/orders/{random.randint(1, 10000)}")
        SENT.labels("ok" if r.status_code < 500 else "error").inc()
    except httpx.HTTPError:
        SENT.labels("fail").inc()


async def control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """아주 작은 HTTP 제어 엔드포인트 (클러스터 안에서 kubectl exec 로만 부른다)."""
    try:
        line = (await reader.readline()).decode()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        method, target, _ = line.split(" ", 2)
        url = urlsplit(target)
        status, body = "404 Not Found", "not found"
        if method == "POST" and url.path == "/chaos/drop":
            factor = float(parse_qs(url.query).get("factor", ["0.05"])[0])
            state["factor"] = min(max(factor, 0.0), 1.0)
            CHAOS.labels("traffic_drop").set(1)
            status, body = "200 OK", f'{{"traffic_drop": {state["factor"]}}}'
        elif method == "POST" and url.path == "/chaos/reset":
            state["factor"] = 1.0
            CHAOS.labels("traffic_drop").set(0)
            status, body = "200 OK", '{"traffic_drop": null}'
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
        await writer.drain()
    except (ValueError, ConnectionError):
        pass
    finally:
        writer.close()


async def main() -> None:
    start_http_server(9100)
    await asyncio.start_server(control, "0.0.0.0", CONTROL_PORT)
    # keepalive 0: 요청마다 새 연결 → Service 가 파드를 매번 고른다
    limits = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(timeout=10, limits=limits) as client:
        while True:
            rps = current_rps(time.time())
            if state["factor"] > 0:
                asyncio.create_task(one(client))
            await asyncio.sleep(1 / rps if state["factor"] > 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())

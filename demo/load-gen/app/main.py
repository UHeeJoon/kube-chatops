"""demo-app 에 일정한 부하를 보낸다.

이상 탐지 모델의 학습 데이터가 되므로 낮과 밤처럼 완만하게 변하는 부하를 만든다.
"""

import asyncio
import math
import os
import random
import time

import httpx
from prometheus_client import Counter, start_http_server

TARGET = os.getenv("TARGET_URL", "http://demo-app.demo.svc:8080")
BASE_RPS = float(os.getenv("BASE_RPS", "5"))
PERIOD_S = float(os.getenv("PERIOD_SECONDS", "3600"))  # 부하 주기
SENT = Counter("loadgen_requests_total", "보낸 요청 수", ["result"])


def current_rps(now: float) -> float:
    wave = 1 + 0.5 * math.sin(2 * math.pi * now / PERIOD_S)
    return max(0.5, BASE_RPS * wave * random.uniform(0.9, 1.1))


async def one(client: httpx.AsyncClient) -> None:
    try:
        r = await client.get(f"{TARGET}/api/orders/{random.randint(1, 10000)}")
        SENT.labels("ok" if r.status_code < 500 else "error").inc()
    except httpx.HTTPError:
        SENT.labels("fail").inc()


async def main() -> None:
    start_http_server(9100)
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            rps = current_rps(time.time())
            asyncio.create_task(one(client))
            await asyncio.sleep(1 / rps)


if __name__ == "__main__":
    asyncio.run(main())

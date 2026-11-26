# 학습 파이프라인 이미지. Argo Workflows 각 스텝이 python -m anomaly <단계> 로 실행한다.
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MLFLOW_DISABLE_AGENT_HINT=1 HOME=/tmp
COPY --from=build /install /usr/local
WORKDIR /app
COPY anomaly ./anomaly
USER 10001:10001
ENTRYPOINT ["python", "-m", "anomaly"]

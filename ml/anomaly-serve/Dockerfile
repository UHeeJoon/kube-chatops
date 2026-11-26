# KServe 커스텀 예측 서버. InferenceService 의 STORAGE_URI 에서 storage-initializer 가 /mnt/models 로 모델을 내려준다.
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MLFLOW_DISABLE_AGENT_HINT=1 HOME=/tmp
COPY --from=build /install /usr/local
WORKDIR /app
COPY server.py .
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "server.py"]

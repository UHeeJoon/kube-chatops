# MLflow tracking + model registry 서버. 공식 이미지에는 PostgreSQL 드라이버와 S3(boto3)가 없어 직접 만든다.
# 실행 인자(DB, 아티팩트 위치, 허용 Host)는 배포 저장소의 Deployment 에서 준다.
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MLFLOW_DISABLE_AGENT_HINT=1 HOME=/tmp
COPY --from=build /install /usr/local
USER 10001:10001
EXPOSE 5000
ENTRYPOINT ["mlflow", "server"]

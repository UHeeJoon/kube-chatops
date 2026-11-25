# 빌드 컨텍스트 두 개: 이 폴더(기본) + mcp-common (CI 의 build-contexts, 로컬은 --build-context mcp-common=services/mcp-common)
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /install /usr/local
WORKDIR /app
COPY --from=mcp-common mcp_common ./mcp_common
COPY app ./app
USER 10001:10001
EXPOSE 8080 9090
CMD ["python", "-m", "app.main"]

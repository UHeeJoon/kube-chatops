# mcp-runbook (Rust). 임베딩 모델(multilingual-e5-small)을 이미지에 넣는다 → 실행 중 외부로 나가지 않는다.
# Debian 13(trixie): 미리 빌드된 ONNX Runtime 이 glibc 2.38 이상을 요구한다 (bookworm 2.36 에서는 링크 실패)
FROM rust:1.95-trixie AS build
WORKDIR /src
# 의존성만 먼저 빌드해 층을 캐시한다 (소스만 바뀌면 아래 두 번째 빌드만 다시 돈다)
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && cargo build --release --locked && rm -rf src
COPY src ./src
RUN touch src/main.rs && cargo build --release --locked
# 모델을 받아 한 번 돌려 본다 (파일이 온전한지 빌드 때 확인)
RUN MODEL_DIR=/models ./target/release/mcp-runbook download-model

FROM gcr.io/distroless/cc-debian13:nonroot
COPY --from=build /src/target/release/mcp-runbook /usr/local/bin/mcp-runbook
COPY --from=build --chown=65532:65532 /models /models
# 런북은 이미지에 넣는다. 고치면 CI 가 다시 빌드·서명·배포하고, 서버가 뜰 때 바뀐 것만 다시 수집한다
COPY runbooks /runbooks
ENV MODEL_DIR=/models RUNBOOK_DIR=/runbooks HF_HUB_OFFLINE=1
USER 65532:65532
EXPOSE 8080 9090
ENTRYPOINT ["/usr/local/bin/mcp-runbook"]
CMD ["serve"]

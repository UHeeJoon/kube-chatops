//! CPU 임베딩 (fastembed, ONNX Runtime). 모델: multilingual-e5-small (384차원, 한국어 포함 다국어).
//!
//! e5 계열은 입력 앞에 역할을 붙여야 한다: 찾는 글은 "query: ", 저장하는 글은 "passage: ". 빼면 검색 품질이 떨어진다.
//! 모델 파일은 이미지에 들어 있다 (빌드 때 `download-model` 로 받음). 실행 중에는 내려받지 않는다.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, anyhow};
use fastembed::{EmbeddingModel, TextEmbedding, TextInitOptions};

/// db.rs 의 `vector(384)` 와 같아야 한다
pub const DIM: usize = 384;

#[derive(Clone)]
pub struct Embedder(Arc<Mutex<TextEmbedding>>);

impl Embedder {
    pub fn load(cache_dir: PathBuf, threads: usize) -> Result<Self> {
        let options = TextInitOptions::new(EmbeddingModel::MultilingualE5Small)
            .with_cache_dir(cache_dir)
            .with_show_download_progress(false)
            .with_intra_threads(threads);
        let model = TextEmbedding::try_new(options).context("임베딩 모델 로드 실패")?;
        Ok(Self(Arc::new(Mutex::new(model))))
    }

    pub async fn query(&self, text: &str) -> Result<Vec<f32>> {
        let mut out = self.run(vec![format!("query: {text}")]).await?;
        out.pop().ok_or_else(|| anyhow!("임베딩 결과가 비어 있다"))
    }

    pub async fn passages(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
        self.run(texts.iter().map(|t| format!("passage: {t}")).collect()).await
    }

    /// 추론은 CPU 를 오래 쓰므로 비동기 런타임 밖(blocking 스레드)에서 돌린다.
    async fn run(&self, inputs: Vec<String>) -> Result<Vec<Vec<f32>>> {
        let model = self.0.clone();
        let vectors = tokio::task::spawn_blocking(move || {
            let mut model = model.lock().map_err(|_| anyhow!("임베딩 모델 잠금 실패"))?;
            model.embed(inputs, Some(16)).map_err(|e| anyhow!("임베딩 실패: {e}"))
        })
        .await??;
        if vectors.iter().any(|v| v.len() != DIM) {
            return Err(anyhow!("임베딩 차원이 {DIM} 이 아니다"));
        }
        Ok(vectors)
    }
}

/// pgvector 입력 형식 `[0.1,0.2,...]`. SQL 에서 `$1::vector` 로 받는다.
pub fn to_pgvector(v: &[f32]) -> String {
    let mut s = String::with_capacity(v.len() * 10 + 2);
    s.push('[');
    for (i, x) in v.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push_str(&x.to_string());
    }
    s.push(']');
    s
}

#[cfg(test)]
mod tests {
    #[test]
    fn pgvector_text() {
        assert_eq!(super::to_pgvector(&[0.5, -1.0, 2.0]), "[0.5,-1,2]");
    }
}

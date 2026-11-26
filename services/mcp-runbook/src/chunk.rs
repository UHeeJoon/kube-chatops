//! 마크다운을 검색 단위(청크)로 나눈다.
//!
//! 제목(#, ##, ###) 단위로 자르고, 한 구역이 길면 문단 경계에서 다시 자른다.
//! 각 청크에는 문서 제목과 구역 제목을 붙여 둔다. 구역 본문만으로는 무엇에 대한 글인지 모르는 경우가 많아서다
//! (예: "## 확인" 아래의 명령 목록).

/// 청크 하나의 목표 길이 (글자 수). multilingual-e5-small 의 입력 한도는 512 토큰이라 한국어 기준 이 정도가 안전하다.
const MAX_CHARS: usize = 900;

#[derive(Debug, Clone, PartialEq)]
pub struct Chunk {
    pub heading: String,
    pub content: String,
}

/// 문서의 첫 `# 제목`. 없으면 None.
pub fn title_of(markdown: &str) -> Option<String> {
    markdown
        .lines()
        .find_map(|l| l.strip_prefix("# ").map(|t| t.trim().to_string()))
        .filter(|t| !t.is_empty())
}

pub fn split(markdown: &str) -> Vec<Chunk> {
    let mut out = Vec::new();
    let mut heading = String::new();
    let mut body = String::new();
    let mut in_code = false;
    for line in markdown.lines() {
        if line.trim_start().starts_with("```") {
            in_code = !in_code;
        }
        let is_heading = !in_code && (line.starts_with("# ") || line.starts_with("## ") || line.starts_with("### "));
        if is_heading {
            flush(&mut out, &heading, &body);
            heading = line.trim_start_matches('#').trim().to_string();
            body.clear();
        } else {
            body.push_str(line);
            body.push('\n');
        }
    }
    flush(&mut out, &heading, &body);
    out
}

fn flush(out: &mut Vec<Chunk>, heading: &str, body: &str) {
    let body = body.trim();
    if body.is_empty() {
        return;
    }
    let mut part = String::new();
    for para in body.split("\n\n") {
        if !part.is_empty() && part.chars().count() + para.chars().count() > MAX_CHARS {
            out.push(Chunk { heading: heading.to_string(), content: part.trim().to_string() });
            part.clear();
        }
        // 문단 하나가 한도를 넘으면 글자 수로 자른다 (긴 표, 긴 코드 블록)
        let mut rest: Vec<char> = para.chars().collect();
        while rest.len() > MAX_CHARS {
            let head: String = rest.drain(..MAX_CHARS).collect();
            out.push(Chunk { heading: heading.to_string(), content: head.trim().to_string() });
        }
        part.push_str(&rest.into_iter().collect::<String>());
        part.push_str("\n\n");
    }
    if !part.trim().is_empty() {
        out.push(Chunk { heading: heading.to_string(), content: part.trim().to_string() });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_by_heading_and_keeps_heading() {
        let md = "# 메모리 누수\n\n요약 문단.\n\n## 증상\n\n- 메모리가 계속 오른다\n\n## 확인\n\n```bash\n# 이건 제목이 아니다\nkubectl top pod\n```\n";
        let chunks = split(md);
        assert_eq!(title_of(md).as_deref(), Some("메모리 누수"));
        assert_eq!(chunks.len(), 3);
        assert_eq!(chunks[0].heading, "메모리 누수");
        assert_eq!(chunks[1].heading, "증상");
        assert_eq!(chunks[2].heading, "확인");
        assert!(chunks[2].content.contains("# 이건 제목이 아니다"));
    }

    #[test]
    fn long_section_is_split_under_limit() {
        let para = "가".repeat(400);
        let md = format!("## 긴 구역\n\n{para}\n\n{para}\n\n{para}\n\n{}", "나".repeat(2000));
        let chunks = split(&md);
        assert!(chunks.len() >= 4);
        assert!(chunks.iter().all(|c| c.content.chars().count() <= MAX_CHARS && c.heading == "긴 구역"));
    }

    #[test]
    fn empty_sections_are_dropped() {
        assert!(split("# 제목만\n\n## 빈 구역\n").is_empty());
    }
}

"""n8n 워크플로 JSON 생성기.

n8n 화면에서 만든 워크플로는 내보내기 JSON 이 길고 diff 를 읽기 어렵다. 그래서 흐름을 여기 코드로 적고
`python3 n8n/build_workflows.py` 로 `n8n/workflows/*.json` 을 만든다. 배포 저장소가 이 JSON 을 ConfigMap 으로
가져가고, n8n 파드가 시작할 때 init 컨테이너가 import 한다 (ID 가 고정이라 다시 import 하면 덮어쓴다).

- WF-01 intake: chat-gateway 이벤트 → 권한 확인 → agent /route → 조사(WF-02) 또는 바로 답장
- WF-02 investigate: WF-01 또는 Alertmanager → agent /investigate → 스레드에 보고서
- WF-99 audit: 각 흐름의 결과와 오류를 chat-gateway /audit 로 → Loki
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).parent / "workflows"
NS = uuid.UUID("6f1c9d2e-8a4b-4c3d-9e5f-0a1b2c3d4e5f")

AGENT = "http://agent.aiops.svc:8080"
GATEWAY = "http://chat-gateway.aiops.svc:8080"

# 자격 증명 ID 는 init 컨테이너가 Secret 으로 만드는 것과 같아야 한다 (배포 저장소 aiops/n8n)
CRED = {
    "agent": {"id": "kcagentapi000001", "name": "agent API"},
    "gateway": {"id": "kcgatewayapi0001", "name": "chat-gateway API"},
    "hook_chat": {"id": "kchookchat000001", "name": "webhook: chat-gateway"},
    "hook_alert": {"id": "kchookalert00001", "name": "webhook: alertmanager"},
}
WF = {"intake": "kcwf01intake0001", "investigate": "kcwf02investig01", "audit": "kcwf99audit00001"}

# Slack 사용자 ID → 역할. viewer: 조사와 조회, operator: viewer + 조치 승인 (Day 6).
# 목록에 없는 사용자의 요청은 거절하고 감사 로그만 남긴다.
ROLES: dict[str, str] = {
    "U0B330BGTJA": "operator",  # 프로젝트 운영자 (2026-11-26 등록)
}


def nid(wf: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{wf}/{name}"))


def node(wf: str, name: str, type_: str, version: float, pos: tuple[int, int], params: dict, **extra) -> dict:
    n = {"id": nid(wf, name), "name": name, "type": type_, "typeVersion": version, "position": list(pos), "parameters": params}
    n.update(extra)
    return n


def http(wf: str, name: str, pos, method: str, url: str, body: str, cred: str, timeout_ms: int = 30000) -> dict:
    """HTTP 호출. 응답 코드가 2xx 가 아니어도 멈추지 않고 {statusCode, body} 로 넘겨 다음 노드가 판단한다."""
    return node(
        wf, name, "n8n-nodes-base.httpRequest", 4.2, pos,
        {
            "method": method,
            "url": url,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"timeout": timeout_ms, "response": {"response": {"fullResponse": True, "neverError": True}}},
        },
        credentials={"httpHeaderAuth": CRED[cred]},
    )


def code(wf: str, name: str, pos, js: str) -> dict:
    return node(wf, name, "n8n-nodes-base.code", 2, pos, {"jsCode": js.strip()})


def if_true(wf: str, name: str, pos, expr: str) -> dict:
    return node(
        wf, name, "n8n-nodes-base.if", 2.2, pos,
        {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{"id": nid(wf, name + "/c"), "leftValue": expr, "rightValue": "",
                                "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                "combinator": "and",
            },
            "options": {},
        },
    )


def call_audit(wf: str, name: str, pos) -> dict:
    return node(
        wf, name, "n8n-nodes-base.executeWorkflow", 1.2, pos,
        {"workflowId": {"__rl": True, "value": WF["audit"], "mode": "id"}, "options": {"waitForSubWorkflow": False}},
    )


def webhook(wf: str, name: str, pos, path: str, cred: str) -> dict:
    return node(
        wf, name, "n8n-nodes-base.webhook", 2, pos,
        {"httpMethod": "POST", "path": path, "authentication": "headerAuth", "responseMode": "onReceived", "options": {}},
        credentials={"httpHeaderAuth": CRED[cred]},
        webhookId=nid(wf, name + "/hook"),
    )


def links(*pairs) -> dict:
    """(from, to) 또는 (from, to, from 의 출력 번호). IF 는 0 = 참, 1 = 거짓."""
    out: dict = {}
    for p in pairs:
        src, dst, idx = (*p, 0) if len(p) == 2 else p
        conns = out.setdefault(src, {"main": []})["main"]
        while len(conns) <= idx:
            conns.append([])
        conns[idx].append({"node": dst, "type": "main", "index": 0})
    return out


def workflow(wid: str, name: str, nodes: list, connections: dict, error_wf: bool = True) -> dict:
    settings = {"executionOrder": "v1", "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"}
    if error_wf:
        settings["errorWorkflow"] = WF["audit"]
    return {"id": wid, "name": name, "active": True, "nodes": nodes, "connections": connections,
            "settings": settings, "pinData": {}, "tags": []}


# ---------------- WF-01 intake ----------------
W = WF["intake"]
PREPARE = f"""
// chat-gateway 가 보낸 공통 이벤트 JSON (body). 권한을 확인하고 다음 단계를 정한다.
const ROLES = {json.dumps(ROLES)};
const ev = $json.body ?? {{}};
const role = ROLES[ev.user_id] ?? 'none';
let mode = 'route', reply = '';
if (role === 'none') {{
  mode = 'reply';
  reply = `권한이 없습니다. 운영자에게 사용자 ID \\`${{ev.user_id}}\\` 등록을 요청하세요.`;
}} else if (ev.type === 'button_click') {{
  mode = 'reply';
  reply = '승인 버튼 처리는 아직 준비 중입니다.';
}} else if (!(ev.text ?? '').trim()) {{
  mode = 'reply';
  reply = '무엇을 도와드릴까요? 예: "주문 조회가 느려졌어요, 원인 확인해줘"';
}}
return [{{ json: {{ ...ev, role, mode, reply, started_at: Date.now() }} }}];
"""
DECIDE = """
// /route 결과로 다음 단계를 정한다. investigate → 조사 보고서, query → 짧은 답(answer 모드). 나머지는 바로 답장.
const ev = $('준비').item.json;
const res = $json;
let next = 'reply', reply = '', intent = 'error', mode = 'investigate';
if (res.statusCode === 200) {
  intent = res.body.intent;
  if (intent === 'investigate' || intent === 'query') next = 'investigate';
  if (intent === 'query') mode = 'answer';
  else if (intent === 'action_request') reply = '조치 실행은 조사 보고서의 승인 버튼으로만 할 수 있습니다 (승인 흐름 준비 중). 먼저 무엇이 문제인지 물어봐 주세요.';
  else if (intent === 'mlops') reply = '재학습과 모델 승격은 MLOps 단계에서 붙습니다.';
  else reply = res.body.reply || '네.';
} else if (res.statusCode === 429) {
  reply = '오늘 AI 사용 예산을 다 썼습니다. 내일 다시 시도해 주세요.';
} else {
  reply = `요청을 처리하지 못했습니다 (agent ${res.statusCode ?? '연결 실패'}).`;
}
return [{ json: { ...ev, intent, next, reply, mode, question: ev.text, route_cost: res.body?.usage?.cost_usd ?? 0 } }];
"""
AUDIT_REPLY = """
return [{ json: { flow: 'wf-01-intake', result: $('답장 정리').item.json.result, user_id: $('답장 정리').item.json.user_id,
  role: $('답장 정리').item.json.role, channel_id: $('답장 정리').item.json.channel_id, thread_id: $('답장 정리').item.json.thread_id,
  intent: $('답장 정리').item.json.intent ?? null, slack_status: $json.statusCode, execution_id: $execution.id } }];
"""
REPLY_PREP = """
// 두 갈래(권한/형식 답장, /route 답장)에서 온 항목을 같은 모양으로
const it = $json;
const result = it.role === 'none' ? 'denied' : (it.intent ? `reply:${it.intent}` : 'reply');
return [{ json: { ...it, result } }];
"""
intake = workflow(W, "WF-01 intake (Slack 이벤트)", [
    webhook(W, "Slack 이벤트", (0, 300), "chat", "hook_chat"),
    code(W, "준비", (220, 300), PREPARE),
    if_true(W, "분류 필요?", (440, 300), "={{ $json.mode === 'route' }}"),
    http(W, "의도 분류", (660, 200), "POST", f"{AGENT}/route", "={{ JSON.stringify({ text: $json.text }) }}", "agent"),
    code(W, "다음 단계", (880, 200), DECIDE),
    if_true(W, "조사?", (1100, 200), "={{ $json.next === 'investigate' }}"),
    node(W, "조사 시작 (WF-02)", "n8n-nodes-base.executeWorkflow", 1.2, (1320, 100),
         {"workflowId": {"__rl": True, "value": WF["investigate"], "mode": "id"}, "options": {"waitForSubWorkflow": False}}),
    code(W, "답장 정리", (1320, 400), REPLY_PREP),
    http(W, "답장", (1540, 400), "POST", f"{GATEWAY}/slack/post",
         "={{ JSON.stringify({ channel: $json.channel_id, thread_ts: $json.thread_id, text: $json.reply }) }}", "gateway"),
    code(W, "감사 기록", (1760, 400), AUDIT_REPLY),
    call_audit(W, "감사 (WF-99)", (1980, 400)),
], links(
    ("Slack 이벤트", "준비"), ("준비", "분류 필요?"),
    ("분류 필요?", "의도 분류", 0), ("분류 필요?", "답장 정리", 1),
    ("의도 분류", "다음 단계"), ("다음 단계", "조사?"),
    ("조사?", "조사 시작 (WF-02)", 0), ("조사?", "답장 정리", 1),
    ("답장 정리", "답장"), ("답장", "감사 기록"), ("감사 기록", "감사 (WF-99)"),
))

# ---------------- WF-02 investigate ----------------
W = WF["investigate"]
ALERT_PREP = """
// Alertmanager webhook (그룹 하나 = 요청 하나). firing 알림만 조사한다.
const g = $json.body ?? {};
const firing = (g.alerts ?? []).filter(a => a.status === 'firing');
if (!firing.length) return [];
const a = firing[0];
const name = a.labels?.alertname ?? 'alert';
const summary = a.annotations?.summary ?? '';
const lines = firing.map(x => `• ${x.labels?.alertname} ${x.labels?.pod ?? x.labels?.service ?? ''} ${x.annotations?.summary ?? ''}`.trim());
return [{ json: {
  title: `:rotating_light: *${name}* (${firing.length}건) ${summary}`,
  detail: lines.join('\\n').slice(0, 2500),
  question: `알림 '${name}' 이(가) 발생했다. ${summary} 원인을 조사해줘.`,
  alert: { status: g.status, commonLabels: g.commonLabels, commonAnnotations: g.commonAnnotations,
           alerts: firing.slice(0, 5).map(x => ({ labels: x.labels, annotations: x.annotations, startsAt: x.startsAt })) },
} }];
"""
ALERT_THREAD = """
const p = $('알림 정리').item.json;
if ($json.statusCode !== 200) throw new Error(`알림 글 올리기 실패: ${$json.statusCode} ${JSON.stringify($json.body).slice(0, 200)}`);
return [{ json: { channel_id: $json.body.channel, thread_id: $json.body.ts, question: p.question, alert: p.alert,
  user_id: 'alertmanager', role: 'system', source: 'alert', started_at: Date.now() } }];
"""
REQ = """
// 두 입구(WF-01, 알림)를 같은 모양으로. agent 의 thread_id 는 Slack 채널 + 스레드 → 후속 질문이 이어진다.
const it = $json;
return [{ json: { channel_id: it.channel_id, thread_id: it.thread_id, question: it.question, alert: it.alert ?? null,
  mode: it.mode === 'answer' ? 'answer' : 'investigate', route_cost: it.route_cost ?? 0,
  user_id: it.user_id, role: it.role, source: it.source ?? 'chat', started_at: it.started_at ?? Date.now(),
  agent_thread: `slack-${it.channel_id}-${it.thread_id}` } }];
"""
REPORT = r"""
// agent 보고서 → Slack 블록
const req = $('조사 요청').item.json;
const res = $json;
const secs = Math.round((Date.now() - req.started_at) / 1000);
const clip = (s, n) => (s ?? '').length > n ? s.slice(0, n - 1) + '…' : (s ?? '');
let blocks = [], text, result;
const u0 = res.body?.usage ?? {};
const cost = (u0.cost_usd ?? 0) + (req.route_cost ?? 0);  // 의도 분류 비용까지 합친 요청 하나의 비용
const llm = (u0.llm_calls ?? 0) + (req.route_cost ? 1 : 0);
if (res.statusCode === 200 && res.body?.mode === 'answer' && res.body?.answer) {
  // 단순 조회: 모델의 답을 그대로, 아래에 한 줄 정보만
  result = 'answer';
  text = clip(res.body.answer, 300);
  blocks.push({ type: 'section', text: { type: 'mrkdwn', text: clip(res.body.answer, 2900) } });
  blocks.push({ type: 'context', elements: [{ type: 'mrkdwn',
    text: `도구 ${res.body.tool_calls ?? 0}회 · LLM ${llm}회 · $${cost.toFixed(3)} · ${secs}초` }] });
} else if (res.statusCode === 200 && res.body?.report) {
  const r = res.body.report, u = res.body.usage ?? {};
  result = 'ok';
  text = clip(r.summary, 300);
  blocks.push({ type: 'section', text: { type: 'mrkdwn', text: clip(`*조사 결과*\n${r.summary}`, 2900) } });
  if (r.causes?.length) {
    const lines = r.causes.map((c, i) => `${i + 1}. *${c.title}* (신뢰도 ${Math.round((c.confidence ?? 0) * 100)}%)\n` +
      (c.evidence ?? []).slice(0, 4).map(e => `    · ${e}`).join('\n'));
    blocks.push({ type: 'section', text: { type: 'mrkdwn', text: clip(`*원인 후보*\n${lines.join('\n')}`, 2900) } });
  }
  const acts = (r.actions ?? []).filter(a => a.type !== 'none');
  if (acts.length) {
    const lines = acts.map(a => `• \`${a.type}\` ${a.target} (위험: ${a.risk}) ${a.reason}`);
    blocks.push({ type: 'section', text: { type: 'mrkdwn', text: clip(`*제안 조치* (실행하지 않았습니다)\n${lines.join('\n')}`, 2900) } });
  }
  if (r.data_gaps?.length) {
    blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: clip(`더 확인할 것: ${r.data_gaps.join(' / ')}`, 2900) }] });
  }
  blocks.push({ type: 'context', elements: [{ type: 'mrkdwn',
    text: `도구 ${res.body.tool_calls ?? 0}회 · LLM ${llm}회 · $${cost.toFixed(3)} · ${secs}초` +
          (res.body.stopped_by && res.body.stopped_by !== 'done' ? ` · 한도 도달로 중단(${res.body.stopped_by})` : '') }] });
} else if (res.statusCode === 429) {
  result = 'budget'; text = '오늘 AI 사용 예산을 다 써서 조사하지 못했습니다.';
} else {
  result = 'error'; text = `조사에 실패했습니다 (agent ${res.statusCode ?? '연결 실패'}). 잠시 후 다시 시도해 주세요.`;
}
return [{ json: { channel: req.channel_id, ts: $('조사 중 표시').item.json.body?.ts, text, blocks: blocks.length ? blocks : null,
  audit: { flow: 'wf-02-investigate', result, source: req.source, user_id: req.user_id, role: req.role,
    channel_id: req.channel_id, thread_id: req.thread_id, seconds: secs, agent_status: res.statusCode,
    mode: req.mode, cost_usd: cost, tool_calls: res.body?.tool_calls ?? 0, stopped_by: res.body?.stopped_by ?? null } } }];
"""
AUDIT_INV = """
return [{ json: { ...$('보고서 만들기').item.json.audit, slack_status: $json.statusCode, execution_id: $execution.id } }];
"""
investigate = workflow(W, "WF-02 investigate (조사와 보고)", [
    node(W, "WF-01 에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 100), {"inputSource": "passthrough"}),
    webhook(W, "Alertmanager", (0, 400), "alert", "hook_alert"),
    code(W, "알림 정리", (220, 400), ALERT_PREP),
    http(W, "알림 글 올리기", (440, 400), "POST", f"{GATEWAY}/slack/post",
         "={{ JSON.stringify({ text: $json.title + '\\n' + $json.detail }) }}", "gateway"),
    code(W, "알림 스레드", (660, 400), ALERT_THREAD),
    code(W, "조사 요청", (880, 200), REQ),
    http(W, "조사 중 표시", (1100, 200), "POST", f"{GATEWAY}/slack/post",
         "={{ JSON.stringify({ channel: $json.channel_id, thread_ts: $json.thread_id, text: $json.mode === 'answer' ? ':hourglass_flowing_sand: 확인하고 있습니다.' : ':mag: 조사하고 있습니다. 보통 30~60초 걸립니다.' }) }}",
         "gateway"),
    http(W, "조사", (1320, 200), "POST", f"{AGENT}/investigate",
         "={{ JSON.stringify({ thread_id: $('조사 요청').item.json.agent_thread, question: $('조사 요청').item.json.question, alert: $('조사 요청').item.json.alert, mode: $('조사 요청').item.json.mode }) }}",
         "agent", timeout_ms=240000),
    code(W, "보고서 만들기", (1540, 200), REPORT),
    http(W, "결과 올리기", (1760, 200), "POST", f"{GATEWAY}/slack/update",
         "={{ JSON.stringify({ channel: $json.channel, ts: $json.ts, text: $json.text, blocks: $json.blocks }) }}", "gateway"),
    code(W, "감사 기록", (1980, 200), AUDIT_INV),
    call_audit(W, "감사 (WF-99)", (2200, 200)),
], links(
    ("WF-01 에서 호출", "조사 요청"),
    ("Alertmanager", "알림 정리"), ("알림 정리", "알림 글 올리기"), ("알림 글 올리기", "알림 스레드"), ("알림 스레드", "조사 요청"),
    ("조사 요청", "조사 중 표시"), ("조사 중 표시", "조사"), ("조사", "보고서 만들기"), ("보고서 만들기", "결과 올리기"),
    ("결과 올리기", "감사 기록"), ("감사 기록", "감사 (WF-99)"),
))

# ---------------- WF-99 audit ----------------
W = WF["audit"]
ON_ERROR = """
// 다른 워크플로가 실패하면 여기로 온다 (워크플로 설정의 Error Workflow)
const e = $json;
return [{ json: { flow: e.workflow?.name ?? 'unknown', result: 'error', execution_id: e.execution?.id ?? null,
  failed_node: e.execution?.lastNodeExecuted ?? null, error: (e.execution?.error?.message ?? '').slice(0, 500) } }];
"""
audit = workflow(W, "WF-99 audit (감사 로그)", [
    node(W, "다른 흐름에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 100), {"inputSource": "passthrough"}),
    node(W, "흐름 실패", "n8n-nodes-base.errorTrigger", 1, (0, 300), {}),
    code(W, "오류 정리", (220, 300), ON_ERROR),
    http(W, "감사 로그 보내기", (440, 200), "POST", f"{GATEWAY}/audit",
         "={{ JSON.stringify({ ...$json, at: new Date().toISOString() }) }}", "gateway", timeout_ms=10000),
], links(("다른 흐름에서 호출", "감사 로그 보내기"), ("흐름 실패", "오류 정리"), ("오류 정리", "감사 로그 보내기")), error_wf=False)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for wf, fname in ((intake, "wf-01-intake.json"), (investigate, "wf-02-investigate.json"), (audit, "wf-99-audit.json")):
        (OUT / fname).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n")
        print(fname, len(wf["nodes"]), "nodes")

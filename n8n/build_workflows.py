"""n8n 워크플로 JSON 생성기.

n8n 화면에서 만든 워크플로는 내보내기 JSON 이 길고 diff 를 읽기 어렵다. 그래서 흐름을 여기 코드로 적고
`python3 n8n/build_workflows.py` 로 `n8n/workflows/*.json` 을 만든다. 배포 저장소가 이 JSON 을 ConfigMap 으로
가져가고, n8n 파드가 시작할 때 init 컨테이너가 import 한다 (ID 가 고정이라 다시 import 하면 덮어쓴다).

- WF-01 intake: chat-gateway 이벤트 → 권한 확인 → agent /route → 조사(WF-02) 또는 바로 답장
- WF-02 investigate: WF-01 또는 Alertmanager → (보안 알림이면 먼저 파드 격리) → agent /investigate → 스레드에 보고서
- WF-03 actions: Slack 조치 버튼 → operator 확인 → 실행 (격리 파드 삭제, 재시작, 증설·롤백 커밋)
- WF-04 mlops: 학습 결과 카드(Argo onExit → webhook), 모델 상태, 재학습 버튼, 승격 버튼 → 배포 저장소 커밋 → 반영 확인 → champion
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
MLFLOW = "http://mlflow.mlops.svc:5000"
MLFLOW_UI = "http://mlflow.ascode.click"
MODEL_PREDICT = "http://demo-anomaly-predictor.mlops.svc/v1/models/demo-anomaly:predict"
K8S = "https://kubernetes.default.svc"  # CA 는 n8n 의 NODE_EXTRA_CA_CERTS (kube-root-ca.crt)
GITHUB_FILE = "https://api.github.com/repos/UHeeJoon/kube-chatops-deploy/contents/mlops/anomaly-serve/inferenceservice.yaml"
MODEL_NAME = "demo-anomaly"

# 자격 증명 ID 는 init 컨테이너가 Secret 으로 만드는 것과 같아야 한다 (배포 저장소 aiops/n8n)
CRED = {
    "agent": {"id": "kcagentapi000001", "name": "agent API"},
    "gateway": {"id": "kcgatewayapi0001", "name": "chat-gateway API"},
    "hook_chat": {"id": "kchookchat000001", "name": "webhook: chat-gateway"},
    "hook_alert": {"id": "kchookalert00001", "name": "webhook: alertmanager"},
    "hook_mlops": {"id": "kchookmlops00001", "name": "webhook: 학습 파이프라인"},
    "k8s": {"id": "kck8smlops000001", "name": "kubernetes: mlops 워크플로 제출"},
    "github": {"id": "kcgithubdeploy01", "name": "github: kube-chatops-deploy"},
    "k8s_actions": {"id": "kck8sactions0001", "name": "kubernetes: demo 보안 조치"},
}
WF = {"intake": "kcwf01intake0001", "investigate": "kcwf02investig01", "actions": "kcwf03actions001", "mlops": "kcwf04mlops00001",
      "audit": "kcwf99audit00001"}
QUARANTINE_LABEL = "security.kube-chatops/quarantine"  # 이 라벨이 붙은 파드는 demo/security 의 CiliumNetworkPolicy 가 모든 통신을 막는다

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


def http(wf: str, name: str, pos, method: str, url: str, body: str | None, cred: str | None, timeout_ms: int = 30000,
         headers: dict[str, str] | None = None) -> dict:
    """HTTP 호출. 응답 코드가 2xx 가 아니어도 멈추지 않고 {statusCode, body} 로 넘겨 다음 노드가 판단한다.

    body 가 None 이면 본문 없이(GET), cred 가 None 이면 인증 없이(클러스터 안 MLflow, 예측 서버) 부른다.
    """
    params: dict = {"method": method, "url": url}
    if cred:
        params |= {"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"}
    if headers:
        params |= {"sendHeaders": True, "headerParameters": {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}}
    if body is not None:
        params |= {"sendBody": True, "specifyBody": "json", "jsonBody": body}
    params["options"] = {"timeout": timeout_ms, "response": {"response": {"fullResponse": True, "neverError": True}}}
    extra = {"credentials": {"httpHeaderAuth": CRED[cred]}} if cred else {}
    return node(wf, name, "n8n-nodes-base.httpRequest", 4.2, pos, params, **extra)


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
}} else if (ev.type === 'button_click' && String(ev.action?.id ?? '').startsWith('mlops_')) {{
  mode = 'mlops';  // 재학습·승격 버튼 → WF-04 (역할 확인은 WF-04 가 한다)
}} else if (ev.type === 'button_click' && /^(sec|act)_/.test(String(ev.action?.id ?? ''))) {{
  mode = 'security';  // 조치 버튼(격리 파드 삭제, 재시작·롤백·증설 승인) → WF-03 (역할 확인은 WF-03 가 한다)
}} else if (ev.type === 'button_click') {{
  mode = 'reply';
  reply = '승인 버튼 처리는 아직 준비 중입니다.';
}} else if ((t => t.length <= 20 && /^(사건|보고서|incidents?)(\\s|$)/i.test(t) && (/목록|기록|list|\\d/i.test(t) || /^\\S+$/.test(t)))((ev.text ?? '').trim())) {{
  // "사건 목록", "사건 목록 조회", "사건 12 보여줘" 처럼 뒤에 말이 붙거나 오타가 있어도 조회로 본다 (짧은 글만)
  mode = 'incident';  // 사건 기록 조회 (agent DB, LLM 을 부르지 않는다)
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
  else if (intent === 'mlops') next = 'mlops';
  else if (intent === 'action_request') reply = '조치는 조사 보고서에 붙는 승인 버튼으로만 실행합니다 (말로 요청한 조치는 실행하지 않습니다). 먼저 무엇이 문제인지 물어봐 주세요. 예: demo-app 왜 재시작해?';
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
INCIDENT_URL = """={{ (() => { const m = String($json.text ?? '').match(/\\d+/); return 'AGENT_BASE/incidents' + (m ? '/' + m[0] : '?limit=10'); })() }}"""
INCIDENT_REPLY = """
// 사건 목록 또는 사건 하나(마크다운)를 Slack 글로
const ev = $('준비').item.json;
const res = $json;
const kst = t => new Date(t).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const toSlack = t => String(t ?? '').replace(/\\*\\*(.+?)\\*\\*/g, '*$1*').replace(/^#{1,6}\\s*(.+)$/gm, '*$1*').replace(/^\\s*- /gm, '• ');
let reply;
if (res.statusCode === 404) reply = '그 번호의 사건이 없습니다. `사건 목록` 으로 번호를 확인하세요.';
else if (res.statusCode !== 200) reply = `사건 기록을 읽지 못했습니다 (agent ${res.statusCode ?? '연결 실패'}).`;
else if (res.body.markdown) {
  const md = toSlack(res.body.markdown);
  reply = md.length > 3800 ? md.slice(0, 3800) + '\\n…(길어서 줄임)' : md;
} else {
  const sev = { critical: ':red_circle:', warning: ':large_yellow_circle:', ok: ':large_green_circle:' };
  const rows = (res.body.incidents ?? []).map(i =>
    `${sev[i.severity] ?? ':white_circle:'} *#${i.id}* ${kst(i.opened_at)} · \\`${i.namespace}\\` · ${String(i.title).slice(0, 80)}\\n      ◦ ${(i.alert_names ?? []).join(', ') || 'Slack 질문'} · 조사 ${i.report_count}회 · $${Number(i.cost_usd).toFixed(2)}`);
  reply = rows.length ? `*최근 사건* (자세히: \\`사건 번호\\`)\\n${rows.join('\\n')}` : '기록된 사건이 없습니다.';
}
return [{ json: { ...ev, reply, intent: 'incident' } }];
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
    if_true(W, "사건 조회?", (660, 800), "={{ $json.mode === 'incident' }}"),
    http(W, "사건 기록", (880, 800), "GET", INCIDENT_URL.replace("AGENT_BASE", AGENT), None, "agent"),
    code(W, "사건 답장", (1100, 800), INCIDENT_REPLY),
    if_true(W, "보안 조치?", (660, 550), "={{ $json.mode === 'security' }}"),
    node(W, "조치 (WF-03)", "n8n-nodes-base.executeWorkflow", 1.2, (880, 450),
         {"workflowId": {"__rl": True, "value": WF["actions"], "mode": "id"}, "options": {"waitForSubWorkflow": False}}),
    if_true(W, "MLOps?", (1100, 550), "={{ $json.mode === 'mlops' || $json.next === 'mlops' }}"),
    node(W, "MLOps (WF-04)", "n8n-nodes-base.executeWorkflow", 1.2, (1320, 650),
         {"workflowId": {"__rl": True, "value": WF["mlops"], "mode": "id"}, "options": {"waitForSubWorkflow": False}}),
    http(W, "의도 분류", (660, 200), "POST", f"{AGENT}/route", "={{ JSON.stringify({ text: $json.text, thread_id: `slack-${$('준비').item.json.channel_id}-${$('준비').item.json.thread_id}` }) }}", "agent"),
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
    ("분류 필요?", "의도 분류", 0), ("분류 필요?", "사건 조회?", 1),
    ("사건 조회?", "사건 기록", 0), ("사건 조회?", "보안 조치?", 1), ("사건 기록", "사건 답장"), ("사건 답장", "답장 정리"),
    ("보안 조치?", "조치 (WF-03)", 0), ("보안 조치?", "MLOps?", 1),
    ("의도 분류", "다음 단계"), ("다음 단계", "조사?"),
    ("조사?", "조사 시작 (WF-02)", 0), ("조사?", "MLOps?", 1),
    ("MLOps?", "MLOps (WF-04)", 0), ("MLOps?", "답장 정리", 1),
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
// 알림의 라벨·주석에는 공격자가 만든 글이 들어올 수 있다 (요청 경로, 파드 이름). Slack 이 해석하는 표기(<!channel>, <@U..>, <주소|글자>)를 뺀다
const plain = v => String(v ?? '').replace(/<[^>]*>/g, ' ').slice(0, 300);
const name = plain(a.labels?.alertname ?? 'alert');
const summary = plain(a.annotations?.summary);
const kst = t => new Date(t).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false });
// 알림이 하나면 한 줄, 여러 개면 대상별 목록
const head = `:rotating_light: *${name}* · ${summary}`;
const more = firing.length > 1
  ? '\\n' + firing.slice(0, 5).map(x => `• ${plain(x.labels?.pod ?? x.labels?.service ?? x.labels?.namespace)} ${plain(x.annotations?.summary)}`.trim()).join('\\n')
  : '';
const meta = `\\n_${a.labels?.namespace ?? ''} · ${a.labels?.severity ?? ''} · ${kst(a.startsAt)} 시작 · 이 스레드에 자동 조사 결과가 달립니다_`;
// 선 조치: auto_action=quarantine 알림이면 가장 최근에 시작된 대상 파드 하나를 격리한다 (demo 만)
const target = firing.filter(x => x.labels?.auto_action === 'quarantine' && /^[a-z0-9.-]{1,253}$/.test(String(x.labels?.pod ?? '')) && x.labels?.namespace === 'demo')
  .sort((x, y) => String(y.startsAt).localeCompare(String(x.startsAt)))[0];
return [{ json: {
  quarantine: target ? { namespace: target.labels.namespace, pod: target.labels.pod, reason: name } : null,
  title: head + more + meta,
  question: `알림 '${name}' 이(가) 발생했다. ${summary} 원인을 조사해줘.`,
  alert: { status: g.status, commonLabels: g.commonLabels, commonAnnotations: g.commonAnnotations,
           alerts: firing.slice(0, 5).map(x => ({ labels: x.labels, annotations: x.annotations, startsAt: x.startsAt })) },
} }];
"""
QUARANTINE_PLAN = """
// 격리 계획: 파드의 라벨을 전부 떼고(→ Deployment·Service 에서 빠지고 새 파드가 대신 뜸) 격리 라벨을 붙인다.
// 떼어 낸 라벨은 어노테이션에 남겨 어느 앱이었는지 알 수 있게 한다. 파드는 지우지 않는다 (증거 보존).
const q = $('알림 정리').item.json.quarantine;
const res = $('파드 조회').item.json;
// 상한: 이미 격리된 파드가 많으면 더 격리하지 않는다. 격리하면 새 파드가 대신 뜨므로, 새 파드마다 걸리는 오탐이면 격리가 끝없이 쌓인다.
// 목록 조회가 실패하면 상한 없이 진행한다 (격리 자체가 안전 조치라 막는 쪽보다 하는 쪽이 낫다).
const MAX_QUARANTINED = 3;
const held = $json.statusCode === 200 ? ($json.body?.items ?? []).length : 0;
if (held >= MAX_QUARANTINED) return [{ json: { ...q, do: false, note: `격리된 파드가 이미 ${held}개라 자동 격리를 멈춤 (상한 ${MAX_QUARANTINED}). 같은 탐지가 새 파드마다 반복되는지 사람이 확인 필요` } }];
if (res.statusCode === 404) return [{ json: { ...q, do: false, note: '파드가 이미 없음' } }];
if (res.statusCode !== 200) return [{ json: { ...q, do: false, note: `파드 조회 실패 (${res.statusCode})` } }];
const labels = res.body.metadata?.labels ?? {};
if (labels['__QL__'] === 'true') return [{ json: { ...q, do: false, already: true, note: '이미 격리됨' } }];
const patch = { metadata: {
  labels: { ...Object.fromEntries(Object.keys(labels).map(k => [k, null])), '__QL__': 'true' },
  annotations: { 'security.kube-chatops/original-labels': JSON.stringify(labels),
                 'security.kube-chatops/quarantined-at': new Date().toISOString(),
                 'security.kube-chatops/reason': q.reason } } };
return [{ json: { ...q, do: true, app: labels['app.kubernetes.io/name'] ?? null, node: res.body.spec?.nodeName ?? null, patch } }];
""".replace("__QL__", QUARANTINE_LABEL)
QUARANTINE_DONE = r"""
// 격리 결과를 알림 글과 조사 질문에 넣는다
const p = $('알림 정리').item.json;
const plan = $('격리 계획').item.json;
let q = { ...p.quarantine, done: false, note: plan.note ?? '' };
if (plan.do) q = { ...q, app: plan.app, node: plan.node, done: $json.statusCode === 200,
  note: $json.statusCode === 200 ? '격리함' : `격리 실패 (${$json.statusCode} ${JSON.stringify($json.body?.message ?? '').slice(0, 120)})` };
else if (plan.already) q.done = true;
const line = q.done
  ? `
:lock: *선 조치: 파드 격리* \`${q.pod}\` · 모든 통신 차단, 서비스에서 제외 (새 파드가 대신 뜸), 파드는 증거로 남김`
  : `
:warning: 격리하지 못함: \`${q.pod}\` · ${q.note}`;
const question = p.question + (q.done
  ? ` 자동 조치로 파드 ${q.pod} 를 이미 격리했다 (라벨을 떼어 서비스에서 제외, 네트워크 전부 차단, 파드는 남김). 격리 전 공격 흔적과 영향 범위를 조사해줘.`
  : ` 파드 ${q.pod} 자동 격리는 실패했다: ${q.note}.`);
return [{ json: { ...p, title: p.title + line, question, quarantine: q } }];
"""
ALERT_THREAD = """
const p = $('격리 반영').isExecuted ? $('격리 반영').item.json : $('알림 정리').item.json;
if ($json.statusCode !== 200) throw new Error(`알림 글 올리기 실패: ${$json.statusCode} ${JSON.stringify($json.body).slice(0, 200)}`);
return [{ json: { channel_id: $json.body.channel, thread_id: $json.body.ts, question: p.question, alert: p.alert,
  quarantine: p.quarantine ?? null, user_id: 'alertmanager', role: 'system', source: 'alert', started_at: Date.now() } }];
"""
REQ = """
// 두 입구(WF-01, 알림)를 같은 모양으로. agent 의 thread_id 는 Slack 채널 + 스레드 → 후속 질문이 이어진다.
const it = $json;
return [{ json: { channel_id: it.channel_id, thread_id: it.thread_id, question: it.question, alert: it.alert ?? null,
  mode: it.mode === 'answer' ? 'answer' : 'investigate', route_cost: it.route_cost ?? 0,
  user_id: it.user_id, role: it.role, source: it.source ?? 'chat', started_at: it.started_at ?? Date.now(),
  quarantine: it.quarantine ?? null,
  agent_thread: `slack-${it.channel_id}-${it.thread_id}` } }];
"""
REPORT = r"""
// agent 결과 → Slack 메시지 (mrkdwn). 보고서는 상태 한 줄 + 영향/원인/조치/미확인 목록, 질문 답은 그대로.
const req = $('조사 요청').item.json;
const res = $json;
const secs = Math.round((Date.now() - req.started_at) / 1000);
const clip = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const list = (xs, n, w) => (xs ?? []).slice(0, n).map(x => `• ${clip(x, w)}`).join('\n');
// 모델이 일반 마크다운을 쓰면 Slack 형식으로 바꾼다 (**굵게** → *굵게*, - 목록 → •, # 제목 → 굵게)
const toSlack = t => String(t ?? '')
  .replace(/\*\*(.+?)\*\*/g, '*$1*')
  .replace(/^\s*[-*] /gm, '• ')
  .replace(/^#{1,6}\s*(.+)$/gm, '*$1*');
const u0 = res.body?.usage ?? {};
const cost = (u0.cost_usd ?? 0) + (req.route_cost ?? 0);  // 의도 분류 비용까지 합친 요청 하나의 비용
const llm = (u0.llm_calls ?? 0) + (req.route_cost ? 1 : 0);
const stopped = res.body?.stopped_by && !['done', 'repeat'].includes(res.body.stopped_by) ? ` · 한도 도달로 중단(${res.body.stopped_by})` : '';
const foot = { type: 'context', elements: [{ type: 'mrkdwn',
  text: `도구 ${res.body?.tool_calls ?? 0}회 · LLM ${llm}회 · $${cost.toFixed(3)} · ${secs}초${stopped}` +
    (res.body?.incident ? ` · 사건 #${res.body.incident.incident_id} 의 ${res.body.incident.report_no}번째 보고 (\`@봇 사건 ${res.body.incident.incident_id}\` 로 전체 보기)` : '') }] };
const sec = t => ({ type: 'section', text: { type: 'mrkdwn', text: clip(t, 2900) } });
// 조사 중 읽은 데이터(로그, 요청)에 지시처럼 보이는 문장이 있었으면 사람에게 알린다. 보고서가 그 문장에 휘둘렸을 수 있다.
const sus = res.body?.injection_suspected ?? [];
if (sus.length) foot.elements.push({ type: 'mrkdwn', text: `:warning: 조사한 데이터에 *지시처럼 보이는 문장* 이 있었음 (${sus.map(x => '`' + x + '`').join(', ')}). 에이전트는 따르지 않도록 되어 있지만, 결론과 조치 제안을 원본 로그로 한 번 확인하세요. 이 보고서에는 승인 버튼을 붙이지 않았습니다.` });
let blocks = [], text, result;
if (res.statusCode === 200 && res.body?.answer && !res.body?.report) {  // 질문 답, 또는 보고서 없이 글로 끝난 조사
  result = 'answer';
  const a = toSlack(res.body.answer);
  text = clip(a, 300);
  blocks = [sec(a), foot];
} else if (res.statusCode === 200 && res.body?.report) {
  const r = res.body.report;
  result = 'ok';
  const icon = { critical: ':red_circle:', warning: ':large_yellow_circle:', ok: ':large_green_circle:' }[r.status] ?? ':white_circle:';
  const label = { critical: '장애', warning: '주의', ok: '정상' }[r.status] ?? '';
  text = `${label} · ${clip(r.headline, 200)}`;
  blocks.push(sec(`${icon} *${label}* · ${clip(r.headline, 150)}`));
  if (r.impact?.length) blocks.push(sec(`*영향*\n${list(r.impact, 3, 90)}`));
  if (r.timeline?.length) blocks.push(sec(`*경과*\n${r.timeline.slice(0, 5).map(t => `• \`${clip(t.time, 11)}\` ${clip(t.event, 80)}`).join('\n')}`));
  if (r.causes?.length) {
    const lines = r.causes.slice(0, 2).map((c, i) =>
      `${i + 1}. ${clip(c.title, 90)}  \`${Math.round((c.confidence ?? 0) * 100)}%\`\n` +
      (c.evidence ?? []).slice(0, 3).map(e => `      ◦ ${clip(e, 90)}`).join('\n'));
    blocks.push(sec(`*원인*\n${lines.join('\n')}`));
  }
  const risk = { low: '낮음', medium: '중간', high: '높음' };
  const acts = (r.actions ?? []).filter(a => a.type !== 'none').slice(0, 2);
  if (acts.length) {
    blocks.push(sec(`*조치 제안* _(승인 전에는 실행하지 않음)_\n` +
      acts.map(a => `• \`${a.type}\` ${a.target} · 위험 ${risk[a.risk] ?? a.risk}\n      ◦ ${clip(a.reason, 90)}`).join('\n')));
  }
  // 승인 버튼. 글자와 확인 창은 여기 고정 문구만 쓴다 (모델이 쓴 문장을 버튼에 넣지 않는다). 대상은 허용 목록에 있는 것만.
  const APPROVE = {
    restart: ['재시작 승인', '파드를 하나씩 다시 시작합니다 (rollout restart). 서비스는 유지됩니다. 메모리와 주입된 장애 상태가 초기화됩니다.'],
    rollback: ['롤백 승인', '배포 저장소에서 이 Deployment 의 마지막 변경(이미지 또는 설정) 하나를 취소하는 커밋을 올립니다. 검사 → Argo CD 반영까지 3~5분 걸립니다.'],
    scale: ['증설 승인 (+1)', 'replicas 를 1 늘리는 커밋을 배포 저장소에 올립니다 (최대 4). 반영까지 3~5분 걸립니다.'],
  };
  const btns = [];
  // 조사한 데이터에 지시처럼 보이는 문장이 있었으면 승인 버튼을 붙이지 않는다. 조치 제안 자체가 그 문장의 결과일 수 있으므로
  // 버튼 하나로 실행되게 두지 않고, 사람이 원본을 확인한 뒤 직접 하게 한다 (제안 내용은 보고서에 그대로 보인다).
  // 보안 알림의 조사는 공격자가 만든 글(요청 경로, User-Agent, 페이로드)을 읽고 낸 결론이다. 지시문 탐지는 패턴이라 피할 수 있으므로
  // (닮은 글자, 뜻만 같은 바꿔 말하기) 보안 조사의 조치 제안은 버튼 하나로 실행되게 두지 않는다. 격리 파드 삭제 버튼은 고정 조치라 그대로 둔다.
  const security = (req.alert?.alerts ?? []).some(x => x.labels?.category === 'security');
  if (security && acts.length) foot.elements.push({ type: 'mrkdwn', text: '보안 알림의 조사라 조치 제안에 승인 버튼을 붙이지 않았습니다. 원본 로그를 확인한 뒤 필요하면 직접 실행하세요.' });
  for (const a of ((sus.length || security) ? [] : acts)) {
    const m = /(?:^|\/)(demo-app)$/.exec(String(a.target ?? '').trim());
    if (a.type === 'retrain' && !btns.some(b => b.action_id === 'mlops_retrain')) {
      btns.push({ type: 'button', action_id: 'mlops_retrain', value: 'anomaly-train', text: { type: 'plain_text', text: '재학습 시작' } });
    } else if (m && APPROVE[a.type] && !btns.some(b => b.action_id === `act_${a.type}`)) {
      btns.push({ type: 'button', action_id: `act_${a.type}`, value: `demo/${m[1]}`, text: { type: 'plain_text', text: APPROVE[a.type][0] },
        confirm: { title: { type: 'plain_text', text: `${a.type} demo/${m[1]}` }, confirm: { type: 'plain_text', text: '실행' }, deny: { type: 'plain_text', text: '취소' },
          text: { type: 'mrkdwn', text: `\`demo/${m[1]}\` ${APPROVE[a.type][1]}` } } });
    }
  }
  if (btns.length) blocks.push({ type: 'actions', elements: btns });
  if (r.data_gaps?.length) blocks.push(sec(`*확인 못 한 것*\n${list(r.data_gaps, 2, 90)}`));
  const q = req.quarantine;
  if (q?.done) {  // 선 조치로 격리한 파드: 조사가 끝나면 사람이 지운다
    blocks.push(sec(`:lock: *격리된 파드* \`${q.namespace}/${q.pod}\`\n• 증거 확인: \`kubectl -n ${q.namespace} logs ${q.pod}\` · \`kubectl -n ${q.namespace} exec ${q.pod} -- ...\` (네트워크만 막혀 있음)\n• 원래 라벨은 어노테이션 \`security.kube-chatops/original-labels\``));
    blocks.push({ type: 'actions', elements: [{ type: 'button', style: 'danger', action_id: 'sec_delete_pod', value: `${q.namespace}/${q.pod}`,
      text: { type: 'plain_text', text: '격리 파드 삭제 (조사 끝)' },
      confirm: { title: { type: 'plain_text', text: '격리 파드 삭제' }, confirm: { type: 'plain_text', text: '삭제' }, deny: { type: 'plain_text', text: '취소' },
        text: { type: 'mrkdwn', text: `\`${q.pod}\` 를 지웁니다. 로그와 파일 같은 증거도 함께 사라집니다.` } } }] });
  }
  if (r.references?.length) blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: `참고: ${r.references.slice(0, 3).map(x => clip(x, 60)).join(' · ')}` }] });
  blocks.push({ type: 'divider' });
  blocks.push(foot);
} else if (res.statusCode === 429) {
  result = 'budget'; text = ':warning: 오늘 AI 사용 예산을 다 써서 조사하지 못했습니다.';
} else {
  result = 'error'; text = `:warning: 조사에 실패했습니다 (agent ${res.statusCode ?? '연결 실패'}). 잠시 후 다시 시도해 주세요.`;
}
return [{ json: { channel: req.channel_id, ts: $('조사 중 표시').item.json.body?.ts, text, blocks: blocks.length ? blocks : null,
  audit: { flow: 'wf-02-investigate', result, source: req.source, user_id: req.user_id, role: req.role,
    channel_id: req.channel_id, thread_id: req.thread_id, seconds: secs, agent_status: res.statusCode,
    mode: req.mode, cost_usd: cost, tool_calls: res.body?.tool_calls ?? 0, stopped_by: res.body?.stopped_by ?? null,
    quarantine: req.quarantine ? `${req.quarantine.pod}:${req.quarantine.done ? 'done' : 'failed'}` : null,
    // 어떤 알림의 조사였는지 (공격 재현 평가 scripts/attack-replay.py 가 보고를 공격과 짝짓는 데 쓴다)
    incident_id: res.body?.incident?.incident_id ?? null, injection_suspected: sus.length,
    alert: req.alert?.alerts?.[0]?.labels?.alertname ?? null,
    alert_pod: req.alert?.alerts?.[0]?.labels?.pod ?? req.alert?.alerts?.[0]?.labels?.source_workload ?? null } } }];
"""
AUDIT_INV = """
return [{ json: { ...$('보고서 만들기').item.json.audit, slack_status: $json.statusCode, execution_id: $execution.id } }];
"""
investigate = workflow(W, "WF-02 investigate (조사와 보고)", [
    node(W, "WF-01 에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 100), {"inputSource": "passthrough"}),
    webhook(W, "Alertmanager", (0, 400), "alert", "hook_alert"),
    code(W, "알림 정리", (220, 400), ALERT_PREP),
    if_true(W, "격리 대상?", (220, 600), "={{ !!$json.quarantine }}"),
    http(W, "파드 조회", (440, 700), "GET",
         f"={{{{ '{K8S}/api/v1/namespaces/' + $json.quarantine.namespace + '/pods/' + $json.quarantine.pod }}}}", None, "k8s_actions"),
    http(W, "격리 수 조회", (550, 800), "GET",
         f"{K8S}/api/v1/namespaces/demo/pods?labelSelector={QUARANTINE_LABEL}%3Dtrue&limit=50", None, "k8s_actions"),
    code(W, "격리 계획", (660, 700), QUARANTINE_PLAN),
    if_true(W, "격리 필요?", (880, 700), "={{ $json.do }}"),
    http(W, "격리", (1100, 650), "PATCH",
         f"={{{{ '{K8S}/api/v1/namespaces/' + $json.namespace + '/pods/' + $json.pod }}}}", "={{ JSON.stringify($json.patch) }}",
         "k8s_actions", headers={"Content-Type": "application/merge-patch+json"}),
    code(W, "격리 반영", (1320, 700), QUARANTINE_DONE),
    http(W, "알림 글 올리기", (440, 400), "POST", f"{GATEWAY}/slack/post",
         "={{ JSON.stringify({ text: $json.title }) }}", "gateway"),
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
    ("Alertmanager", "알림 정리"), ("알림 정리", "격리 대상?"),
    ("격리 대상?", "파드 조회", 0), ("격리 대상?", "알림 글 올리기", 1),
    ("파드 조회", "격리 수 조회"), ("격리 수 조회", "격리 계획"), ("격리 계획", "격리 필요?"), ("격리 필요?", "격리", 0), ("격리 필요?", "격리 반영", 1),
    ("격리", "격리 반영"), ("격리 반영", "알림 글 올리기"),
    ("알림 글 올리기", "알림 스레드"), ("알림 스레드", "조사 요청"),
    ("조사 요청", "조사 중 표시"), ("조사 중 표시", "조사"), ("조사", "보고서 만들기"), ("보고서 만들기", "결과 올리기"),
    ("결과 올리기", "감사 기록"), ("감사 기록", "감사 (WF-99)"),
))

# ---------------- WF-04 mlops ----------------
# 들어오는 길 두 개.
#  1) 학습 파이프라인이 끝나면(Argo onExit) webhook → 결과 카드 (지표, champion 비교, 승격 버튼)
#  2) WF-01 에서 호출: 'mlops' 의도 메시지 → 상태 카드 / 재학습 버튼 → 워크플로 제출 / 승격 버튼 → 배포 저장소 커밋
# 승격: InferenceService 의 STORAGE_URI 를 바꾸는 커밋(main) → 배포 CI 검사 → release → Argo CD → 새 모델이 응답하면
#       MLflow alias champion 을 옮긴다. 서빙 중인 모델과 champion 이 항상 같다.
W = WF["mlops"]
CARD_LIB = r"""
const MLFLOW_UI = '__MLFLOW_UI__';
const pct = v => (v === null || v === undefined || v === '') ? '-' : Number(v).toFixed(2);
const sec = t => ({ type: 'section', text: { type: 'mrkdwn', text: String(t).slice(0, 2900) } });
const ctx = t => ({ type: 'context', elements: [{ type: 'mrkdwn', text: String(t).slice(0, 2900) }] });
const promoteBtn = v => ({ type: 'button', style: 'primary', action_id: 'mlops_promote', value: String(v),
  text: { type: 'plain_text', text: `v${v} 승격 (서빙 교체)` },
  confirm: { title: { type: 'plain_text', text: `v${v} 승격` }, confirm: { type: 'plain_text', text: '승격' }, deny: { type: 'plain_text', text: '취소' },
    text: { type: 'mrkdwn', text: `배포 저장소에 v${v} 로 바꾸는 커밋을 올립니다. 검사 통과 후 3~5분 안에 서빙 모델이 바뀝니다.` } } });
const retrainBtn = { type: 'button', action_id: 'mlops_retrain', value: 'anomaly-train', text: { type: 'plain_text', text: '재학습 시작' } };
const linkBtn = (text, url) => ({ type: 'button', action_id: 'mlops_link', url, text: { type: 'plain_text', text } });
""".replace("__MLFLOW_UI__", MLFLOW_UI)

RESULT_CARD = CARD_LIB + r"""
// Argo onExit 의 notify 단계가 보낸 학습 결과 → Slack 카드
const r = $json.body ?? {};
const ok = r.status === 'Succeeded';
const passed = r.evaluation === 'passed';
const champ = r.champion;
let head;
if (!ok) head = `:x: *모델 재학습 실패* · \`${r.workflow}\``;
else if (passed && r.version) head = `:white_check_mark: *모델 재학습 완료* · v${r.version} 등록 (challenger)`;
else head = `:warning: *모델 재학습 완료, 평가 미통과* · 등록하지 않음`;
const blocks = [sec(head)];
if (r.f1 !== undefined) {
  const kinds = Object.entries(r.recall_by_kind ?? {}).map(([k, v]) => `${k} ${pct(v)}`).join(' · ') || '-';
  // 오탐은 분 수와 표본 크기를 같이 보여 준다. 하루 환산만 보면 정상 구간이 짧을 때 크게 부풀려진다 (v6: 0.4일에서 "39회/일").
  const fa = (r.false_alarm_minutes === null || r.false_alarm_minutes === undefined)
    ? `${r.false_alarms_per_day ?? '-'}분/일`
    : `*${r.false_alarm_minutes}분* (${r.false_alarm_events}건, 정상 ${r.normal_days}일 중 · 하루 환산 ${r.false_alarms_per_day}분)` +
      ((r.champion_false_alarm_minutes === null || r.champion_false_alarm_minutes === undefined) ? '' : ` · 지금 서빙은 같은 데이터에서 ${r.champion_false_alarm_minutes}분`);
  const cv = (r.cv_false_alarm_minutes === null || r.cv_false_alarm_minutes === undefined) ? ''
    : `• 오탐(교차 검증, 정상 ${r.cv_normal_days}일 전체): *${r.cv_false_alarm_minutes}분* (${r.cv_false_alarm_events}건)\n`;
  // 장애를 건으로 센 것: 분 단위 재현율은 긴 장애 하나가 좌우한다
  const has = v => v !== null && v !== undefined;
  const eps = !has(r.episodes) ? ''
    : `• 장애 ${r.episodes}건 중 *${r.episodes_detected}건* 탐지` + (has(r.detect_late_minutes_max) ? ` · 탐지 지연 중앙값 ${r.detect_late_minutes_median}분, 최대 ${r.detect_late_minutes_max}분 (주입 3분 뒤부터)` : '') + `\n`;
  // 최근 24시간을 새 모델과 지금 서빙 모델로 다시 채점해 판정이 갈린 분
  const rp = r.replay;
  const rep = !rp || !has(rp.minutes) ? ''
    : `• 최근 24시간 재채점 (${rp.minutes}분): 새 모델 이상 ${rp.new_anomaly_minutes}분, 지금 서빙 ${rp.champion_anomaly_minutes}분` +
      ` · 새 모델만 이상: 장애 중 ${rp.only_new_in_trouble}분, 평온할 때 *${rp.only_new_in_quiet}분*` +
      ` · 지금 서빙만 이상: 장애 중 ${rp.only_champion_in_trouble}분, 평온할 때 ${rp.only_champion_in_quiet}분\n`;
  blocks.push(sec(
    `*평가* (정상 뒤쪽 + 장애 구간 ${r.data?.test_anomaly_rows ?? '?'}분)\n` +
    `• F1 *${pct(r.f1)}* · precision ${pct(r.precision)} · recall ${pct(r.recall)}\n` +
    `• 장애 종류별 재현율: ${kinds}\n` +
    eps +
    `• 오탐 ${fa}\n` + cv + rep +
    `• 비교: z-score 기준선 F1 ${pct(r.baseline_f1)}, IsolationForest 단독 F1 ${pct(r.iforest_f1)}\n` +
    `• 지금 서빙(champion): ${champ ? `v${champ.version} · F1 ${pct(champ.f1)}` : '없음'}`));
}
if (r.failures?.length) blocks.push(sec('*실패한 단계*\n' + r.failures.map(f => `• ${f.step ?? ''} ${f.message ?? ''}`).join('\n')));
if (r.summary_error) blocks.push(ctx(`MLflow 조회 실패: ${r.summary_error}`));
const buttons = [];
if (ok && passed && r.version && (!champ || String(champ.version) !== String(r.version))) buttons.push(promoteBtn(r.version));
buttons.push(linkBtn('MLflow 에서 보기', `${MLFLOW_UI}/#/models/demo-anomaly`));
blocks.push({ type: 'actions', elements: buttons });
blocks.push(ctx(`워크플로 \`${r.workflow}\` · 데이터 ${String(r.data?.data_start ?? '').slice(0, 16)} ~ ${String(r.data?.data_end ?? '').slice(0, 16)} UTC`));
// 결과 카드는 항상 채널에 새 메시지로 올린다 (thread_ts 없음). 요청한 스레드에 달면, 그 카드의 버튼으로 시작한
// 다음 재학습 결과도 같은 스레드에 달려 오래된 스레드 하나에 계속 쌓인다 (11-27). 승격 진행 상황은 이 카드의 스레드에 달린다.
return [{ json: { channel: r.slack?.channel ?? null, thread_ts: null, text: head.replace(/[*:`]/g, ''), blocks,
  audit: { flow: 'wf-04-mlops', result: `training:${r.status}`, workflow: r.workflow, version: r.version ?? null, f1: r.f1 ?? null } } }];
"""

REQUEST = """
// WF-01 이 넘긴 이벤트 → 할 일. 재학습·승격은 operator 만.
const ev = $json;
const id = ev.action?.id ?? '';
let op = 'status';
if (ev.type === 'button_click') {
  if (id === 'mlops_link') return [];  // 링크 버튼은 브라우저가 연다. 할 일 없음
  op = id === 'mlops_retrain' ? 'retrain' : id === 'mlops_promote' ? 'promote' : 'unknown';
}
if ((op === 'retrain' || op === 'promote') && ev.role !== 'operator') op = 'deny';
return [{ json: { ...ev, op, version: op === 'promote' ? String(ev.action?.value ?? '') : null } }];
"""

STATUS_CARD = CARD_LIB + r"""
// 최근 모델 버전 5개 (태그 test_f1, alias) → 상태 카드
const req = $('요청 정리').item.json;
const vs = $json.statusCode === 200 ? ($json.body.model_versions ?? []) : null;
if (!vs) return [{ json: { channel: req.channel_id, thread_ts: req.thread_id, text: `MLflow 조회 실패 (${$json.statusCode})` } }];
const tag = (v, k) => (v.tags ?? []).find(t => t.key === k)?.value;
const champ = vs.find(v => (v.aliases ?? []).includes('champion'));
const chall = vs.find(v => (v.aliases ?? []).includes('challenger'));
const lines = vs.map(v => {
  const al = (v.aliases ?? []).map(a => a === 'champion' ? '*champion (서빙 중)*' : a).join(', ');
  const at = new Date(Number(v.creation_timestamp)).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  return `• v${v.version} · F1 ${pct(tag(v, 'test_f1'))} · ${at}${al ? ' · ' + al : ''}`;
});
const blocks = [sec(`:bar_chart: *이상 탐지 모델 \`demo-anomaly\`*\n${lines.join('\n') || '등록된 버전 없음'}`)];
const buttons = [retrainBtn];
if (chall && (!champ || chall.version !== champ.version)) buttons.unshift(promoteBtn(chall.version));
buttons.push(linkBtn('MLflow', `${MLFLOW_UI}/#/models/demo-anomaly`));
blocks.push({ type: 'actions', elements: buttons });
blocks.push(ctx('재학습은 2~3분 걸리고 결과는 이 스레드에 달립니다. 매주 월 03:00 에도 자동으로 돕니다. 승격은 operator 만 할 수 있습니다.'));
return [{ json: { channel: req.channel_id, thread_ts: req.thread_id, text: '이상 탐지 모델 상태', blocks,
  audit: { flow: 'wf-04-mlops', result: 'status', user_id: req.user_id } } }];
"""

SUBMIT_BODY = (
    "={{ JSON.stringify({ apiVersion: 'argoproj.io/v1alpha1', kind: 'Workflow', metadata: { generateName: 'anomaly-train-', "
    "namespace: 'mlops', labels: { 'kube-chatops/trigger': 'slack' }, annotations: { 'kube-chatops/requested-by': $json.user_id } }, "
    "spec: { workflowTemplateRef: { name: 'anomaly-train' }, arguments: { parameters: ["
    "{ name: 'slack-channel', value: $json.channel_id }, { name: 'slack-thread', value: $json.thread_id }] } } }) }}"
)
RETRAIN_REPLY = """
const req = $('요청 정리').item.json;
const ok = $json.statusCode === 201;
const name = $json.body?.metadata?.name;
return [{ json: { channel: req.channel_id, thread_ts: req.thread_id,
  text: ok ? `:gear: <@${req.user_id}> 재학습을 시작했습니다 (\\`${name}\\`). 2~3분 뒤 이 스레드에 결과가 달립니다.`
           : `:warning: 재학습 제출 실패 (kubernetes ${$json.statusCode}: ${String($json.body?.message ?? '').slice(0, 200)})`,
  audit: { flow: 'wf-04-mlops', result: ok ? 'retrain:submitted' : 'retrain:error', user_id: req.user_id, workflow: name ?? null, k8s_status: $json.statusCode } } }];
"""
DENY_REPLY = """
const req = $json;
const text = req.op === 'deny' ? `:no_entry: 재학습과 승격은 operator 만 할 수 있습니다 (<@${req.user_id}> 는 ${req.role}).` : '알 수 없는 버튼입니다.';
return [{ json: { channel: req.channel_id, thread_ts: req.thread_id, text, audit: { flow: 'wf-04-mlops', result: req.op, user_id: req.user_id, role: req.role } } }];
"""

# 승격 버튼이 달린 글(학습 결과 카드, 상태 카드)을 고친다: 누른 버전의 버튼을 상태 한 줄로 바꾼다.
#   진행 중 → 버튼 없음 / 완료 → "v8 로 되돌리기" 버튼 (나중에 다른 버전으로 옮긴 뒤 돌아올 때) / 실패 → 원래 버튼 그대로.
# 글의 원래 블록은 chat-gateway 가 버튼 이벤트에 실어 준다 (없으면 ts 가 비어 gateway 가 거절하고 흐름은 계속된다).
CARD_EDIT = r"""
function cardEdit(p, status, button) {
  const kst = new Date().toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const src = (p.message?.blocks ?? []).filter(b => b.block_id !== `promote_status_${p.version}`);
  const out = [];
  for (const b of src) {
    if (b.type !== 'actions' || !(b.elements ?? []).some(e => e.action_id === 'mlops_promote' && String(e.value) === String(p.version))) { out.push(b); continue; }
    out.push({ type: 'context', block_id: `promote_status_${p.version}`, elements: [{ type: 'mrkdwn', text: `${status} · ${kst}` }] });
    const elements = b.elements.map(e => {
      if (e.action_id !== 'mlops_promote' || String(e.value) !== String(p.version)) return e;
      if (button === 'keep') return e;
      if (button === 'rollback') return { type: 'button', action_id: 'mlops_promote', value: String(p.version),
        text: { type: 'plain_text', text: `v${p.version} 로 되돌리기` },
        confirm: { title: { type: 'plain_text', text: `v${p.version} 로 되돌리기` }, confirm: { type: 'plain_text', text: '전환' }, deny: { type: 'plain_text', text: '취소' },
          text: { type: 'mrkdwn', text: `서빙 모델을 v${p.version} 로 바꾸는 커밋을 올립니다. 이미 v${p.version} 가 서빙 중이면 아무 일도 하지 않습니다.` } } };
      return null;
    }).filter(Boolean);
    if (elements.length) out.push({ ...b, elements });
  }
  return { channel: p.channel_id, ts: src.length ? (p.message_ts ?? null) : null, text: p.message?.text || `demo-anomaly v${p.version}`, blocks: out };
}
"""
CARD_PROGRESS = CARD_EDIT + """
const p = $('커밋 완료').item.json;
return [{ json: { card: cardEdit(p, `:rocket: *v${p.version} 승격 진행 중* · <@${p.user_id}> 승인`, 'none') } }];
"""
CARD_DONE = CARD_EDIT + """
const p = $('반영 확인').item.json;
return [{ json: { card: cardEdit(p, `:white_check_mark: *v${p.version} 서빙 시작* · <@${p.user_id}> 승격`, 'rollback') } }];
"""
CARD_TIMEOUT = CARD_EDIT + """
const p = $('반영 확인').item.json;
return [{ json: { card: cardEdit(p, `:warning: *v${p.version} 승격이 15분 안에 반영되지 않음* (스레드 참고) · <@${p.user_id}>`, 'keep') } }];
"""
CARD_BODY = "={{ JSON.stringify($json.card) }}"

PROMOTE_PREP = """
// 승격할 버전의 모델 위치(storage_uri 태그)와 model id. 이미 champion 이면 멈춘다.
const req = $('요청 정리').item.json;
if ($json.statusCode !== 200) throw new Error(`MLflow 버전 조회 실패: v${req.version} ${$json.statusCode}`);
const mv = $json.body.model_version;
const uri = (mv.tags ?? []).find(t => t.key === 'storage_uri')?.value;
const f1 = (mv.tags ?? []).find(t => t.key === 'test_f1')?.value;
const modelId = String(mv.source ?? '').replace(/^models:\\//, '');
if (!uri || !/^s3:\\/\\/mlops\\/mlflow\\/[\\w/.-]+$/.test(uri)) throw new Error(`v${req.version} 에 올바른 storage_uri 태그가 없다: ${uri}`);
return [{ json: { ...req, storage_uri: uri, model_id: modelId, f1, already: (mv.aliases ?? []).includes('champion') } }];
"""
EDIT_FILE = """
// 배포 저장소의 InferenceService 에서 STORAGE_URI 값과 '지금:' 주석 한 줄만 바꾼다.
const p = $('승격 준비').item.json;
if ($json.statusCode !== 200) throw new Error(`GitHub 파일 읽기 실패 ${$json.statusCode}: ${JSON.stringify($json.body).slice(0, 200)}`);
const src = Buffer.from($json.body.content, 'base64').toString('utf8');
const kst = new Date().toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false });
let out = src.replace(/(- name: STORAGE_URI\\n\\s*value: )\\S+/, `$1${p.storage_uri}`);
out = out.replace(/^#   지금: .*$/m, `#   지금: demo-anomaly v${p.version} (MLflow model ${p.model_id}, 평가 F1 ${p.f1 ?? '-'}, ${kst} Slack 승인 ${p.user_id})`);
if (out === src) throw new Error('파일이 바뀌지 않았다 (이미 이 버전이거나 STORAGE_URI 줄을 못 찾음)');
if (!out.includes(p.storage_uri)) throw new Error('STORAGE_URI 줄을 못 찾았다');
return [{ json: { ...p, put: { message: `mlops: demo-anomaly v${p.version} 승격 (Slack 승인 ${p.user_id})\\n\\nMLflow model ${p.model_id}, 평가 F1 ${p.f1 ?? '-'}\\nSTORAGE_URI ${p.storage_uri}`,
  content: Buffer.from(out, 'utf8').toString('base64'), sha: $json.body.sha, branch: 'main' } } }];
"""
COMMITTED = """
const p = $('파일 고치기').item.json;
if (![200, 201].includes($json.statusCode)) throw new Error(`GitHub 커밋 실패 ${$json.statusCode}: ${JSON.stringify($json.body).slice(0, 300)}`);
const sha = $json.body.commit.sha;
return [{ json: { ...p, commit: sha, channel: p.channel_id, thread_ts: p.thread_id,
  text: `:rocket: <@${p.user_id}> 승인으로 v${p.version} 승격 커밋을 올렸습니다 (<${$json.body.commit.html_url}|${sha.slice(0, 7)}>). 배포 검사 → release → Argo CD 반영을 기다립니다 (보통 3~5분).` } }];
"""
CHECK = """
// 예측 서버가 새 모델로 답하는지. 15번(약 15분)까지 기다린다.
const p = $('커밋 완료').item.json;
const served = $json.body?.model_version ?? null;
const tries = $runIndex + 1;
return [{ json: { ...p, served, tries, done: served === p.model_id, give_up: served !== p.model_id && tries >= 15 } }];
"""
DONE_REPLY = """
const p = $('반영 확인').item.json;
const ok = $json.statusCode === 200;
return [{ json: { channel: p.channel_id, thread_ts: p.thread_id,
  text: ok ? `:white_check_mark: v${p.version} 서빙을 시작했습니다. MLflow alias \\`champion\\` 을 v${p.version} 으로 옮겼습니다.`
           : `:warning: v${p.version} 서빙은 시작됐지만 champion 지정에 실패했습니다 (MLflow ${$json.statusCode}).`,
  audit: { flow: 'wf-04-mlops', result: ok ? 'promote:done' : 'promote:alias_error', user_id: p.user_id, version: p.version, commit: p.commit } } }];
"""
TIMEOUT_REPLY = """
const p = $json;
return [{ json: { channel: p.channel_id, thread_ts: p.thread_id,
  text: `:warning: 커밋 ${String(p.commit).slice(0, 7)} 뒤 15분이 지나도 예측 서버가 v${p.version} 로 답하지 않습니다 (지금 ${p.served ?? '응답 없음'}). 배포 CI 와 Argo CD 앱 kube-chatops-mlops-anomaly-serve 를 확인하세요. champion 은 옮기지 않았습니다.`,
  audit: { flow: 'wf-04-mlops', result: 'promote:timeout', user_id: p.user_id, version: p.version, commit: p.commit } } }];
"""
ALREADY_REPLY = """
const p = $json;
return [{ json: { channel: p.channel_id, thread_ts: p.thread_id, text: `v${p.version} 은 이미 champion(서빙 중) 입니다.`,
  audit: { flow: 'wf-04-mlops', result: 'promote:already', user_id: p.user_id, version: p.version } } }];
"""
AUDIT_04 = """
return [{ json: { ...($('Slack 글 정리').item.json.audit ?? { flow: 'wf-04-mlops', result: 'reply' }), slack_status: $json.statusCode, execution_id: $execution.id } }];
"""
PASS = "return [{ json: $json }];"
GH = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kube-chatops-n8n"}
POST_SLACK = "={{ JSON.stringify({ channel: $json.channel, thread_ts: $json.thread_ts, text: $json.text, blocks: $json.blocks ?? null }) }}"

mlops = workflow(W, "WF-04 mlops (재학습과 승격)", [
    webhook(W, "학습 결과", (0, 0), "mlops", "hook_mlops"),
    code(W, "결과 카드", (220, 0), RESULT_CARD),
    node(W, "WF-01 에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 400), {"inputSource": "passthrough"}),
    code(W, "요청 정리", (220, 400), REQUEST),
    if_true(W, "상태?", (440, 400), "={{ $json.op === 'status' }}"),
    http(W, "버전 목록", (660, 300), "GET",
         f"={{{{ '{MLFLOW}/api/2.0/mlflow/model-versions/search?max_results=5&order_by=version_number%20DESC&filter=' + encodeURIComponent(\"name='{MODEL_NAME}'\") }}}}",
         None, None),
    code(W, "상태 카드", (880, 300), STATUS_CARD),
    if_true(W, "재학습?", (660, 500), "={{ $json.op === 'retrain' }}"),
    http(W, "워크플로 제출", (880, 450), "POST", f"{K8S}/apis/argoproj.io/v1alpha1/namespaces/mlops/workflows", SUBMIT_BODY, "k8s"),
    code(W, "재학습 답장", (1100, 450), RETRAIN_REPLY),
    if_true(W, "승격?", (880, 650), "={{ $json.op === 'promote' }}"),
    code(W, "권한 없음", (1100, 800), DENY_REPLY),
    http(W, "버전 조회", (1100, 650), "GET",
         f"={{{{ '{MLFLOW}/api/2.0/mlflow/model-versions/get?name={MODEL_NAME}&version=' + encodeURIComponent($json.version) }}}}", None, None),
    code(W, "승격 준비", (1320, 650), PROMOTE_PREP),
    if_true(W, "이미 champion?", (1540, 650), "={{ $json.already }}"),
    code(W, "이미 서빙 중", (1760, 800), ALREADY_REPLY),
    http(W, "파일 읽기", (1760, 600), "GET", f"{GITHUB_FILE}?ref=main", None, "github", headers=GH),
    code(W, "파일 고치기", (1980, 600), EDIT_FILE),
    http(W, "커밋", (2200, 600), "PUT", GITHUB_FILE, "={{ JSON.stringify($json.put) }}", "github", headers=GH),
    code(W, "커밋 완료", (2420, 600), COMMITTED),
    http(W, "커밋 알림", (2640, 600), "POST", f"{GATEWAY}/slack/post", POST_SLACK, "gateway"),
    code(W, "카드: 진행 중", (2640, 780), CARD_PROGRESS),
    http(W, "카드 고치기 (진행)", (2860, 780), "POST", f"{GATEWAY}/slack/update", CARD_BODY, "gateway"),
    code(W, "카드: 완료", (3960, 380), CARD_DONE),
    http(W, "카드 고치기 (완료)", (4180, 380), "POST", f"{GATEWAY}/slack/update", CARD_BODY, "gateway"),
    code(W, "카드: 시간 초과", (3960, 880), CARD_TIMEOUT),
    http(W, "카드 고치기 (시간 초과)", (4180, 880), "POST", f"{GATEWAY}/slack/update", CARD_BODY, "gateway"),
    node(W, "1분 대기", "n8n-nodes-base.wait", 1.1, (2860, 600), {"amount": 60, "unit": "seconds"}, webhookId=nid(W, "wait/hook")),
    http(W, "서빙 모델 확인", (3080, 600), "POST", MODEL_PREDICT,
         "={{ JSON.stringify({ instances: [{ rps: 1, error_ratio: 0, p95_seconds: 0.01, mem_ratio: 0.1 }] }) }}", None, timeout_ms=10000),
    code(W, "반영 확인", (3300, 600), CHECK),
    if_true(W, "반영됨?", (3520, 600), "={{ $json.done }}"),
    http(W, "champion 지정", (3740, 500), "POST", f"{MLFLOW}/api/2.0/mlflow/registered-models/alias",
         f"={{{{ JSON.stringify({{ name: '{MODEL_NAME}', alias: 'champion', version: String($json.version) }}) }}}}", None),
    code(W, "완료 답장", (3960, 500), DONE_REPLY),
    if_true(W, "포기?", (3740, 750), "={{ $json.give_up }}"),
    code(W, "시간 초과", (3960, 750), TIMEOUT_REPLY),
    code(W, "Slack 글 정리", (4180, 300), PASS),
    http(W, "Slack 글", (4400, 300), "POST", f"{GATEWAY}/slack/post", POST_SLACK, "gateway"),
    code(W, "감사 기록", (4620, 300), AUDIT_04),
    call_audit(W, "감사 (WF-99)", (4840, 300)),
], links(
    ("학습 결과", "결과 카드"), ("결과 카드", "Slack 글 정리"),
    ("WF-01 에서 호출", "요청 정리"), ("요청 정리", "상태?"),
    ("상태?", "버전 목록", 0), ("상태?", "재학습?", 1),
    ("버전 목록", "상태 카드"), ("상태 카드", "Slack 글 정리"),
    ("재학습?", "워크플로 제출", 0), ("재학습?", "승격?", 1),
    ("워크플로 제출", "재학습 답장"), ("재학습 답장", "Slack 글 정리"),
    ("승격?", "버전 조회", 0), ("승격?", "권한 없음", 1), ("권한 없음", "Slack 글 정리"),
    ("버전 조회", "승격 준비"), ("승격 준비", "이미 champion?"),
    ("이미 champion?", "이미 서빙 중", 0), ("이미 champion?", "파일 읽기", 1), ("이미 서빙 중", "Slack 글 정리"),
    ("파일 읽기", "파일 고치기"), ("파일 고치기", "커밋"), ("커밋", "커밋 완료"), ("커밋 완료", "커밋 알림"),
    ("커밋 알림", "1분 대기"), ("커밋 완료", "카드: 진행 중"), ("카드: 진행 중", "카드 고치기 (진행)"),
    ("champion 지정", "카드: 완료"), ("카드: 완료", "카드 고치기 (완료)"),
    ("포기?", "카드: 시간 초과", 0), ("카드: 시간 초과", "카드 고치기 (시간 초과)"), ("1분 대기", "서빙 모델 확인"), ("서빙 모델 확인", "반영 확인"), ("반영 확인", "반영됨?"),
    ("반영됨?", "champion 지정", 0), ("반영됨?", "포기?", 1),
    ("champion 지정", "완료 답장"), ("완료 답장", "Slack 글 정리"),
    ("포기?", "시간 초과", 0), ("포기?", "1분 대기", 1), ("시간 초과", "Slack 글 정리"),
    ("Slack 글 정리", "Slack 글"), ("Slack 글", "감사 기록"), ("감사 기록", "감사 (WF-99)"),
))

# ---------------- WF-03 actions ----------------
# Slack 조치 버튼 → operator 확인 → 실행.
#   sec_delete_pod  격리 파드 삭제 (격리 라벨이 붙은 demo 파드만)
#   act_restart     Deployment 재시작 (파드 템플릿에 restartedAt 어노테이션. Argo CD 가 관리하지 않는 필드라 되돌려지지 않는다)
#   act_scale       replicas +1  ┐ Argo CD 가 selfHeal 이라 클러스터를 직접 바꾸면 되돌려진다
#   act_rollback    마지막 변경 취소 ┘ → 배포 저장소에 커밋한다 (검사 → release → Argo CD)
# 안전장치: operator 만, 대상은 허용 목록(DEPLOYS)만, 1시간 넘은 보고서의 버튼은 거절, 재시작은 5분에 한 번, 증설은 최대 4,
#   롤백은 마지막 변경이 이미 되돌리기면 거절.
# 버튼 값(대상)을 바꿔 보내도 여기서 다시 검사하므로 다른 것을 건드릴 수 없다. 모델이 쓴 문장은 실행에 쓰이지 않는다.
W = WF["actions"]
DEPLOY_FILE = "https://api.github.com/repos/UHeeJoon/kube-chatops-deploy/contents/demo/__D__/deployment.yaml"
DEPLOY_COMMITS = "https://api.github.com/repos/UHeeJoon/kube-chatops-deploy/commits?sha=main&per_page=2&path=demo/__D__/deployment.yaml"
ACT_REQ = """
const DEPLOYS = ['demo-app'];
const MAX_AGE_MIN = 60;
const ev = $json;
const id = ev.action?.id ?? '';
const [namespace, name] = String(ev.action?.value ?? '').split('/');
let op = id === 'sec_delete_pod' ? 'delete_pod' : ['act_restart', 'act_rollback', 'act_scale'].includes(id) ? id.slice(4) : 'unknown', note = '';
if (ev.role !== 'operator') { op = 'deny'; note = '조치 버튼은 operator 만 누를 수 있습니다.'; }
else if (op === 'unknown') note = `모르는 조치입니다 (${id}).`;
else if (op === 'delete_pod' && (namespace !== 'demo' || !/^[a-z0-9.-]{1,253}$/.test(name ?? ''))) { op = 'deny'; note = '대상이 올바르지 않습니다.'; }
else if (op !== 'delete_pod' && (namespace !== 'demo' || !DEPLOYS.includes(name))) { op = 'deny'; note = `조치할 수 없는 대상입니다 (허용: ${DEPLOYS.map(d => 'demo/' + d).join(', ')}).`; }
// 오래된 보고서의 버튼은 받지 않는다. 그 사이 상태가 바뀌었을 수 있다 (이미 되돌린 뒤 롤백을 또 누르면 되돌림이 취소된다).
else if (op !== 'delete_pod' && !(Date.now() / 1000 - Number(ev.message_ts) < MAX_AGE_MIN * 60)) { op = 'deny'; note = `이 보고서는 ${MAX_AGE_MIN}분이 넘었습니다. 지금 상태가 달라졌을 수 있으니 다시 조사한 뒤 새 보고서에서 승인하세요.`; }
return [{ json: { ...ev, op, namespace, pod: op === 'delete_pod' ? name : null, deploy: op !== 'delete_pod' && op !== 'deny' && op !== 'unknown' ? name : null, note } }];
"""
ACT_CHECK = r"""
const req = $('요청 정리').item.json;
const res = $json;
const ok = res.statusCode === 200 && res.body.metadata?.labels?.['__QL__'] === 'true';
const note = res.statusCode === 404 ? `\`${req.pod}\` 는 이미 없습니다.`
  : res.statusCode !== 200 ? `파드 조회 실패 (${res.statusCode}).`
  : ok ? '' : `\`${req.pod}\` 는 격리된 파드가 아니라 지우지 않았습니다.`;
return [{ json: { ...req, ok, note, result: ok ? null : 'refused' } }];
""".replace("__QL__", QUARANTINE_LABEL)
ACT_DONE = r"""
const req = $('확인').item.json;
const ok = $json.statusCode === 200 || $json.statusCode === 202;
return [{ json: { ...req, note: ok ? `:wastebasket: 격리 파드 \`${req.pod}\` 를 삭제했습니다 (<@${req.user_id}>).` : `삭제 실패 (${$json.statusCode}).`,
  result: ok ? 'deleted' : 'delete_failed', done: ok ? `격리 파드 \`${req.pod}\` 삭제` : null } }];
"""
RESTART_PLAN = r"""
// 지금 상태를 보고 재시작해도 되는지 정한다. 5분 안에 다시 시작한 적이 있으면 거절 (버튼 연타, 재시작 반복 방지).
const req = $('요청 정리').item.json;
const res = $json;
if (res.statusCode !== 200) return [{ json: { ...req, ok: false, result: 'refused', note: `Deployment 조회 실패 (${res.statusCode}).` } }];
const last = res.body.spec?.template?.metadata?.annotations?.['kubectl.kubernetes.io/restartedAt'];
const ago = last ? (Date.now() - Date.parse(last)) / 60000 : Infinity;
if (ago < 5) return [{ json: { ...req, ok: false, result: 'refused', note: `\`${req.deploy}\` 는 ${Math.round(ago)}분 전에 재시작했습니다. 5분 안에는 다시 하지 않습니다.` } }];
const now = new Date().toISOString();
return [{ json: { ...req, ok: true, replicas: res.body.spec?.replicas ?? null,
  patch: { spec: { template: { metadata: { annotations: { 'kubectl.kubernetes.io/restartedAt': now, 'kube-chatops/restarted-by': `slack:${req.user_id}` } } } } } } }];
"""
RESTART_DONE = r"""
const req = $('재시작 판단').item.json;
const ok = $json.statusCode === 200;
return [{ json: { ...req, result: ok ? 'restarted' : 'restart_failed', done: ok ? `\`restart\` ${req.namespace}/${req.deploy}` : null,
  note: ok ? `:arrows_counterclockwise: <@${req.user_id}> 승인으로 \`${req.namespace}/${req.deploy}\` 를 재시작합니다 (파드 ${req.replicas ?? '?'}개를 하나씩 교체, 1분쯤 걸림).`
    : `재시작 실패 (${$json.statusCode}): ${JSON.stringify($json.body?.message ?? '').slice(0, 200)}` } }];
"""
GIT_EDIT = r"""
// 배포 저장소의 deployment.yaml 에서 한 줄만 바꾼다. 증설: replicas +1 (최대 4). 롤백: 이미지 digest 를 직전 커밋의 것으로.
const MAX_REPLICAS = 4;
const req = $('요청 정리').item.json;
const cur = $('현재 파일').item.json;
const refuse = note => [{ json: { ...req, ok: false, result: 'refused', note } }];
if (cur.statusCode !== 200) return refuse(`배포 저장소 파일 읽기 실패 (${cur.statusCode}).`);
const src = Buffer.from(cur.body.content, 'base64').toString('utf8');
let out, what, message;
if (req.op === 'scale') {
  const m = src.match(/^(\s*replicas: )(\d+)\s*$/m);
  if (!m) return refuse('replicas 줄을 찾지 못했습니다.');
  const n = Number(m[2]);
  if (n >= MAX_REPLICAS) return refuse(`\`${req.deploy}\` 는 이미 replicas ${n} 입니다 (최대 ${MAX_REPLICAS}). 더 늘리지 않습니다.`);
  out = src.replace(m[0], `${m[1]}${n + 1}`);
  what = `replicas ${n} → ${n + 1}`;
  message = `demo: ${req.deploy} replicas ${n} → ${n + 1} (Slack 승인 ${req.user_id})`;
} else {
  // 롤백: 파일을 직전 커밋의 내용으로 되돌린다 (이미지든 설정이든 마지막 변경 하나를 취소). 무엇을 되돌리는지 커밋 제목을 보여 준다.
  const prev = $json;
  const pick = $('직전 커밋').item.json;
  if (!pick.prev_sha) return refuse('이 파일의 이전 커밋이 없어 되돌릴 수 없습니다.');
  // 롤백은 "이 파일의 마지막 커밋 취소" 다. 보고서가 올라온 뒤에 파일이 또 바뀌었으면(증설 승인, 수정 커밋) 취소되는 것은
  // 보고서가 문제로 본 변경이 아니라 그 뒤의 변경이다. 보고서가 본 상태와 다르면 실행하지 않는다.
  const changedAt = Date.parse(pick.last_at ?? '') / 1000;
  if (changedAt && changedAt > Number(req.message_ts)) return refuse(`이 보고서가 올라온 뒤에 배포 파일이 바뀌었습니다 ("${pick.last_title}"). 지금 롤백하면 보고서가 본 변경이 아니라 그 변경이 취소되므로 실행하지 않았습니다. 다시 조사한 뒤 새 보고서에서 승인하세요.`);
  // 마지막 변경이 이미 되돌리기면 또 되돌리지 않는다 (고장 난 상태로 돌아간다)
  if (/롤백|되돌|revert/i.test(pick.last_title)) return refuse(`마지막 변경이 이미 되돌리기입니다 ("${pick.last_title}"). 또 되돌리면 그 전 상태로 돌아가므로 실행하지 않았습니다.`);
  if (prev.statusCode !== 200) return refuse(`이전 버전 파일 읽기 실패 (${prev.statusCode}).`);
  out = Buffer.from(prev.body.content, 'base64').toString('utf8');
  if (out === src) return refuse('직전 커밋과 내용이 같아 되돌릴 것이 없습니다.');
  const re = /image: \S+@sha256:([0-9a-f]{12})/;
  const img = (src.match(re)?.[1] ?? '') !== (out.match(re)?.[1] ?? '') ? ` (이미지 ${src.match(re)?.[1]} → ${out.match(re)?.[1]})` : ' (이미지는 그대로, 설정 변경 취소)';
  what = `마지막 변경 취소: "${pick.last_title}"${img}`;
  message = `demo: ${req.deploy} 직전 상태로 롤백 (Slack 승인 ${req.user_id})\n\n되돌린 커밋 ${pick.last_sha}: ${pick.last_title}\n되돌아간 상태 ${pick.prev_sha}`;
}
return [{ json: { ...req, ok: true, what, put: { message, content: Buffer.from(out, 'utf8').toString('base64'), sha: cur.body.sha, branch: 'main' } } }];
"""
PREV_REF = r"""
// 이 파일을 바꾼 최근 커밋 2개 중 두 번째(직전 상태)를 고른다.
const req = $('요청 정리').item.json;
const list = Array.isArray($json.body) ? $json.body : [];
return [{ json: { ...req, prev_sha: list[1]?.sha ?? null, last_sha: String(list[0]?.sha ?? '').slice(0, 7),
  last_at: list[0]?.commit?.committer?.date ?? null,
  last_title: String(list[0]?.commit?.message ?? '').split('\n')[0].slice(0, 80) } }];
"""
GIT_DONE = r"""
const req = $('파일 고치기').item.json;
const ok = [200, 201].includes($json.statusCode);
const sha = ok ? $json.body.commit.sha : '';
return [{ json: { ...req, result: ok ? (req.op === 'scale' ? 'scale_committed' : 'rollback_committed') : 'commit_failed',
  done: ok ? `\`${req.op}\` ${req.namespace}/${req.deploy} (${req.what})` : null,
  note: ok ? `:rocket: <@${req.user_id}> 승인으로 \`${req.namespace}/${req.deploy}\` ${req.what} 커밋을 올렸습니다 (<${$json.body.commit.html_url}|${sha.slice(0, 7)}>). 배포 검사 → release → Argo CD 반영까지 3~5분 걸립니다.`
    : `커밋 실패 (${$json.statusCode}): ${JSON.stringify($json.body?.message ?? '').slice(0, 200)}` } }];
"""
ACT_REPLY = r"""
// 스레드 답장 + 실행한 조치의 버튼을 카드에서 빼고 "누가 언제 승인" 한 줄로 바꾼다 (두 번 누르지 않게).
const it = $json;
let card = null;
if (it.done && it.message_ts && (it.message?.blocks ?? []).length) {
  const kst = new Date().toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const out = [];
  for (const b of it.message.blocks) {
    if (b.type !== 'actions') { out.push(b); continue; }
    const rest = (b.elements ?? []).filter(e => !(e.action_id === it.action?.id && String(e.value) === String(it.action?.value)));
    if (rest.length !== (b.elements ?? []).length) out.push({ type: 'context', elements: [{ type: 'mrkdwn', text: `:white_check_mark: ${it.done} · <@${it.user_id}> 승인 · ${kst}` }] });
    if (rest.length) out.push({ ...b, elements: rest });
  }
  card = { channel: it.channel_id, ts: it.message_ts, text: it.message?.text || '조치 승인', blocks: out };
}
return [{ json: { channel: it.channel_id, thread_ts: it.thread_id, text: it.note || '처리했습니다.', card,
  audit: { flow: 'wf-03-actions', result: it.result ?? it.op, op: it.op, target: it.pod ? `${it.namespace}/${it.pod}` : it.deploy ? `${it.namespace}/deployment/${it.deploy}` : null,
    detail: it.what ?? null, user_id: it.user_id, role: it.role, channel_id: it.channel_id, thread_id: it.thread_id } } }];
"""
AUDIT_03 = """
return [{ json: { ...$('답장 정리').item.json.audit, slack_status: $('답장').item.json.statusCode, execution_id: $execution.id } }];
"""
_DEPLOY_API = f"={{{{ '{K8S}/apis/apps/v1/namespaces/' + $json.namespace + '/deployments/' + $json.deploy }}}}"
_FILE = lambda ref: "={{ '" + DEPLOY_FILE.replace("__D__", "' + $json.deploy + '") + "?ref=' + " + ref + " }}"  # noqa: E731
actions = workflow(W, "WF-03 actions (승인 조치)", [
    node(W, "WF-01 에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 300), {"inputSource": "passthrough"}),
    code(W, "요청 정리", (220, 300), ACT_REQ),
    if_true(W, "격리 파드 삭제?", (440, 300), "={{ $json.op === 'delete_pod' }}"),
    http(W, "파드 확인", (660, 200), "GET",
         f"={{{{ '{K8S}/api/v1/namespaces/' + $json.namespace + '/pods/' + $json.pod }}}}", None, "k8s_actions"),
    code(W, "확인", (880, 200), ACT_CHECK),
    if_true(W, "격리된 파드?", (1100, 200), "={{ $json.ok }}"),
    http(W, "삭제", (1320, 100), "DELETE",
         f"={{{{ '{K8S}/api/v1/namespaces/' + $json.namespace + '/pods/' + $json.pod }}}}", None, "k8s_actions"),
    code(W, "삭제 결과", (1540, 100), ACT_DONE),
    # 재시작
    if_true(W, "재시작?", (660, 500), "={{ $json.op === 'restart' }}"),
    http(W, "Deployment 확인", (880, 440), "GET", _DEPLOY_API, None, "k8s_actions"),
    code(W, "재시작 판단", (1100, 440), RESTART_PLAN),
    if_true(W, "재시작 가능?", (1320, 440), "={{ $json.ok }}"),
    http(W, "재시작", (1540, 380), "PATCH", _DEPLOY_API, "={{ JSON.stringify($json.patch) }}", "k8s_actions",
         headers={"Content-Type": "application/merge-patch+json"}),
    code(W, "재시작 결과", (1760, 380), RESTART_DONE),
    # 증설·롤백 (배포 저장소 커밋)
    if_true(W, "Git 조치?", (880, 700), "={{ ['scale', 'rollback'].includes($json.op) }}"),
    http(W, "현재 파일", (1100, 700), "GET", _FILE("'main'"), None, "github", headers=GH),
    if_true(W, "롤백?", (1320, 700), "={{ $('요청 정리').item.json.op === 'rollback' }}"),
    http(W, "최근 커밋", (1540, 620), "GET",
         "={{ '" + DEPLOY_COMMITS.replace("__D__", "' + $('요청 정리').item.json.deploy + '") + "' }}", None, "github", headers=GH),
    code(W, "직전 커밋", (1760, 620), PREV_REF),
    http(W, "이전 파일", (1980, 620), "GET", _FILE("($json.prev_sha ?? 'main')"), None, "github", headers=GH),
    code(W, "파일 고치기", (2200, 700), GIT_EDIT),
    if_true(W, "커밋할 것?", (2420, 700), "={{ $json.ok }}"),
    http(W, "커밋", (2640, 640), "PUT",
         "={{ '" + DEPLOY_FILE.replace("__D__", "' + $json.deploy + '") + "' }}", "={{ JSON.stringify($json.put) }}", "github", headers=GH),
    code(W, "커밋 결과", (2860, 640), GIT_DONE),
    code(W, "답장 정리", (3080, 300), ACT_REPLY),
    http(W, "답장", (3300, 300), "POST", f"{GATEWAY}/slack/post", POST_SLACK, "gateway"),
    if_true(W, "카드 고칠 것?", (3520, 300), "={{ !!$('답장 정리').item.json.card }}"),
    http(W, "카드 고치기", (3740, 220), "POST", f"{GATEWAY}/slack/update", "={{ JSON.stringify($('답장 정리').item.json.card) }}", "gateway"),
    code(W, "감사 기록", (3960, 300), AUDIT_03),
    call_audit(W, "감사 (WF-99)", (4180, 300)),
], links(
    ("WF-01 에서 호출", "요청 정리"), ("요청 정리", "격리 파드 삭제?"),
    ("격리 파드 삭제?", "파드 확인", 0), ("격리 파드 삭제?", "재시작?", 1),
    ("파드 확인", "확인"), ("확인", "격리된 파드?"), ("격리된 파드?", "삭제", 0), ("격리된 파드?", "답장 정리", 1),
    ("삭제", "삭제 결과"), ("삭제 결과", "답장 정리"),
    ("재시작?", "Deployment 확인", 0), ("재시작?", "Git 조치?", 1),
    ("Deployment 확인", "재시작 판단"), ("재시작 판단", "재시작 가능?"), ("재시작 가능?", "재시작", 0), ("재시작 가능?", "답장 정리", 1),
    ("재시작", "재시작 결과"), ("재시작 결과", "답장 정리"),
    ("Git 조치?", "현재 파일", 0), ("Git 조치?", "답장 정리", 1),
    ("현재 파일", "롤백?"), ("롤백?", "최근 커밋", 0), ("롤백?", "파일 고치기", 1),
    ("최근 커밋", "직전 커밋"), ("직전 커밋", "이전 파일"), ("이전 파일", "파일 고치기"),
    ("파일 고치기", "커밋할 것?"), ("커밋할 것?", "커밋", 0), ("커밋할 것?", "답장 정리", 1),
    ("커밋", "커밋 결과"), ("커밋 결과", "답장 정리"),
    ("답장 정리", "답장"), ("답장", "카드 고칠 것?"), ("카드 고칠 것?", "카드 고치기", 0), ("카드 고칠 것?", "감사 기록", 1),
    ("카드 고치기", "감사 기록"), ("감사 기록", "감사 (WF-99)"),
))

# ---------------- WF-99 audit ----------------
W = WF["audit"]
ON_ERROR = """
// 다른 워크플로가 실패하면 여기로 온다 (워크플로 설정의 Error Workflow)
const e = $json;
return [{ json: { flow: e.workflow?.name ?? 'unknown', result: 'error', execution_id: e.execution?.id ?? null,
  failed_node: e.execution?.lastNodeExecuted ?? null, error: (e.execution?.error?.message ?? '').slice(0, 500) } }];
"""
ERROR_NOTICE = r"""
// 워크플로가 중간에 실패하면 사용자는 아무 답도 받지 못한다 ("조사하고 있습니다" 가 그대로 남거나 버튼을 눌러도 무응답).
// 어느 스레드였는지는 여기서 알 수 없으므로 알림 채널에 한 줄 올린다. 오류 글은 줄이고 Slack 서식 문자를 뺀다.
const e = $json;
const msg = String(e.error ?? '').replace(/[<>`*_~|]/g, ' ').slice(0, 200);
return [{ json: { text: `:x: 자동화 흐름 *${String(e.flow).replace(/[<>`*_~|]/g, ' ')}* 가 \`${String(e.failed_node ?? '?').replace(/[<>`*_~|]/g, ' ')}\` 단계에서 실패했습니다. 진행 중이던 조사나 버튼 처리가 끝나지 않았을 수 있습니다.\n_${msg} · 실행 ${e.execution_id ?? '?'}_` } }];
"""
audit = workflow(W, "WF-99 audit (감사 로그)", [
    node(W, "다른 흐름에서 호출", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, 100), {"inputSource": "passthrough"}),
    node(W, "흐름 실패", "n8n-nodes-base.errorTrigger", 1, (0, 300), {}),
    code(W, "오류 정리", (220, 300), ON_ERROR),
    http(W, "감사 로그 보내기", (440, 200), "POST", f"{GATEWAY}/audit",
         "={{ JSON.stringify({ ...$json, at: new Date().toISOString() }) }}", "gateway", timeout_ms=10000),
    code(W, "오류 알림 글", (440, 400), ERROR_NOTICE),
    http(W, "오류 알림", (660, 400), "POST", f"{GATEWAY}/slack/post", "={{ JSON.stringify({ text: $json.text }) }}", "gateway", timeout_ms=10000),
], links(("다른 흐름에서 호출", "감사 로그 보내기"), ("흐름 실패", "오류 정리"), ("오류 정리", "감사 로그 보내기"),
         ("오류 정리", "오류 알림 글"), ("오류 알림 글", "오류 알림")), error_wf=False)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for wf, fname in ((intake, "wf-01-intake.json"), (investigate, "wf-02-investigate.json"), (actions, "wf-03-actions.json"),
                      (mlops, "wf-04-mlops.json"),
                      (audit, "wf-99-audit.json")):
        (OUT / fname).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n")
        print(fname, len(wf["nodes"]), "nodes")

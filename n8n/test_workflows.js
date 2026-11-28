// 생성된 워크플로의 Code 노드를 꺼내 가짜 입력으로 실행해 본다
const fs = require('fs');
const dir = process.argv[2];
const load = f => JSON.parse(fs.readFileSync(`${dir}/${f}`, 'utf8'));
const codeOf = (wf, name) => wf.nodes.find(n => n.name === name).parameters.jsCode;
const run = (code, $json, nodes = {}) => new Function('$json', '$', '$execution', 'Buffer', code)($json, n => ({ item: { json: nodes[n] }, isExecuted: n in nodes }), { id: 'x1' }, Buffer);
const assert = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok  ', m); };
const b64 = s => Buffer.from(s).toString('base64');

const act = load('wf-03-actions.json');
const req = { op: 'rollback', deploy: 'demo-app', namespace: 'demo', user_id: 'U1', message_ts: '1000' };
const cur = { statusCode: 200, body: { content: b64('image: x@sha256:aaaaaaaaaaaa1\nreplicas: 2\n'), sha: 's' } };
const prev = { statusCode: 200, body: { content: b64('image: x@sha256:bbbbbbbbbbbb1\nreplicas: 2\n') } };
const edit = codeOf(act, '파일 고치기');
let pick = run(codeOf(act, '직전 커밋'), { body: [{ sha: 'abcdef123', commit: { message: 'demo: 새 이미지', committer: { date: new Date(900e3).toISOString() } } }, { sha: 'p1' }] }, { '요청 정리': req });
assert(pick[0].json.last_at && pick[0].json.prev_sha === 'p1', '직전 커밋: 시각과 이전 sha');
let out = run(edit, prev, { '요청 정리': req, '현재 파일': cur, '직전 커밋': pick[0].json });
assert(out[0].json.ok === true, '보고서보다 앞선 커밋은 롤백한다');
pick = run(codeOf(act, '직전 커밋'), { body: [{ sha: 'abcdef123', commit: { message: 'demo: replicas 2 → 3', committer: { date: new Date(1100e3).toISOString() } } }, { sha: 'p1' }] }, { '요청 정리': req });
out = run(edit, prev, { '요청 정리': req, '현재 파일': cur, '직전 커밋': pick[0].json });
assert(out[0].json.ok === false && /보고서가 올라온 뒤/.test(out[0].json.note), '보고서 뒤에 바뀐 파일은 롤백하지 않는다');
out = run(edit, cur, { '요청 정리': { ...req, op: 'scale' }, '현재 파일': cur });
assert(out[0].json.ok === true && /replicas 2 → 3/.test(out[0].json.what), '증설은 그대로 동작');

const inv = load('wf-02-investigate.json');
const prep = codeOf(inv, '알림 정리');
const alertOf = pod => ({ body: { status: 'firing', alerts: [{ status: 'firing', startsAt: '2026-11-28T00:00:00Z', labels: { alertname: 'DemoCompromiseBlocked', namespace: 'demo', pod, auto_action: 'quarantine' }, annotations: { summary: 's' } }] } });
assert(run(prep, alertOf('demo-app-abc-1'))[0].json.quarantine?.pod === 'demo-app-abc-1', '정상 파드 이름은 격리 대상');
assert(run(prep, alertOf('../../deployments/x'))[0].json.quarantine === null, '형식이 틀린 파드 이름은 격리하지 않는다');

const aud = load('wf-99-audit.json');
const err = run(codeOf(aud, '오류 정리'), { workflow: { name: 'WF-02 <!channel>' }, execution: { id: 77, lastNodeExecuted: '보고', error: { message: 'boom `x` <@U1>' } } });
const note = run(codeOf(aud, '오류 알림 글'), err[0].json);
assert(/WF-02/.test(note[0].json.text) && !/<!channel>|<@U1>/.test(note[0].json.text) && /실행 77/.test(note[0].json.text), '오류 알림 글: 서식 문자 제거');
assert(aud.connections['오류 정리'].main[0].length === 2, '오류 정리 → 감사 + 알림');

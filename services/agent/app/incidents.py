"""사건 기록: 조사 보고서를 사건 단위로 Postgres 에 남긴다.

왜: 보고서는 Slack 스레드에만 있었고, 근거가 된 원본 로그(Loki)는 7일 뒤 지워진다. "누가 언제 어떤 요청을
연달아 보냈나" 를 나중에 다시 볼 곳이 없었다 (11-27).

- 사건(incidents): 같은 네임스페이스에서 INCIDENT_GAP_MINUTES 안에 이어진 알림 조사를 하나로 묶는다. 보안 알림은 서로 묶고,
  장애 알림은 같은 알림이거나 같은 파드일 때만 묶는다 (이어서 난 다른 장애가 한 사건이 되지 않게)
  (예: WAF 차단 → 웹셸 차단 → 투하 실행 차단 = 한 번의 공격). 사람이 Slack 에서 물어 시작한 조사는 묶지 않고 하나씩.
- 보고(incident_reports): 조사 한 번. 알림 원문, 에이전트 보고서, 에이전트가 부른 도구와 결과,
  그리고 그 시점의 타임라인 스냅숏(막힌 요청의 출발 IP·규칙·시각, 커널 보안 이벤트)을 함께 넣는다.
  스냅숏은 LLM 없이 MCP 도구를 직접 불러 만든다 (비용 0).
- 조회: GET /incidents, GET /incidents/{id} (JSON + 마크다운). Slack 에서는 "@봇 사건 목록", "@봇 사건 12".
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .safety import NOTE, for_memory
from .toolbox import Toolbox

log = logging.getLogger("agent.incidents")

INCIDENT_GAP_MINUTES = 30
TIMELINE_MINUTES = 30
RESULT_CHARS = 2000  # 도구 결과는 앞부분만 (근거 확인용. 전문은 Loki 에 7일)
KST = timezone(timedelta(hours=9))

SCHEMA = (
    """
    create table if not exists incidents (
      id bigserial primary key,
      opened_at timestamptz not null default now(),
      last_at timestamptz not null default now(),
      namespace text not null,
      source text not null,
      severity text,
      title text not null,
      alert_names text[] not null default '{}',
      report_count integer not null default 0,
      cost_usd numeric(10, 4) not null default 0
    )
    """,
    """
    create table if not exists incident_reports (
      id bigserial primary key,
      incident_id bigint not null references incidents(id) on delete cascade,
      at timestamptz not null default now(),
      thread_id text,
      alertname text,
      question text,
      alert jsonb,
      report jsonb,
      tool_trace jsonb,
      timeline jsonb,
      stopped_by text,
      tool_calls integer,
      cost_usd numeric(10, 4)
    )
    """,
    "create index if not exists incident_reports_incident on incident_reports (incident_id, at)",
    # 묶는 기준 (11-27 추가): 보안 알림끼리, 또는 같은 알림·같은 파드의 장애 알림끼리
    "alter table incidents add column if not exists category text not null default 'ops'",
    "alter table incidents add column if not exists pods text[] not null default '{}'",
    "create index if not exists incidents_last_at on incidents (last_at desc)",
)
SEVERITY_RANK = {"critical": 3, "warning": 2, "ok": 1}
# 타임라인 스냅숏에 쓰는 도구 (있을 때만 부른다. demo 의 보안·요청 기록)
TIMELINE_TOOLS = (
    ("blocked_requests", "loki__waf_blocks", {"minutes": TIMELINE_MINUTES, "limit": 10}),
    ("kernel_events", "loki__security_events", {"minutes": TIMELINE_MINUTES, "limit": 30}),
)


def _unwrap(text: str) -> Any:
    """<tool_output> 포장을 벗기고, JSON 이면 객체로."""
    body = re.sub(r"^<tool_output>\n|\n</tool_output>$", "", text).removeprefix(NOTE)  # 지시문 주의 문구가 붙어 있으면 뗀다
    try:
        return json.loads(body)
    except ValueError:
        return body


def tool_trace(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """이번 조사에서 부른 도구 (마지막 limit 개): 이름, 인자, 결과 앞부분."""
    results: dict[str, Any] = {}
    uses: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if b.get("type") == "tool_use":
                uses.append(b)
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = b.get("content")
    out = []
    for b in uses[-limit:] if limit > 0 else []:
        if b.get("name") == "submit_report":
            continue
        res = results.get(b.get("id"))
        res = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        out.append({"tool": b.get("name"), "args": b.get("input"), "result": re.sub(r"^<tool_output>\n|\n</tool_output>$", "", res or "")[:RESULT_CHARS]})
    return out


async def snapshot(toolbox: Toolbox, alert: dict[str, Any] | None) -> dict[str, Any]:
    """조사 시점의 타임라인 재료. 실패해도 조사 결과는 그대로 저장한다."""
    out: dict[str, Any] = {"window_minutes": TIMELINE_MINUTES}
    if alert:
        out["alerts"] = [{"alertname": (a.get("labels") or {}).get("alertname"), "started_at": a.get("startsAt"),
                          "labels": a.get("labels"), "summary": (a.get("annotations") or {}).get("summary")} for a in alert.get("alerts") or []]
    have = {t["name"] for t in toolbox.tools}
    for key, name, args in TIMELINE_TOOLS:
        if name not in have:
            continue
        text, is_error = await toolbox.call(name, args)
        out[key] = {"error": _unwrap(text)} if is_error else _unwrap(text)
    return out


def alert_pods(alert: dict[str, Any] | None) -> set[str]:
    return {str(a.get("labels", {}).get("pod")) for a in (alert or {}).get("alerts") or [] if a.get("labels", {}).get("pod")}


def same_target(new: set[str], old: set[str]) -> bool:
    """새 알림이 앞의 조사와 같은 대상인가. 새로 나타난 파드가 하나라도 있으면 다른 일로 본다 (다시 조사한다)."""
    return new <= old


def repeat_answer(alertname: str, prior: dict[str, Any], minutes: int) -> str:
    report = prior.get("report") or {}
    ago = max(1, round(prior["age_seconds"] / 60))
    return (f"{ago}분 전에 같은 알림(`{alertname}`)을 같은 대상에 대해 조사했습니다 (사건 #{prior['incident_id']}).\n"
            f"그때의 결론: {str(report.get('headline') or '')[:300]}\n"
            f"{minutes}분 안에 다시 온 알림이라 새로 조사하지 않았습니다. 상황이 달라졌으면 이 스레드에서 봇을 불러 다시 조사를 요청하세요.")


class Incidents:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            for stmt in SCHEMA:
                await conn.execute(stmt)

    async def record(self, *, thread_id: str, question: str, alert: dict[str, Any] | None, report: dict[str, Any] | None,
                     trace: list[dict[str, Any]], timeline: dict[str, Any], stopped_by: str, tool_calls: int, cost_usd: float) -> dict[str, Any]:
        labels = ((alert or {}).get("alerts") or [{}])[0].get("labels") or {}
        alertname = labels.get("alertname")
        namespace = labels.get("namespace") or "-"
        source = "alert" if alert else "question"
        category = "security" if labels.get("category") == "security" else "ops"
        pods = sorted({str(a.get("labels", {}).get("pod")) for a in (alert or {}).get("alerts") or [] if a.get("labels", {}).get("pod")})
        severity = (report or {}).get("status") or labels.get("severity")
        title = (report or {}).get("headline") or question[:200]
        async with self.pool.connection() as conn, conn.transaction():
            row = None
            if alert:
                # 알림에서 온 조사만 묶는다. 30분 안에 이어졌다고 다 같은 일은 아니다 (지연 장애 뒤의 오류 장애는 다른 사건).
                #   보안: 같은 네임스페이스의 보안 알림은 한 공격의 단계로 본다 (WAF 차단 → 실행 차단 → 격리)
                #   장애: 같은 알림이 다시 울렸거나 같은 파드의 알림일 때만 (메모리 한도 근접 → 그 파드의 재시작)
                cur = await conn.execute(
                    "select id, severity from incidents where source = 'alert' and namespace = %s and category = %s "
                    "and last_at > now() - make_interval(mins => %s) "
                    "and (%s = 'security' or %s = any(alert_names) or pods && %s::text[]) "
                    "order by last_at desc limit 1 for update",
                    (namespace, category, INCIDENT_GAP_MINUTES, category, alertname or "", pods))
                row = await cur.fetchone()
            if row is None:
                cur = await conn.execute(
                    "insert into incidents (namespace, source, severity, title, alert_names, category, pods) values (%s, %s, %s, %s, %s, %s, %s) returning id",
                    (namespace, source, severity, title, [alertname] if alertname else [], category, pods))
                incident_id = (await cur.fetchone())["id"]
            else:
                incident_id = row["id"]
                worse = SEVERITY_RANK.get(severity or "", 0) > SEVERITY_RANK.get(row["severity"] or "", 0)
                await conn.execute(
                    "update incidents set last_at = now(), severity = %s, "
                    "alert_names = (select array(select distinct unnest(alert_names || %s::text[]))), "
                    "pods = (select array(select distinct unnest(pods || %s::text[]))) where id = %s",
                    (severity if worse else row["severity"], [alertname] if alertname else [], pods, incident_id))
            await conn.execute(
                "insert into incident_reports (incident_id, thread_id, alertname, question, alert, report, tool_trace, timeline, stopped_by, tool_calls, cost_usd) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (incident_id, thread_id, alertname, question, Jsonb(alert), Jsonb(report), Jsonb(trace), Jsonb(timeline), stopped_by, tool_calls, cost_usd))
            cur = await conn.execute(
                "update incidents set report_count = report_count + 1, cost_usd = cost_usd + %s where id = %s returning report_count",
                (cost_usd, incident_id))
            count = (await cur.fetchone())["report_count"]
        return {"incident_id": incident_id, "report_no": count}

    async def recent_report(self, alert: dict[str, Any], minutes: int) -> dict[str, Any] | None:
        """같은 알림을 같은 대상에 대해 최근에 조사한 보고가 있으면 돌려준다 (반복 조사 억제용)."""
        labels = (alert.get("alerts") or [{}])[0].get("labels") or {}
        alertname, namespace = labels.get("alertname"), labels.get("namespace") or "-"
        if not alertname or minutes <= 0:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select r.incident_id, r.alert, r.report, extract(epoch from now() - r.at)::int as age_seconds "
                "from incident_reports r join incidents i on i.id = r.incident_id "
                "where r.alertname = %s and i.namespace = %s and r.report is not null and r.at > now() - make_interval(mins => %s) "
                "order by r.at desc limit 5", (alertname, namespace, minutes))
            for row in await cur.fetchall():
                if same_target(alert_pods(alert), alert_pods(row["alert"])):
                    return {"incident_id": row["incident_id"], "age_seconds": row["age_seconds"], "report": row["report"]}
        return None

    async def list(self, limit: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select id, opened_at, last_at, namespace, source, severity, title, alert_names, report_count, cost_usd "
                "from incidents order by last_at desc limit %s", (limit,))
            return [_plain(r) for r in await cur.fetchall()]

    async def get(self, incident_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select * from incidents where id = %s", (incident_id,))
            inc = await cur.fetchone()
            if inc is None:
                return None
            cur = await conn.execute("select * from incident_reports where incident_id = %s order by at", (incident_id,))
            reports = [_plain(r) for r in await cur.fetchall()]
        return {**_plain(inc), "reports": reports}


def search_document(incident_id: int, report_no: int, alertname: str | None, namespace: str, report: dict[str, Any] | None, question: str,
                    timeline: dict[str, Any], pod: str | None = None, suspicious: int = 0) -> dict[str, Any]:
    """런북 검색 서버에 넣을 사건 보고 한 건 (마크다운). 나중에 similar_incidents 가 이 글을 찾는다.

    여기 들어가는 글은 모델이 쓴 것이고, 모델은 공격자가 만든 글(요청 경로, User-Agent)을 읽고 썼다. 다음 조사가 이 글을 다시 읽으므로
    글마다 for_memory 로 지시처럼 보이는 문장을 빼고 길이를 자른다. 그런 문장이 있었거나 조사 중 지시문 의심(suspicious)이 있었으면
    meta.tainted 로 표시한다 (검색 결과에 그대로 나간다).
    """
    tainted = suspicious > 0

    def safe(value: Any, limit: int = 400) -> str:
        nonlocal tainted
        text, hit = for_memory(value, limit)
        tainted = tainted or hit
        return text

    def walk(value: Any, limit: int) -> Any:
        if isinstance(value, str):
            return safe(value, limit)
        if isinstance(value, list):
            return [walk(v, limit) for v in value[:20]]
        if isinstance(value, dict):
            return {k: walk(v, limit) for k, v in value.items()}
        return value

    rep = walk({k: (report or {}).get(k) for k in ("status", "headline", "impact", "timeline", "causes", "actions")}, 400)
    rep = {k: v for k, v in rep.items() if v}
    headline = rep.get("headline") or safe(question, 200)
    lines = [f"# 사건 #{incident_id}: {headline}", "", f"알림 {alertname or '없음 (Slack 질문)'} · 대상 {namespace} · {_kst(datetime.now(timezone.utc).isoformat())} (KST)"]
    if rep.get("impact"):
        lines += ["", "## 영향", ""] + [f"- {x}" for x in rep["impact"]]
    if rep.get("timeline"):
        lines += ["", "## 경과", ""] + [f"- {t.get('time')} {t.get('event')}" for t in rep["timeline"]]
    if rep.get("causes"):
        lines += ["", "## 원인", ""]
        for c in rep["causes"]:
            lines += [f"- {c.get('title')} ({round((c.get('confidence') or 0) * 100)}%)"] + [f"  - {e}" for e in c.get("evidence") or []]
    acts = [a for a in rep.get("actions") or [] if a.get("type") != "none"]
    if acts:
        lines += ["", "## 조치 제안", ""] + [f"- {a['type']} {a['target']}: {a['reason']}" for a in acts]
    blocked = timeline.get("blocked_requests")
    ips = [ip.get("client_ip") for ip in (blocked.get("by_ip") or blocked.get("ips") or [])] if isinstance(blocked, dict) else []
    if ips:
        lines += ["", "## 출발 IP", "", ", ".join(map(str, ips[:10]))]
    # 값으로 찾을 개체 (incident_lookup). 형식 검사는 받는 쪽(mcp-runbook)이 한 번 더 한다.
    kernel = timeline.get("kernel_events")
    pods = [p.get("pod") for p in (kernel.get("by_pod") or kernel.get("pods") or [])] if isinstance(kernel, dict) else []
    rules = sorted({r for ip in (blocked.get("by_ip") or blocked.get("ips") or []) for r in (ip.get("rules") or {})}) if isinstance(blocked, dict) else []
    entities = {"alert": [alertname] if alertname else [], "ip": [str(x) for x in ips[:20] if x], "rule": sorted({str(r).split(":")[0].strip() for r in rules})[:20],  # "942100: SQL Injection ..." → 번호만 (받는 쪽은 공백이 든 값을 버린다)
                "pod": [str(p) for p in pods[:20] if p] + ([str(pod)] if pod else [])}
    return {"kind": "incident", "source": f"incident/{incident_id}/report/{report_no}", "title": f"사건 #{incident_id}: {headline}",
            "content": "\n".join(lines),
            "meta": {"incident_id": incident_id, "report_no": report_no, "alertname": alertname, "namespace": namespace, "severity": rep.get("status"),
                     "tainted": tainted, "entities": {k: v for k, v in entities.items() if v}}}


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).isoformat(timespec="seconds")
        elif hasattr(v, "is_finite"):  # Decimal
            v = float(v)
        out[k] = v
    return out


def _kst(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return iso


def timeline_rows(inc: dict[str, Any]) -> list[tuple[str, str]]:
    """사건의 일을 시간순으로: 알림 시작, 막힌 요청(출발 IP 별 처음~마지막), 커널 이벤트(파드별), 보고."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(at: str | None, text: str) -> None:
        key = f"{at}|{text}"
        if at and key not in seen:
            seen.add(key)
            rows.append((at, text))

    for r in inc["reports"]:
        tl = r.get("timeline") or {}
        for a in tl.get("alerts") or []:
            add(a.get("started_at"), f"알림 `{a.get('alertname')}` 시작: {a.get('summary') or ''}")
        blocked = tl.get("blocked_requests")
        if isinstance(blocked, dict):
            for ip in blocked.get("by_ip") or blocked.get("ips") or []:
                rules = ", ".join(list((ip.get("rules") or {}).keys())[:3]) if isinstance(ip.get("rules"), dict) else ""
                extra = f", 요청 수 제한 {ip['rate_limited']}건" if ip.get("rate_limited") else ""
                sample = next((f" · 예: {x.get('method')} {x.get('uri')}" for x in ip.get("samples") or []), "")
                add(ip.get("first"), f"출발 IP `{ip.get('client_ip')}` 의 요청이 막히기 시작 (WAF {ip.get('count', 0)}건{extra}, 마지막 {_kst(ip.get('last'))}){' · ' + rules if rules else ''}{sample}")
        kernel = tl.get("kernel_events")
        if isinstance(kernel, dict):
            for p in kernel.get("by_pod") or kernel.get("pods") or []:
                add(p.get("first"), f"파드 `{p.get('pod')}` 에서 커널 보안 이벤트 {p.get('count', 0)}건 시작 (시도 {', '.join(map(str, (p.get('targets') or [])[:3])) or '-'}, 조치 {', '.join(map(str, p.get('actions') or [])) or '-'}, 마지막 {_kst(p.get('last'))})")
        add(r.get("at"), f"에이전트 보고 #{r['id']}: {(r.get('report') or {}).get('headline') or r.get('question') or ''}")
    return sorted(rows)


def to_markdown(inc: dict[str, Any]) -> str:
    """사건 보고서 (사람이 읽는 글). Slack 답장과 파일 보관에 같이 쓴다."""
    sev = {"critical": "장애", "warning": "주의", "ok": "정상"}.get(inc.get("severity") or "", inc.get("severity") or "-")
    lines = [
        f"# 사건 #{inc['id']} · {inc['title']}",
        "",
        f"- 기간: {_kst(inc['opened_at'])} ~ {_kst(inc['last_at'])} (KST)",
        f"- 대상: `{inc['namespace']}` · 심각도 {sev} · 시작 {'알림' if inc['source'] == 'alert' else 'Slack 질문'}",
        f"- 알림: {', '.join(f'`{a}`' for a in inc.get('alert_names') or []) or '-'}",
        f"- 조사 {inc['report_count']}회 · ${inc['cost_usd']:.2f}",
        "",
        "## 시간순",
    ]
    rows = timeline_rows(inc)
    lines += [f"- {_kst(at)} {text}" for at, text in rows] or ["- (기록 없음)"]
    for r in inc["reports"]:
        rep = r.get("report") or {}
        lines += ["", f"## 보고 #{r['id']} · {_kst(r['at'])} · {r.get('alertname') or 'Slack 질문'}", "", rep.get("headline") or r.get("question") or ""]
        if rep.get("impact"):
            lines += ["", "**영향**"] + [f"- {x}" for x in rep["impact"]]
        if rep.get("timeline"):
            lines += ["", "**경과 (에이전트가 확인한 것)**"] + [f"- {t.get('time')} {t.get('event')}" for t in rep["timeline"]]
        for i, c in enumerate(rep.get("causes") or [], 1):
            lines += ["", f"**원인 {i}** ({round((c.get('confidence') or 0) * 100)}%) {c.get('title')}"] + [f"- {e}" for e in c.get("evidence") or []]
        acts = [a for a in rep.get("actions") or [] if a.get("type") != "none"]
        if acts:
            lines += ["", "**조치 제안**"] + [f"- `{a['type']}` {a['target']}: {a['reason']} (위험 {a['risk']})" for a in acts]
        if rep.get("data_gaps"):
            lines += ["", "**확인 못 한 것**"] + [f"- {x}" for x in rep["data_gaps"]]
        if rep.get("references"):
            lines += ["", "**참고**"] + [f"- {x}" for x in rep["references"]]
        trace = r.get("tool_trace") or []
        if trace:
            lines += ["", "**조사에 쓴 도구**"] + [f"- `{t['tool']}` {json.dumps(t.get('args'), ensure_ascii=False)}" for t in trace]
        lines += ["", f"_도구 {r.get('tool_calls') or 0}회 · ${r.get('cost_usd') or 0:.3f} · 종료 {r.get('stopped_by')} · Slack 스레드 {r.get('thread_id')}_"]
    return "\n".join(lines)

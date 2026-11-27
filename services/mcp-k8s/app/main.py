"""mcp-k8s — 쿠버네티스 상태를 읽기 전용으로 조회하는 MCP 서버.

도구: get_pod_status, get_events, describe_deployment, rollout_history
권한: demo 네임스페이스의 Role(get/list: pods, events, deployments, replicasets) 만. secrets 는 권한 자체가 없다.
코드에서도 ALLOWED_NAMESPACES 밖은 거절한다 (권한과 코드 이중 제한).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from mcp.server.mcpserver import MCPServer

from mcp_common import ToolInputError, check_namespace, instrumented, serve, to_json

try:
    config.load_incluster_config()
except config.ConfigException:  # 로컬 개발
    config.load_kube_config()

core = client.CoreV1Api()
apps = client.AppsV1Api()

server = MCPServer(
    name="mcp-k8s",
    instructions="쿠버네티스 리소스 상태 조회 (읽기 전용). 파드, 이벤트, Deployment, 롤아웃 이력.",
)


NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")  # 리소스 이름 (DNS subdomain)
LABEL_SELECTOR = re.compile(r"[A-Za-z0-9_./-]{1,253}(=|==|!=)[A-Za-z0-9_.-]{0,63}(,[A-Za-z0-9_./-]{1,253}(=|==|!=)[A-Za-z0-9_.-]{0,63}){0,7}")


def _name(value: str, what: str = "name") -> str:
    """이름은 선택자 문자열에 들어간다. 쉼표나 = 가 섞이면 조건이 덧붙으므로 형식을 먼저 본다."""
    if not NAME.fullmatch(value or ""):
        raise ToolInputError(f"{what} 은 쿠버네티스 리소스 이름 형식이어야 합니다 (소문자, 숫자, -, .)")
    return value


def _age(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    sec = int((datetime.now(timezone.utc) - ts).total_seconds())
    return f"{sec // 3600}h{sec % 3600 // 60}m" if sec >= 3600 else f"{sec // 60}m{sec % 60}s"


async def _call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ApiException as e:
        if e.status in (403, 404):
            raise ToolInputError(f"{e.status} {e.reason}: 대상이 없거나 조회 권한이 없습니다") from e
        raise


def _container_state(cs) -> dict:
    out = {"name": cs.name, "ready": cs.ready, "restarts": cs.restart_count, "image": cs.image}
    if cs.state.waiting:
        out["waiting"] = cs.state.waiting.reason
    if cs.state.terminated:
        out["terminated"] = cs.state.terminated.reason
    last = cs.last_state.terminated if cs.last_state else None
    if last:
        out["last_terminated"] = {"reason": last.reason, "exit_code": last.exit_code, "finished": last.finished_at}
    return out


@server.tool()
@instrumented
async def get_pod_status(namespace: str = "demo", label_selector: str | None = None) -> str:
    """파드 목록과 상태 (phase, 준비, 재시작 수, 마지막 종료 이유 예: OOMKilled, 대기 이유 예: CrashLoopBackOff).

    label_selector 예: app.kubernetes.io/name=demo-app
    """
    ns = check_namespace(namespace)  # 범위 검사가 먼저 (다른 입력 검사나 API 호출보다 앞)
    if label_selector and not LABEL_SELECTOR.fullmatch(label_selector):
        raise ToolInputError("label_selector 는 key=value 를 쉼표로 이은 형식이어야 합니다 (예: app.kubernetes.io/name=demo-app)")
    pods = await _call(core.list_namespaced_pod, ns, label_selector=label_selector or "", limit=50)
    items = [_pod_row(p) for p in pods.items]
    return to_json({"namespace": namespace, "count": len(items), "pods": items})


def _pod_row(p) -> dict:
    """멀쩡한 파드(Running, 전부 준비됨, 재시작·종료 기록 없음)는 한 줄로 줄인다. 문제가 있는 파드만 컨테이너별로 자세히.

    파드 8개에 2,300자였고 대부분 멀쩡한 파드의 이미지 주소였다. 이미지·개정은 rollout_history 가 알려 준다.
    """
    statuses = p.status.container_statuses or []
    row = {"name": p.metadata.name, "phase": p.status.phase, "node": p.spec.node_name, "age": _age(p.metadata.creation_timestamp),
           "ready": f"{sum(1 for cs in statuses if cs.ready)}/{len(statuses)}", "restarts": sum(cs.restart_count for cs in statuses)}
    labels = p.metadata.labels or {}
    if labels.get("security.kube-chatops/quarantine"):
        row["quarantined"] = True
    not_true = [{"type": c.type, "status": c.status, "reason": c.reason} for c in (p.status.conditions or []) if c.status != "True"]
    healthy = (p.status.phase == "Running" and statuses and all(cs.ready and not cs.restart_count and not (cs.last_state and cs.last_state.terminated) for cs in statuses)
               and not not_true)
    if not healthy:
        row["containers"] = [_container_state(cs) for cs in statuses]
        row["conditions"] = not_true
    return row


@server.tool()
@instrumented
async def get_events(namespace: str = "demo", minutes: int = 60, object_name: str | None = None, warnings_only: bool = False) -> str:
    """최근 `minutes` 분(최대 1440) 의 이벤트를 최신순으로 최대 50개. object_name 으로 특정 리소스만 볼 수 있다."""
    ns = check_namespace(namespace)
    if not 1 <= minutes <= 1440:
        raise ToolInputError("minutes 는 1~1440 사이여야 합니다")
    selector = f"involvedObject.name={_name(object_name, 'object_name')}" if object_name else ""
    events = await _call(core.list_namespaced_event, ns, field_selector=selector)
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = []
    for e in events.items:
        last = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        if last is None or last < since or (warnings_only and e.type != "Warning"):
            continue
        rows.append(
            {
                "last": last,
                "type": e.type,
                "reason": e.reason,
                "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                "count": e.count,
                "message": (e.message or "")[:400],
            }
        )
    rows.sort(key=lambda r: r["last"], reverse=True)
    return to_json({"namespace": namespace, "minutes": minutes, "count": len(rows), "events": rows[:50]})


@server.tool()
@instrumented
async def describe_deployment(name: str, namespace: str = "demo") -> str:
    """Deployment 의 원하는 상태와 실제 상태, 컨테이너 이미지와 리소스, 프로브, 상태 조건."""
    ns, name = check_namespace(namespace), _name(name)
    d = await _call(apps.read_namespaced_deployment, name, ns)
    containers = []
    for c in d.spec.template.spec.containers:
        res = c.resources
        containers.append(
            {
                "name": c.name,
                "image": c.image,
                "requests": res.requests if res else None,
                "limits": res.limits if res else None,
                "env_names": [e.name for e in (c.env or [])],
                "readiness_probe": bool(c.readiness_probe),
                "liveness_probe": bool(c.liveness_probe),
            }
        )
    s = d.status
    return to_json(
        {
            "name": name,
            "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
            "replicas": {"desired": d.spec.replicas, "ready": s.ready_replicas or 0, "updated": s.updated_replicas or 0, "available": s.available_replicas or 0, "unavailable": s.unavailable_replicas or 0},
            "strategy": d.spec.strategy.type if d.spec.strategy else None,
            "selector": d.spec.selector.match_labels,
            "containers": containers,
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message, "last_update": c.last_update_time} for c in (s.conditions or [])],
        }
    )


@server.tool()
@instrumented
async def rollout_history(name: str, namespace: str = "demo") -> str:
    """Deployment 의 롤아웃 이력 (ReplicaSet 리비전별 이미지, 생성 시각, 현재 복제본 수). 최근 배포가 장애와 겹치는지 볼 때 쓴다."""
    ns, name = check_namespace(namespace), _name(name)
    d = await _call(apps.read_namespaced_deployment, name, ns)
    selector = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
    rss = await _call(apps.list_namespaced_replica_set, ns, label_selector=selector)
    rows = []
    for rs in rss.items:
        if not any(o.kind == "Deployment" and o.name == name for o in (rs.metadata.owner_references or [])):
            continue
        ann = rs.metadata.annotations or {}
        rows.append(
            {
                "revision": int(ann.get("deployment.kubernetes.io/revision", "0")),
                "replicaset": rs.metadata.name,
                "created": rs.metadata.creation_timestamp,
                "age": _age(rs.metadata.creation_timestamp),
                "replicas": rs.status.replicas or 0,
                "images": [c.image for c in rs.spec.template.spec.containers],
                "change_cause": ann.get("kubernetes.io/change-cause"),
            }
        )
    rows.sort(key=lambda r: r["revision"], reverse=True)
    return to_json({"deployment": name, "current_revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"), "history": rows[:10]})


if __name__ == "__main__":
    serve(server, "mcp-k8s")

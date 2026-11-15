"""查询审计日志接口：登记审计记录，覆盖标记复核、归档记录等动作。

读接口的分工：
- 列表 / 明细：审计记录（执行事实）+ 最新复核事件（状态），按执行时间稳定排序；
- 复核事件：``/events`` 返回完整事件流，每条事件带事件号、版本号和操作时间；
- 导出：``/export`` 生成整份清单快照，成功后才落库，失败不覆盖上一版。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit import STATUS_ORDER, AuditService

router = APIRouter(prefix="/api/audit", tags=["查询审计日志"])

service = AuditService()

LIST_FIELDS = ["记录编号", "查询语句", "执行人", "执行耗时", "扫描行数", "返回行数", "执行时间", "复核状态"]
STATUSES = STATUS_ORDER


def _resolve_action_payload(payload: EntryPayload) -> dict[str, Any]:
    """兼容两种写法：``{"values": {...}}`` 与 ``{"action": ..., "版本号": ...}``。"""
    values = dict(payload.values)
    if payload.action is not None:
        values.setdefault("action", payload.action)
    if payload.version is not None:
        values.setdefault("版本号", payload.version)
    if payload.operator is not None:
        values.setdefault("操作人", payload.operator)
    return values


def _parse_version(values: dict[str, Any]) -> int | None:
    raw = values.get("版本号")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="版本号必须是整数") from None


# 注意：静态路径必须注册在 /{entry_id} 之前，否则会被动态路由抢先匹配。
@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已复核、已归档"),
) -> dict[str, Any]:
    """导出查询审计日志清单：一次构建完整快照，失败时保留上一版快照。"""
    return service.export_entries(keyword=keyword, status=status)


@router.get("/export/snapshots")
def list_snapshots() -> dict[str, Any]:
    """查看历史导出快照的批次号与导出时间，用于核对清单版本。"""
    snapshots = [
        {key: value for key, value in snapshot.items() if key != "items"}
        for snapshot in service.list_snapshots()
    ]
    return {"module": "audit", "total": len(snapshots), "items": snapshots}


@router.get("/events")
def list_events(entry_id: int | None = Query(default=None, description="按审计记录 id 过滤")) -> dict[str, Any]:
    """读取复核事件流：新动作只在这里追加一条带版本和操作时间的事件。"""
    events = service.list_events(entry_id=entry_id)
    return {"module": "audit", "total": len(events), "items": events}


@router.get("/duration-summary")
def duration_summary() -> dict[str, Any]:
    """查询耗时汇总的唯一读取口径，优化建议等模块共用这一份事实。"""
    return service.query_duration_summary()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已复核、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤查询审计日志列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条审计记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条审计记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(_resolve_action_payload(payload))
    if entry is None:
        return ActionResult(ok=False, message="；".join(missing))
    return ActionResult(ok=True, message="审计记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条审计记录执行标记复核、归档记录。

    状态只能单向推进；重复归档幂等并保留首次事件号。请求体里带 ``版本号``（或顶层
    ``version``）时按该版本做并发校验，两个批次同时写同一条记录只有一个能成功。
    """
    values = _resolve_action_payload(payload)
    action = str(values.get("action") or "").strip()
    version = _parse_version(values)
    operator = str(values.get("操作人") or "").strip() or None
    entry, message = service.run_action(
        entry_id,
        action,
        expected_version=version,
        operator=operator,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

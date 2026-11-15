"""查询优化建议业务规则：状态流转、字段校验与筛选口径都收在这里。

优化建议不单独保存查询耗时副本：列表里的“当前耗时”统一从查询审计日志的耗时事实
（``AuditService.query_duration_summary``）按源记录编号读取，保证同一执行时间下的
多条审计记录在审计清单与优化建议里汇总一致。
"""
from __future__ import annotations

from typing import Any

from app.services.audit import audit_service
from app.store import store

MODULE = "opt"
REQUIRED_FIELDS = ["建议编号", "目标查询", "优化类型"]
STATUS_ORDER = ["待采纳", "已采纳", "已执行", "已关闭"]
ACTION_RULES = {"采纳建议": "待采纳", "标记执行": "已采纳", "关闭建议": "已执行"}
NEGATIVE_ACTIONS = []


class OptService:
    def _with_duration(self, row: dict[str, Any]) -> dict[str, Any]:
        """按目标查询（源记录编号）读取唯一一份查询耗时汇总。"""
        view = dict(row)
        target = str(view.get("目标查询") or "")
        duration = audit_service.duration_for(target)
        if duration is not None:
            view["当前耗时"] = duration
            view["耗时来源"] = "查询审计日志"
        else:
            view["耗时来源"] = "未匹配到审计记录"
        return view

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._with_duration(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("建议编号", ""))
                or keyword in str(row.get("目标查询", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._with_duration(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in REQUIRED_FIELDS + ["预计提速", "优化方案", "建议时间"]:
            if field in values:
                entry[field] = values.get(field)
        entry.setdefault("建议时间", "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self._with_duration(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"优化建议 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询优化建议可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._with_duration(entry), f"优化建议已{action}"

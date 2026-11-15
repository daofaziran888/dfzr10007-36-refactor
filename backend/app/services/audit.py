"""查询审计日志业务规则。

边界划分：
- 审计记录（``store.rows("audit")``）只保存查询执行事实，登记后不再因复核动作改写，
  列表按“执行时间 + 记录编号”稳定排序，历史清单的顺序和内容可重复。
- 复核事件（``store.internal_rows("audit_review_events")``）单独追加，一个动作只生成
  一条事件，事件自带版本号与操作时间；复核状态由事件流推导，不散落在多个模块。
- 导出快照（``store.internal_rows("audit_export_snapshots")``）在完整构建成功后才落库，
  构建中途失败不会覆盖上一版清单。

状态只能按 已记录 -> 已复核 -> 已归档 单向推进。重复归档是幂等的：返回首次归档的
事件号，不追加第二条事件。写事件时用版本号做乐观并发校验，两个批次同时写同一条审计
记录只有一个能提交。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

MODULE = "audit"
EVENTS = "audit_review_events"
SNAPSHOTS = "audit_export_snapshots"
REQUIRED_FIELDS = ["记录编号", "查询语句", "执行人"]
STATUS_ORDER = ["已记录", "已复核", "已归档"]
ACTION_RULES = {"标记复核": "已复核", "归档记录": "已归档"}
NEGATIVE_ACTIONS: list[str] = []

# 历史记录的操作时间在原始数据里只有执行日期，补齐版本时用当天零点作为占位，
# 并标记来源，避免把补齐时间误当成新的复核动作时间。
LEGACY_TIME_SUFFIX = "T00:00:00+08:00"
CN_TZ = timezone(timedelta(hours=8))

_lock = threading.RLock()


def _sorted_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按执行时间、记录编号、id 排序；同执行时间也不会因写入顺序不同而漂移。"""
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("执行时间") or ""),
            str(row.get("记录编号") or ""),
            int(row.get("id", 0)),
        ),
    )


def _record_no(row: dict[str, Any]) -> str:
    """源记录编号缺失时回落到记录编号，保证旧数据也能被事件引用。"""
    return str(row.get("源记录编号") or row.get("记录编号") or "")


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(event.get("操作时间") or ""),
        str(event.get("事件号") or ""),
        int(event.get("id", 0)),
    )


class AuditService:
    # ------------------------------------------------------------------ 公共视图
    def _events(self) -> list[dict[str, Any]]:
        return store.internal_rows(EVENTS)

    def _latest_event(self, record_no: str) -> dict[str, Any] | None:
        events = [event for event in self._events() if event.get("源记录编号") == record_no]
        if not events:
            return None
        return max(events, key=lambda event: (int(event.get("版本号", 0)), *_event_sort_key(event)))

    def _current_version(self, record_no: str) -> int:
        events = [event for event in self._events() if event.get("源记录编号") == record_no]
        return max((int(event.get("版本号", 0)) for event in events), default=0)

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        """把不可变的审计记录与最新复核事件合成为列表/导出共用的展示视图。"""
        event = self._latest_event(_record_no(row))
        view = dict(row)
        view["源记录编号"] = _record_no(row)
        if event is not None:
            view["复核状态"] = event["复核状态"]
            view["status"] = event["复核状态"]
            view["复核事件号"] = event["事件号"]
            view["版本号"] = event["版本号"]
            view["操作时间"] = event["操作时间"]
            view["复核动作"] = event["动作"]
            view["pending"] = event["复核状态"] != STATUS_ORDER[-1]
        else:
            view.setdefault("复核状态", view.get("status") or STATUS_ORDER[0])
            view.setdefault("复核事件号", None)
            view.setdefault("版本号", 0)
            view.setdefault("操作时间", None)
            view.setdefault("复核动作", None)
        return view

    # -------------------------------------------------------------- 历史数据补齐
    def _ensure_events(self) -> None:
        """为既有审计记录补齐复核事件。

        版本号补齐规则（旧记录缺失版本时）：
        1. 一条记录若完全没有事件，则按其当前复核状态补一条基线事件，版本号取该状态在
           ``已记录 -> 已复核 -> 已归档`` 中的位次（1/2/3），事件号沿用记录上已有的
           “复核事件号”，没有则按执行日期生成；
        2. 基线事件的操作时间取执行日期当天零点（+08:00），并标注“操作来源=历史补齐”，
           不与新产生的复核事件混淆；
        3. 已有事件但版本号缺失或重复时，按“操作时间 -> 事件号 -> 内部 id”升序排列，
           合法的版本号原样保留，缺失或冲突的补入下一个未占用的版本号，保证同一记录内
           版本号唯一且随操作时间单调不减；
        4. 整个补齐过程幂等，重复调用不会追加重复事件。
        """
        records = _sorted_records(store.rows(MODULE))
        known_numbers = {str(event.get("事件号")) for event in self._events() if event.get("事件号")}
        for row in records:
            record_no = _record_no(row)
            events = [event for event in self._events() if event.get("源记录编号") == record_no]
            if not events:
                event_no = str(row.get("复核事件号") or "").strip()
                if not event_no or event_no in known_numbers:
                    event_no = self._issue_event_no(str(row.get("执行时间") or ""), known_numbers)
                known_numbers.add(event_no)
                status = str(row.get("复核状态") or row.get("status") or STATUS_ORDER[0])
                if status not in STATUS_ORDER:
                    status = STATUS_ORDER[0]
                event = {
                    "id": self._next_event_id(),
                    "事件号": event_no,
                    "源记录编号": record_no,
                    "记录Id": int(row.get("id", 0)),
                    "动作": self._action_for_status(status),
                    "复核状态": status,
                    "版本号": STATUS_ORDER.index(status) + 1,
                    "操作时间": self._legacy_operation_time(row),
                    "操作来源": "历史补齐",
                    "执行耗时": row.get("执行耗时"),
                    "执行时间": row.get("执行时间"),
                }
                self._events().append(event)
                continue
            # 已有事件但版本号缺失或重复时，按时间顺序补齐：
            # 保留合法且不冲突的版本号，缺失的取“当前序号”，保证版本随时间单调不减。
            used_versions: set[int] = set()
            next_version = 1
            for event in sorted(events, key=_event_sort_key):
                version = int(event.get("版本号") or 0)
                if version and version >= next_version and version not in used_versions:
                    used_versions.add(version)
                    next_version = version + 1
                    continue
                while next_version in used_versions:
                    next_version += 1
                event["版本号"] = next_version
                used_versions.add(next_version)
                next_version += 1
        # 事件号也可能在补齐过程中撞号，按时间顺序重新确保唯一，保留最早事件的原号。
        seen: set[str] = set()
        for event in sorted(self._events(), key=_event_sort_key):
            event_no = str(event.get("事件号") or "")
            if not event_no or event_no in seen:
                base_date = str(event.get("操作时间") or "")[:10].replace("-", "")
                event["事件号"] = self._issue_event_no(base_date, seen)
            seen.add(str(event["事件号"]))

    @staticmethod
    def _action_for_status(status: str) -> str:
        return {
            "已记录": "登记审计记录",
            "已复核": "标记复核",
            "已归档": "归档记录",
        }.get(status, "登记审计记录")

    @staticmethod
    def _legacy_operation_time(row: dict[str, Any]) -> str:
        execution_time = str(row.get("执行时间") or "").strip()
        if execution_time:
            return f"{execution_time[:10]}{LEGACY_TIME_SUFFIX}"
        return f"1970-01-01{LEGACY_TIME_SUFFIX}"

    # ------------------------------------------------------------------ 事件编号
    @staticmethod
    def _date_part(value: str | None) -> str:
        normalized = (value or "").strip().replace("-", "")[:8]
        if len(normalized) == 8 and normalized.isdigit():
            return normalized
        return datetime.now(CN_TZ).strftime("%Y%m%d")

    @classmethod
    def _issue_event_no(cls, date_hint: str, known_numbers: set[str]) -> str:
        date_part = cls._date_part(date_hint)
        sequence = 1
        while True:
            candidate = f"{date_part}-{sequence:02d}"
            if candidate not in known_numbers:
                return candidate
            sequence += 1

    def _next_event_id(self) -> int:
        return max((int(event.get("id", 0)) for event in self._events()), default=0) + 1

    def _append_event(
        self,
        row: dict[str, Any],
        action: str,
        target: str,
        *,
        operator: str | None,
        operation_time: datetime,
        source: str = "本次复核",
    ) -> dict[str, Any]:
        """追加且只追加一条复核事件，版本号在源记录维度单调 +1。"""
        record_no = _record_no(row)
        known_numbers = {str(event.get("事件号")) for event in self._events() if event.get("事件号")}
        event = {
            "id": self._next_event_id(),
            "事件号": self._issue_event_no(operation_time.strftime("%Y%m%d"), known_numbers),
            "源记录编号": record_no,
            "记录Id": int(row.get("id", 0)),
            "动作": action,
            "复核状态": target,
            "版本号": self._current_version(record_no) + 1,
            "操作时间": operation_time.isoformat(timespec="seconds"),
            "操作来源": source,
            "操作人": operator or "",
            "执行耗时": row.get("执行耗时"),
            "执行时间": row.get("执行时间"),
        }
        self._events().append(event)
        return event

    # ------------------------------------------------------------------ 查询接口
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with _lock:
            self._ensure_events()
            rows = _sorted_records(store.rows(MODULE))
            views = [self._view(row) for row in rows]
            if keyword:
                views = [
                    view for view in views
                    if keyword in str(view.get("记录编号", ""))
                    or keyword in str(view.get("源记录编号", ""))
                ]
            if status:
                views = [view for view in views if view.get("复核状态") == status]
            total = len(views)
            start = max(page - 1, 0) * size
            return views[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with _lock:
            self._ensure_events()
            row = store.find(MODULE, entry_id)
            return self._view(row) if row is not None else None

    def list_events(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        """返回复核事件流；按事件号/版本号可追溯到每一次状态推进。"""
        with _lock:
            self._ensure_events()
            events = list(self._events())
            if entry_id is not None:
                events = [event for event in events if int(event.get("记录Id", 0)) == entry_id]
            return sorted(events, key=_event_sort_key)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记审计记录；返回 (记录, 错误说明列表)。

        缺少必填字段与记录编号重复都会给出可读说明，调用方直接展示错误列表。
        """
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        with _lock:
            self._ensure_events()
            rows = store.rows(MODULE)
            record_no = str(values.get("记录编号") or "").strip()
            if any(_record_no(row) == record_no for row in rows):
                return None, [f"记录编号 {record_no} 已存在，请勿重复登记"]
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in REQUIRED_FIELDS + ["执行耗时", "扫描行数", "返回行数", "执行时间"]:
                if field in values:
                    entry[field] = values.get(field)
            entry.setdefault("执行耗时", "")
            entry.setdefault("扫描行数", 0)
            entry.setdefault("返回行数", 0)
            entry.setdefault("执行时间", datetime.now(CN_TZ).strftime("%Y-%m-%d"))
            entry["源记录编号"] = record_no
            entry["复核状态"] = STATUS_ORDER[0]
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["复核事件号"] = ""
            rows.append(entry)
            # 登记本身是一条基线事件：版本 1、带操作时间，后续复核在此基础上递增。
            self._append_event(
                entry,
                "登记审计记录",
                STATUS_ORDER[0],
                operator=str(values.get("操作人") or values.get("执行人") or ""),
                operation_time=datetime.now(CN_TZ),
                source="本次登记",
            )
            entry["复核事件号"] = self._latest_event(record_no)["事件号"]
            return self._view(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        expected_version: int | None = None,
        operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """单向推进复核状态；重复归档幂等，并发写入按版本号只允许一个成功。

        并发语义：请求进入时先记下看到的版本（观察值），提交前在锁内比对。两个批次
        同时写同一条审计记录时，只有先提交的一方能把版本推进；后提交的一方观察值已经
        过期，按“本次写入未生效”拒绝，事件流里不会出现两条同版本事件。
        """
        # 观察版本在进入提交临界区前读取：先补齐全历史事件，再记录当前版本，
        # 提交时与它比对即可识别出并发批次。
        with _lock:
            self._ensure_events()
            snapshot = store.find(MODULE, entry_id)
        observed_version = (
            self._current_version(_record_no(snapshot)) if snapshot is not None else None
        )
        with _lock:
            self._ensure_events()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"审计记录 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于查询审计日志可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

            current = str(self._view(entry).get("复核状态") or STATUS_ORDER[0])
            current_version = self._current_version(_record_no(entry))
            if expected_version is not None and expected_version != current_version:
                return None, (
                    f"审计记录 {entry_id} 已被其他批次更新（期望版本 {expected_version}，"
                    f"当前版本 {current_version}），本次写入未生效"
                )
            if observed_version is not None and observed_version != current_version:
                return None, (
                    f"审计记录 {entry_id} 在本次请求期间已被其他批次写入"
                    f"（观察到版本 {observed_version}，当前版本 {current_version}），本次写入未生效"
                )

            current_index = STATUS_ORDER.index(current) if current in STATUS_ORDER else 0
            target_index = STATUS_ORDER.index(target)

            if target_index < current_index:
                return None, f"审计状态只能按{'、'.join(STATUS_ORDER)}单向推进，当前已是{current}，不能回退到{target}"
            if target_index == current_index:
                if target == STATUS_ORDER[-1]:
                    event = self._latest_event(_record_no(entry))
                    event_no = event["事件号"] if event else "—"
                    return self._view(entry), f"审计记录已归档，重复归档不产生新事件（保留首次事件 {event_no}）"
                return self._view(entry), f"审计记录当前已是{current}，本次未重复生成复核事件"
            if target_index > current_index + 1:
                return None, f"审计状态只能按{'、'.join(STATUS_ORDER)}逐级推进，请先完成复核"

            self._append_event(
                entry,
                action,
                target,
                operator=operator,
                operation_time=datetime.now(CN_TZ),
                source="本次复核",
            )
            return self._view(entry), f"审计记录{action}成功"

    # ------------------------------------------------------------ 查询耗时事实汇总
    def query_duration_summary(self) -> dict[str, Any]:
        """查询耗时汇总的唯一来源。

        汇总以“源记录编号”为最小粒度，同一执行时间下的多条记录各自保留耗时，不会因为
        时间相同或写入顺序不同而给出互相矛盾的汇总。查询优化建议直接读这份结果，不再
        自己保存一份耗时副本。
        """
        with _lock:
            self._ensure_events()
            rows = _sorted_records(store.rows(MODULE))
            by_record: dict[str, dict[str, Any]] = {}
            by_time: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                record_no = _record_no(row)
                execution_time = str(row.get("执行时间") or "")
                fact = {
                    "记录编号": record_no,
                    "执行耗时": row.get("执行耗时"),
                    "执行时间": execution_time,
                    "扫描行数": row.get("扫描行数"),
                    "返回行数": row.get("返回行数"),
                }
                # 以源记录编号为键：AUDI-0002 的 820ms 与 AUDI-0003 的 1.4s 各自独立。
                by_record[record_no] = fact
                by_time.setdefault(execution_time, []).append(
                    {"记录编号": record_no, "执行耗时": row.get("执行耗时")}
                )
            return {
                "记录数": len(by_record),
                "按记录编号": by_record,
                "按执行时间": by_time,
            }

    def duration_for(self, record_no: str) -> str | None:
        fact = self.query_duration_summary()["按记录编号"].get(str(record_no))
        return None if fact is None else fact.get("执行耗时")

    # ------------------------------------------------------------------ 导出快照
    def export_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """导出一份完整清单快照。

        只有列表、视图和快照对象全部构建成功后才追加到快照账本；构建过程中出现异常时
        直接抛出，上一版快照保持原样。导出接口与列表共用同一份审计记录和复核事件视图。
        """
        with _lock:
            self._ensure_events()
            views, total = self.list_entries(keyword=keyword, status=status, page=1, size=100000)
            export_time = datetime.now(CN_TZ).isoformat(timespec="seconds")
            snapshots = store.internal_rows(SNAPSHOTS)
            snapshot_id = max((int(item.get("id", 0)) for item in snapshots), default=0) + 1
            batch_no = self._issue_snapshot_no(export_time, snapshots)
            items = [
                {**view, "导出时间": export_time, "导出批次号": batch_no}
                for view in views
            ]
            # 先完整构建，成功后才写账本；任何一步失败都不会触碰上一版快照。
            snapshot = {
                "id": snapshot_id,
                "批次号": batch_no,
                "导出时间": export_time,
                "源记录编号": [item.get("源记录编号") for item in items],
                "total": total,
                # 快照保存独立副本，接口返回对象被调用方修改时不会回写历史清单。
                "items": [dict(item) for item in items],
            }
            snapshots.append(snapshot)
            return {
                "module": MODULE,
                "total": total,
                "导出时间": export_time,
                "导出批次号": batch_no,
                "源记录编号": snapshot["源记录编号"],
                "items": items,
            }

    @staticmethod
    def _issue_snapshot_no(export_time: str, snapshots: list[dict[str, Any]]) -> str:
        date_part = export_time[:10].replace("-", "")
        used = {str(item.get("批次号")) for item in snapshots}
        sequence = 1
        while True:
            candidate = f"AUDI-EXP-{date_part}-{sequence:02d}"
            if candidate not in used:
                return candidate
            sequence += 1

    def list_snapshots(self) -> list[dict[str, Any]]:
        """只读查看历史导出快照；快照一旦写入不再被后续导出改写。"""
        with _lock:
            self._ensure_events()
            return list(store.internal_rows(SNAPSHOTS))


def _overview_view(row: dict[str, Any]) -> dict[str, Any]:
    """概览统计用的轻量视图：状态来自最新复核事件，不触发事件补齐。"""
    view = dict(row)
    events = [
        event
        for event in store.internal_rows(EVENTS)
        if event.get("源记录编号") == _record_no(row)
    ]
    if events:
        latest = max(events, key=lambda event: (int(event.get("版本号", 0)), *_event_sort_key(event)))
        view["status"] = latest["复核状态"]
        view["pending"] = latest["复核状态"] != STATUS_ORDER[-1]
    return view


# 让运营概览与列表读同一份复核状态，避免两个入口给出不同的待处理量。
store.register_viewer(MODULE, _overview_view)

audit_service = AuditService()

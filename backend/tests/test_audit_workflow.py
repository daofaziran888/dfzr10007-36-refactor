"""查询审计日志版本边界回归测试。

覆盖：既有 AUDI-0001~0003 清单稳定、复核事件号/导出时间/源记录编号、
单向状态推进、重复归档幂等、并发写入只生效一个、导出失败不覆盖上一版快照、
旧记录版本补齐规则，以及优化建议共用同一份查询耗时汇总。

运行：backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import audit as audit_module  # noqa: E402
from app.services.audit import AuditService  # noqa: E402
from app.store import store  # noqa: E402


class AuditVersionBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = AuditService()
        self.client = TestClient(app)

    # ------------------------------------------------------------ 既有清单稳定
    def test_legacy_records_keep_execution_order_and_event_numbers(self) -> None:
        items, total = self.service.list_entries(page=1, size=20)
        self.assertEqual(total, 3)
        self.assertEqual([row["记录编号"] for row in items], ["AUDI-0001", "AUDI-0002", "AUDI-0003"])
        self.assertEqual(
            [row["复核事件号"] for row in items],
            ["20260901-01", "20260902-02", "20260903-03"],
        )
        self.assertEqual([row["执行时间"] for row in items], ["2026-09-01", "2026-09-02", "2026-09-03"])

    def test_repeated_reads_are_stable(self) -> None:
        first, _ = self.service.list_entries(page=1, size=20)
        second, _ = self.service.list_entries(page=1, size=20)
        self.assertEqual(
            [(row["记录编号"], row["复核事件号"], row["执行耗时"]) for row in first],
            [(row["记录编号"], row["复核事件号"], row["执行耗时"]) for row in second],
        )
        # 事件补齐是幂等的：多读几次不会多出事件。
        self.assertEqual(len(self.service.list_events()), 3)

    def test_same_execution_time_durations_are_not_merged(self) -> None:
        store.rows("audit")[0]["执行时间"] = "2026-09-02"
        store.rows("audit")[1]["执行时间"] = "2026-09-02"
        summary = self.service.query_duration_summary()
        self.assertEqual(summary["按记录编号"]["AUDI-0001"]["执行耗时"], "600ms")
        self.assertEqual(summary["按记录编号"]["AUDI-0002"]["执行耗时"], "820ms")
        same_day = summary["按执行时间"]["2026-09-02"]
        self.assertEqual(
            sorted((item["记录编号"], item["执行耗时"]) for item in same_day),
            [("AUDI-0001", "600ms"), ("AUDI-0002", "820ms")],
        )

    def test_original_durations_are_preserved_per_event(self) -> None:
        events = {event["源记录编号"]: event for event in self.service.list_events()}
        self.assertEqual(events["AUDI-0002"]["执行耗时"], "820ms")
        self.assertEqual(events["AUDI-0003"]["执行耗时"], "1.4s")
        self.assertEqual(events["AUDI-0002"]["事件号"], "20260902-02")
        self.assertEqual(events["AUDI-0003"]["事件号"], "20260903-03")

    # ------------------------------------------------------------ 新动作只生成一条事件
    def test_new_action_appends_single_versioned_event(self) -> None:
        entry, message = self.service.run_action(1, "标记复核", operator="值班管理员")
        self.assertIsNotNone(entry)
        self.assertIn("成功", message)
        events = [event for event in self.service.list_events(1)]
        self.assertEqual(len(events), 2)
        latest = events[-1]
        self.assertEqual(latest["复核状态"], "已复核")
        self.assertEqual(latest["版本号"], 2)
        self.assertTrue(latest["操作时间"])
        self.assertEqual(latest["动作"], "标记复核")

        # 再执行一次不会生成第三条事件。
        again, message = self.service.run_action(1, "标记复核")
        self.assertIsNotNone(again)
        self.assertIn("未重复生成", message)
        self.assertEqual(len(self.service.list_events(1)), 2)

    def test_status_progresses_one_way_only(self) -> None:
        blocked, message = self.service.run_action(3, "标记复核")
        self.assertIsNone(blocked)
        self.assertIn("不能回退", message)
        # 不允许跳过复核直接归档。
        skipped, message = self.service.run_action(1, "归档记录")
        self.assertIsNone(skipped)
        self.assertIn("逐级推进", message)

    def test_repeated_archive_keeps_first_event(self) -> None:
        first, _ = self.service.run_action(3, "归档记录")
        self.assertIsNotNone(first)
        first_event = first["复核事件号"]
        count_after_first = len(self.service.list_events(3))

        second, message = self.service.run_action(3, "归档记录")
        self.assertIsNotNone(second)
        self.assertIn("重复归档不产生新事件", message)
        self.assertIn(first_event, message)
        self.assertEqual(second["复核事件号"], first_event)
        self.assertEqual(len(self.service.list_events(3)), count_after_first)

    # ------------------------------------------------------------ 并发写入
    def test_two_batches_writing_same_record_only_one_effective(self) -> None:
        results: list[tuple[bool, str]] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            entry, message = self.service.run_action(1, "标记复核", expected_version=1)
            results.append((entry is not None, message))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        succeeded = [item for item in results if item[0]]
        failed = [item for item in results if not item[0]]
        self.assertEqual(len(succeeded), 1, results)
        self.assertEqual(len(failed), 1, results)
        self.assertIn("本次写入未生效", failed[0][1])
        self.assertEqual(len(self.service.list_events(1)), 2)

    def test_unversioned_concurrent_writes_still_only_one_effective(self) -> None:
        results: list[tuple[bool, str]] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            entry, message = self.service.run_action(1, "标记复核")
            results.append((entry is not None, message))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 未带版本号时，后到的批次识别出状态已推进，按幂等处理；只有一次真正生效。
        self.assertEqual(sum(1 for _, message in results if "成功" in message), 1, results)
        self.assertEqual(len(self.service.list_events(1)), 2)
        self.assertEqual(self.service.get_entry(1)["复核状态"], "已复核")

    # ------------------------------------------------------------ 导出快照
    def test_export_contains_time_and_source_numbers(self) -> None:
        payload = self.client.get("/api/audit/export").json()
        self.assertEqual(payload["total"], 3)
        self.assertTrue(payload["导出时间"])
        self.assertEqual(payload["源记录编号"], ["AUDI-0001", "AUDI-0002", "AUDI-0003"])
        for item in payload["items"]:
            self.assertEqual(item["导出时间"], payload["导出时间"])
            self.assertIn(item["源记录编号"], payload["源记录编号"])
        self.assertEqual(
            [(item["源记录编号"], item["执行耗时"], item["复核事件号"]) for item in payload["items"]],
            [
                ("AUDI-0001", "600ms", "20260901-01"),
                ("AUDI-0002", "820ms", "20260902-02"),
                ("AUDI-0003", "1.4s", "20260903-03"),
            ],
        )

    def test_export_failure_keeps_previous_snapshot(self) -> None:
        self.client.get("/api/audit/export")
        before = self.service.list_snapshots()
        self.assertEqual(len(before), 1)

        original = AuditService.list_entries

        def broken(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("清单构建失败")

        AuditService.list_entries = broken  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                self.service.export_entries()
        finally:
            AuditService.list_entries = original  # type: ignore[assignment]

        after = self.service.list_snapshots()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["批次号"], before[0]["批次号"])
        self.assertEqual(after[0]["total"], 3)

    # ------------------------------------------------------------ 版本补齐与耗时汇总
    def test_legacy_events_without_version_are_backfilled(self) -> None:
        events = store.internal_rows(audit_module.EVENTS)
        # AUDI-0001：一条已有版本 1，一条缺失版本 -> 补齐为 2。
        events.append(
            {
                "id": 90,
                "事件号": "20260901-01",
                "源记录编号": "AUDI-0001",
                "记录Id": 1,
                "动作": "登记审计记录",
                "复核状态": "已记录",
                "版本号": 1,
                "操作时间": "2026-09-01T00:00:00+08:00",
                "操作来源": "历史补齐",
            }
        )
        events.append(
            {
                "id": 91,
                "事件号": "20260905-05",
                "源记录编号": "AUDI-0001",
                "记录Id": 1,
                "动作": "标记复核",
                "复核状态": "已复核",
                "操作时间": "2026-09-05T00:00:00+08:00",
                "操作来源": "历史补齐",
            }
        )
        # AUDI-0002：两条都缺失版本 -> 按操作时间补 1、2。
        for index, event_no in enumerate(("20260906-06", "20260907-07"), start=1):
            events.append(
                {
                    "id": 91 + index,
                    "事件号": event_no,
                    "源记录编号": "AUDI-0002",
                    "记录Id": 2,
                    "动作": "标记复核",
                    "复核状态": "已复核",
                    "操作时间": f"2026-09-0{5 + index}T00:00:00+08:00",
                    "操作来源": "历史补齐",
                }
            )

        self.service.list_entries()

        versions = {
            event["事件号"]: event["版本号"]
            for event in self.service.list_events()
            if event["事件号"] in {"20260901-01", "20260905-05", "20260906-06", "20260907-07"}
        }
        self.assertEqual(versions["20260901-01"], 1)
        self.assertEqual(versions["20260905-05"], 2)
        self.assertEqual(versions["20260906-06"], 1)
        self.assertEqual(versions["20260907-07"], 2)

        # 补齐后再次读取不会改变结果，也不会重复追加事件。
        self.service.list_entries()
        self.assertEqual(len(self.service.list_events()), len(events))

    def test_create_entry_validates_and_starts_at_recorded(self) -> None:
        bad = self.client.post("/api/audit", json={"values": {"记录编号": "AUDI-0100"}}).json()
        self.assertFalse(bad["ok"])
        self.assertIn("缺少必填字段", bad["message"])

        created = self.client.post(
            "/api/audit",
            json={
                "values": {
                    "记录编号": "AUDI-0100",
                    "查询语句": "SELECT 1",
                    "执行人": "测试",
                    "执行耗时": "200ms",
                    "执行时间": "2026-09-05",
                }
            },
        ).json()
        self.assertTrue(created["ok"])
        entry = created["entry"]
        self.assertEqual(entry["复核状态"], "已记录")
        self.assertEqual(entry["版本号"], 1)
        self.assertTrue(entry["复核事件号"])

        duplicate = self.client.post(
            "/api/audit",
            json={"values": {"记录编号": "AUDI-0100", "查询语句": "SELECT 1", "执行人": "测试"}},
        ).json()
        self.assertFalse(duplicate["ok"])
        self.assertIn("已存在", duplicate["message"])

        # 新登记的记录出现在清单末尾，既有三条顺序不变。
        items, total = self.service.list_entries(page=1, size=20)
        self.assertEqual(total, 4)
        self.assertEqual([row["记录编号"] for row in items][:3], ["AUDI-0001", "AUDI-0002", "AUDI-0003"])

    def test_opt_reads_same_duration_summary(self) -> None:
        payload = self.client.get("/api/opt").json()
        durations = {
            item["建议编号"]: (item["目标查询"], item["当前耗时"])
            for item in payload["items"]
        }
        summary = self.client.get("/api/audit/duration-summary").json()
        self.assertEqual(durations["OPT-0002"], ("AUDI-0002", summary["按记录编号"]["AUDI-0002"]["执行耗时"]))
        self.assertEqual(durations["OPT-0002"][1], "820ms")
        self.assertEqual(durations["OPT-0003"][1], "1.4s")

    def test_export_route_not_shadowed_by_detail_route(self) -> None:
        response = self.client.get("/api/audit/export")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["module"], "audit")


if __name__ == "__main__":
    unittest.main()

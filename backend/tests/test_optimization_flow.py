"""表优化执行计划链路的回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
每个用例开始前重置内存仓库，避免批次编号、容量版本互相影响。
"""
from __future__ import annotations

import unittest
from copy import deepcopy

from app.seed import SEED_ROWS
from app.services.iceberg import IcebergService
from app.services.stats import StatsService
from app.store import store


def reset_store() -> None:
    store._tables = {
        name: [deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
    }


class OptimizationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.iceberg = IcebergService()
        self.stats = StatsService()

    def _publish(self, plan_code: str, result: dict | None = None) -> dict:
        for action in ("确认计划", "执行计划"):
            plan, message, failure = self.iceberg.run_plan_action(plan_code, action)
            self.assertIsNone(failure, message)
        plan, message, failure = self.iceberg.run_plan_action(
            plan_code,
            "完成计划",
            {
                "任务结果": result
                or [{"任务编号": "ICEB-0001", "实际文件数": 4100, "实际容量GB": 510}],
                "恢复结论": "快照恢复校验通过",
            },
        )
        self.assertIsNone(failure, message)
        plan, message, failure = self.iceberg.run_plan_action(plan_code, "发布批次")
        self.assertIsNone(failure, message)
        return plan

    def test_budget_is_never_overspent_and_same_table_kept_once(self) -> None:
        plan, message = self.iceberg.create_plan({"预算总量GB": 520})
        self.assertIsNotNone(plan, message)
        self.assertLessEqual(plan["预算占用GB"], 520)
        self.assertEqual(plan["剩余预算GB"], 520 - plan["预算占用GB"])
        tables = [item["表名称"] for item in plan["被选任务明细"]]
        self.assertEqual(len(tables), len(set(tables)))
        # 低收益的大表不应因为登记顺序靠后而被优先执行。
        selected_codes = plan["被选任务"]
        self.assertNotIn("ICEB-0005", selected_codes)
        self.assertIn("ICEB-0002", selected_codes)

    def test_single_table_example_numbers(self) -> None:
        plan, message = self.iceberg.create_plan(
            {"预算总量GB": 520, "任务编号": ["ICEB-0001"]}
        )
        self.assertIsNotNone(plan, message)
        self.assertEqual(plan["被选任务"], ["ICEB-0001"])
        self.assertEqual(plan["优化前文件数"], 18600)
        self.assertEqual(plan["优化前容量GB"], 860.0)
        self.assertEqual(plan["优化后文件数"], 4100)
        self.assertEqual(plan["预算占用GB"], 520)

    def test_concurrent_same_table_reuses_one_plan(self) -> None:
        first, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        second, message = self.iceberg.create_plan(
            {"预算总量GB": 400, "任务编号": ["ICEB-0006"]}
        )
        self.assertIsNotNone(second, message)
        self.assertEqual(first["批次编号"], second["批次编号"])
        plans = [p for p in store.rows("plans") if p["批次编号"] == first["批次编号"]]
        self.assertEqual(len(plans), 1)

    def test_status_only_moves_in_confirm_order(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        _, message, failure = self.iceberg.run_plan_action(plan["批次编号"], "完成计划")
        self.assertIsNotNone(failure)
        self.assertIn("只允许从", message)
        _, message, failure = self.iceberg.run_plan_action(plan["批次编号"], "发布批次")
        self.assertIsNotNone(failure)

    def test_capacity_conflict_blocks_progress_and_can_freeze(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        for action in ("确认计划", "执行计划"):
            self.iceberg.run_plan_action(plan["批次编号"], action)
        self.iceberg.update_capacity(
            {"items": [{"表名称": "dws_customer_360", "文件数": 18000, "容量GB": 850}]}
        )
        _, message, failure = self.iceberg.run_plan_action(plan["批次编号"], "完成计划")
        self.assertIsNotNone(failure)
        self.assertIn("版本冲突", message)
        plan, message, failure = self.iceberg.run_plan_action(plan["批次编号"], "沿用冻结顺序")
        self.assertIsNone(failure, message)
        self.assertFalse(plan["版本冲突"])
        self.assertEqual(plan["冲突处理"], "沿用冻结顺序")
        self.assertEqual(
            [item["任务编号"] for item in plan["被选任务明细"]], ["ICEB-0001"]
        )

    def test_resort_rebuilds_batch_by_new_capacity(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520})
        self.iceberg.update_capacity(
            {"items": [{"表名称": "dwd_order_detail", "文件数": 22000, "容量GB": 1100}]}
        )
        plan, message, failure = self.iceberg.run_plan_action(plan["批次编号"], "按新统计重排")
        self.assertIsNone(failure, message)
        self.assertEqual(plan["计划版本"], 2)
        self.assertEqual(plan["冲突处理"], "按新统计重排")
        self.assertLessEqual(plan["预算占用GB"], plan["预算总量GB"])

    def test_effect_and_report_written_after_publish(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        published = self._publish(plan["批次编号"])
        self.assertEqual(published["状态"], "已发布")
        self.assertEqual(published["实际文件数"], 4100)
        self.assertEqual(published["实际容量GB"], 510.0)
        self.assertEqual(published["文件数变化"], -14500)
        self.assertEqual(published["容量变化GB"], -350.0)

        effects, total = self.iceberg.list_effects(table="dws_customer_360")
        self.assertEqual(total, 1)
        self.assertEqual(effects[0]["批次编号"], plan["批次编号"])
        self.assertEqual(effects[0]["被选任务"], ["ICEB-0001"])
        self.assertTrue(effects[0]["是否最终批次"])

        reports, _ = self.stats.list_entries(keyword=plan["批次编号"])
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report["文件总数"], 4100)
        self.assertEqual(report["总容量"], "510GB")
        self.assertEqual(report["被选任务"], ["ICEB-0001"])
        self.assertEqual(report["文件数变化"], -14500)
        self.assertEqual(report["容量变化"], "-350GB")
        self.assertEqual(report["恢复结论"], "快照恢复校验通过")

    def test_history_reports_stay_readable(self) -> None:
        rows, total = self.stats.list_entries(keyword="STAT-0005")
        self.assertEqual(total, 1)
        self.assertTrue(rows[0]["是否历史记录"])
        self.assertEqual(rows[0]["被选任务"], [])
        self.assertEqual(rows[0]["批次编号"], "")

    def test_stats_cannot_roll_back_published_version(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        self._publish(plan["批次编号"])
        reports, _ = self.stats.list_entries(keyword=plan["批次编号"])
        report = reports[0]
        entry, message = self.stats.run_action(report["id"], "标记过期")
        self.assertIsNotNone(entry, message)
        entry, message = self.stats.run_action(report["id"], "采集统计")
        self.assertIsNone(entry)
        self.assertIn("已发布版本", message)

    def test_plans_support_filter_sort_and_paging(self) -> None:
        self.iceberg.create_plan({"预算总量GB": 420})
        self.iceberg.create_plan({"预算总量GB": 300, "任务编号": ["ICEB-0003"]})
        items, total, summary = self.iceberg.list_plans(
            min_benefit=100, sort="capacity", page=1, size=1
        )
        self.assertEqual(len(items), 1)
        self.assertGreaterEqual(total, 1)
        self.assertEqual(summary["计划总数"], total)
        items, total, _ = self.iceberg.list_plans(table="ads_traffic_daily")
        self.assertTrue(any("ICEB-0003" in item["被选任务"] for item in items))

    def test_task_action_cannot_bypass_plan(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        task = self.iceberg.get_entry(1)
        # 计划还没确认执行时，任务不能靠“执行优化”抢先变成执行中。
        entry, message = self.iceberg.run_action(int(task["id"]), "执行优化")
        self.assertIsNone(entry, message)
        self.assertIn("执行计划", message)
        # 计划执行中，任务不能越过批次直接归档。
        for action in ("确认计划", "执行计划"):
            self.iceberg.run_plan_action(plan["批次编号"], action)
        entry, message = self.iceberg.run_action(int(task["id"]), "归档任务")
        self.assertIsNone(entry, message)
        self.assertIn("只允许从已完成发起", message)
        # 计划执行中也不能把任务单独取消，否则同表会重新被别的批次选中。
        entry, message = self.iceberg.run_action(int(task["id"]), "取消优化")
        self.assertIsNone(entry, message)
        self.assertIn("执行期间不能单独取消", message)

    def test_diagnose_points_to_failed_step(self) -> None:
        plan, _ = self.iceberg.create_plan({"预算总量GB": 520, "任务编号": ["ICEB-0001"]})
        self.iceberg.run_plan_action(plan["批次编号"], "完成计划")
        diagnosis = self.iceberg.diagnose_plan(plan["批次编号"])
        self.assertEqual(diagnosis["失败环节"], "状态推进")
        self.assertIn("推进执行计划状态", diagnosis["调用链"][4]["步骤"])
        self.assertEqual(diagnosis["调用链"][4]["结果"], "失败")


if __name__ == "__main__":
    unittest.main()

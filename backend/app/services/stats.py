"""表统计信息业务规则：状态流转、历史记录兼容与执行计划版本边界校验。

统计报告只记录“最终执行批次”的实际效果：
- 没有执行计划字段的历史报告照常读取，回填为历史记录标记；
- 同一个批次重复采信时，只接受不早于已发布版本的统计版本；
- 已发布版本的统计报告不允许回退，状态只能按 草稿 → 已采集 → 已过期 → 已清理 单向推进。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "stats"
REQUIRED_FIELDS = ["报告编号", "表名称", "行总数"]
STATUS_ORDER = ["草稿", "已采集", "已过期", "已清理"]
ACTION_RULES = {"采集统计": "草稿", "标记过期": "已采集", "清理报告": "已过期"}
NEGATIVE_ACTIONS: list[str] = []

# 历史报告没有这些字段时按“历史记录”读取，不参与版本边界校验。
PLAN_FIELDS = ("批次编号", "执行计划版本", "被选任务", "被选任务明细", "文件数变化", "容量变化")


class StatsService:
    @staticmethod
    def _normalize(entry: dict[str, Any]) -> dict[str, Any]:
        """兼容历史统计报告：缺字段时补齐可读的默认值，而不是报错或丢字段。"""
        for field in PLAN_FIELDS:
            if field == "被选任务" or field == "被选任务明细":
                entry.setdefault(field, [])
            elif field in ("文件数变化", "容量变化"):
                entry.setdefault(field, 0 if field == "文件数变化" else "0GB")
            else:
                entry.setdefault(field, "")
        if not entry.get("批次编号") and "是否历史记录" not in entry:
            entry["是否历史记录"] = True
        entry.setdefault("是否历史记录", False)
        entry.setdefault("是否最终批次", bool(entry.get("批次编号")))
        entry.setdefault("统计版本", int(entry.get("执行计划版本") or 0))
        entry.setdefault("发布版本", 0)
        return entry

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            needle = keyword.strip().lower()
            rows = [
                row
                for row in rows
                if needle in str(row.get("报告编号", "")).lower()
                or needle in str(row.get("表名称", "")).lower()
                or needle in str(row.get("批次编号", "")).lower()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._normalize(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._normalize(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["报告状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for field in ("批次编号", "执行计划版本", "被选任务", "被选任务明细", "文件数变化", "容量变化"):
            if field in values:
                entry[field] = values[field]
        rows.append(entry)
        return self._normalize(entry), []

    def _version_guard(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[bool, str]:
        """核对执行计划版本边界：已发布版本不能回退，历史记录不参与。"""
        code = str(entry.get("批次编号") or "")
        published = int(entry.get("发布版本") or 0)
        if not code:
            return True, ""
        incoming_published = int(
            values.get("发布版本") if values.get("发布版本") is not None else published
        )
        incoming_version = int(
            values.get("执行计划版本")
            if values.get("执行计划版本") is not None
            else entry.get("执行计划版本") or entry.get("统计版本") or 0
        )
        if published and incoming_published and incoming_published < published:
            return False, f"批次 {code} 已发布版本 {published}，不允许回退到 {incoming_published}"
        if published and incoming_version and incoming_version < published:
            return False, f"批次 {code} 的统计版本 {incoming_version} 早于已发布版本 {published}"
        if values.get("统计版本") is not None:
            incoming_stats = int(values.get("统计版本") or 0)
            current_stats = int(entry.get("统计版本") or 0)
            if current_stats and incoming_stats < current_stats:
                return False, f"批次 {code} 的统计版本不允许从 {current_stats} 回退到 {incoming_stats}"
        return True, ""

    def _sync_effects(self, entry: dict[str, Any]) -> None:
        """统计报告处理结果同步回优化效果统计，只更新同批次且未发布的记录。"""
        code = str(entry.get("批次编号") or "")
        if not code:
            return
        for effect in store.rows("effects"):
            if str(effect.get("批次编号") or "") != code:
                continue
            effect["统计版本"] = max(
                int(effect.get("统计版本") or 0), int(entry.get("统计版本") or 0)
            )
            if entry.get("报告状态") or entry.get("status"):
                effect["统计状态"] = entry.get("status")
            if entry.get("恢复结论"):
                effect["恢复结论"] = entry.get("恢复结论")
            effect["是否最终批次"] = bool(entry.get("是否最终批次", effect.get("是否最终批次")))

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"统计报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表统计信息可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        # 已发布批次只能前进，统计模块不允许把它回退到更早的状态。
        published = int(entry.get("发布版本") or 0)
        current = entry.get("status")
        if published and current in STATUS_ORDER and target in STATUS_ORDER:
            if STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
                return None, (
                    f"统计报告 {entry.get('报告编号')} 对应批次已发布版本 {published}，"
                    f"不允许从{current}回退到{target}"
                )

        allowed, reason = self._version_guard(self._normalize(entry), values)
        if not allowed:
            return None, reason
        if values.get("执行计划版本") is not None:
            entry["执行计划版本"] = int(values.get("执行计划版本") or 0)
        if values.get("统计版本") is not None:
            entry["统计版本"] = int(values.get("统计版本") or 0)
        if values.get("恢复结论"):
            entry["恢复结论"] = values.get("恢复结论")

        entry["status"] = target
        entry["报告状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if entry.get("批次编号"):
            entry["是否最终批次"] = True
        self._sync_effects(entry)
        return entry, f"统计报告已{action}"

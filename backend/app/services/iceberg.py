"""表优化管理业务规则：任务登记、执行计划批次、预算排序与效果统计。

这一层同时承担“表优化执行计划”的状态机：
候选任务按“预计收益 / 预算需求”排序（不再按登记顺序抢预算），
在预算总量内贪心选择，同一张表在任一时刻只允许出现在一个活动计划里。
计划按 草稿 → 已确认 → 执行中 → 已完成 → 已发布 单向推进，
容量统计版本发生变化时，只允许“沿用冻结顺序”或“按新统计重排”两种处理方式。
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any, Iterable

from app.store import store

MODULE = "iceberg"
PLAN_MODULE = "plans"
EFFECT_MODULE = "effects"
CAPACITY_MODULE = "capacity"

REQUIRED_FIELDS = ["任务编号", "表名称", "优化类型"]
STATUS_ORDER = ["待执行", "执行中", "已完成", "已取消"]
ACTION_RULES = {"执行优化": "执行中", "取消优化": "已取消", "归档任务": "已完成"}
ACTION_PRECONDITIONS = {
    "执行优化": ("待执行",),
    "取消优化": ("待执行", "执行中"),
    "归档任务": ("已完成",),
}
NEGATIVE_ACTIONS = ["取消优化"]

PLAN_STATUS_ORDER = ["草稿", "已确认", "执行中", "已完成", "已发布"]
# 计划一旦创建就占用表级并发名额，直到发布完成才释放；
# 否则同表再建一个计划会在效果统计里互相覆盖。
ACTIVE_PLAN_STATUSES = ["草稿", "已确认", "执行中", "已完成"]
FINAL_PLAN_STATUSES = ["已完成", "已发布"]

LOCK = threading.RLock()

NUMERIC_FIELDS = {
    "文件数": int,
    "容量GB": float,
    "预算需求GB": float,
    "预计收益GB": float,
    "优化后文件数": int,
    "优化后容量GB": float,
}

PLAN_ACTION_ALIASES = {
    "确认计划": "确认计划",
    "确认": "确认计划",
    "confirm": "确认计划",
    "执行计划": "执行计划",
    "执行": "执行计划",
    "run": "执行计划",
    "完成计划": "完成计划",
    "完成": "完成计划",
    "complete": "完成计划",
    "发布批次": "发布批次",
    "发布计划": "发布批次",
    "发布": "发布批次",
    "publish": "发布批次",
    "沿用冻结顺序": "沿用冻结顺序",
    "沿用": "沿用冻结顺序",
    "冻结顺序": "沿用冻结顺序",
    "freeze": "沿用冻结顺序",
    "按新统计重排": "按新统计重排",
    "重排": "按新统计重排",
    "resort": "按新统计重排",
}

PLAN_TRANSITIONS = {
    "确认计划": ("草稿", "已确认"),
    "执行计划": ("已确认", "执行中"),
    "完成计划": ("执行中", "已完成"),
    "发布批次": ("已完成", "已发布"),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def to_number(value: Any, default: float = 0.0) -> float:
    """把 520、'520GB'、'520 GB' 一类输入统一成数字，解析失败给默认值。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    for suffix in ("GB", "gb", "Gb", "g", "G", "个", " "):
        text = text.replace(suffix, "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _pretty_gb(value: float) -> str:
    if abs(value - int(value)) < 1e-9:
        return f"{int(value)}GB"
    return f"{value:.1f}GB"


def _signed(value: float) -> str:
    return f"+{value:g}" if value > 0 else f"{value:g}"


class IcebergService:
    # ------------------------------------------------------------------ 任务
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
                if needle in str(row.get("任务编号", "")).lower()
                or needle in str(row.get("表名称", "")).lower()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with LOCK:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field, caster in NUMERIC_FIELDS.items():
                default = 0 if caster is int else 0.0
                entry[field] = caster(to_number(values.get(field), default))
            entry["登记顺序"] = max((int(row.get("登记顺序", 0)) for row in rows), default=0) + 1
            entry["收益分"] = self._score(entry)
            entry["计划版本"] = 0
            entry["批次编号"] = ""
            entry["实际文件数"] = 0
            entry["实际容量GB"] = 0.0
            entry["status"] = STATUS_ORDER[0]
            entry["任务状态"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"优化任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表优化管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        allowed_from = ACTION_PRECONDITIONS.get(action, ())
        if allowed_from and entry.get("status") not in allowed_from:
            return None, (
                f"优化任务当前为{entry.get('status')}，{action}只允许从"
                f"{'、'.join(allowed_from)}发起"
            )
        with LOCK:
            plan = self._find_plan_by_code(str(entry.get("批次编号") or ""))
            plan_active = plan is not None and plan.get("状态") in ACTIVE_PLAN_STATUSES
            if action == "执行优化" and plan_active:
                return None, (
                    f"任务 {entry.get('任务编号')} 属于执行计划 {entry.get('批次编号')}，"
                    "必须通过执行计划推进，确认并执行计划后才能进入执行中"
                )
            if action == "取消优化" and plan_active:
                return None, (
                    f"任务 {entry.get('任务编号')} 属于执行计划 {entry.get('批次编号')}，"
                    "执行期间不能单独取消，请先处理执行计划"
                )
            if action == "归档任务" and entry.get("批次编号"):
                effect = next(
                    (
                        item
                        for item in store.rows(EFFECT_MODULE)
                        if item.get("批次编号") == entry.get("批次编号")
                    ),
                    None,
                )
                if effect is None:
                    return None, (
                        f"任务 {entry.get('任务编号')} 的效果尚未回写批次 {entry.get('批次编号')}，"
                        "请通过执行计划完成批次后再归档"
                    )
            entry["status"] = target
            entry["任务状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"优化任务已{action}"

    # -------------------------------------------------------------- 执行计划
    @staticmethod
    def _score(task: dict[str, Any]) -> float:
        cost = to_number(task.get("预算需求GB"))
        if cost <= 0:
            return 0.0
        return round(to_number(task.get("预计收益GB")) / cost, 4)

    @staticmethod
    def _plan_tables(plan: dict[str, Any]) -> list[str]:
        tables: list[str] = []
        for item in plan.get("被选任务明细") or []:
            table = str(item.get("表名称") or "")
            if table and table not in tables:
                tables.append(table)
        if not tables:
            for item in plan.get("候选顺序") or []:
                if item.get("被选中"):
                    table = str(item.get("表名称") or "")
                    if table and table not in tables:
                        tables.append(table)
        return tables

    def _active_plan_tables(self, exclude_plan_id: int | None = None) -> dict[str, str]:
        """表名 → 占用该表的活动批次编号。已发布批次不再占用。"""
        active: dict[str, str] = {}
        for plan in store.rows(PLAN_MODULE):
            if int(plan.get("id", 0)) == int(exclude_plan_id or 0):
                continue
            if plan.get("状态") not in ACTIVE_PLAN_STATUSES:
                continue
            for table in self._plan_tables(plan):
                active.setdefault(table, str(plan.get("批次编号") or ""))
        return active

    def _active_task_tables(self, exclude_plan_code: str = "") -> dict[str, str]:
        active: dict[str, str] = {}
        for task in store.rows(MODULE):
            if task.get("status") != "执行中":
                continue
            if exclude_plan_code and task.get("批次编号") == exclude_plan_code:
                continue
            table = str(task.get("表名称") or "")
            if table:
                active.setdefault(table, str(task.get("任务编号") or ""))
        return active

    def _find_task(self, ref: Any) -> dict[str, Any] | None:
        text = str(ref).strip()
        for task in store.rows(MODULE):
            if text and (text == str(task.get("id")) or text == str(task.get("任务编号"))):
                return task
        return None

    def _capacity_map(self) -> dict[str, dict[str, Any]]:
        return {str(row.get("表名称") or ""): row for row in store.rows(CAPACITY_MODULE)}

    def _rank_candidates(
        self,
        tasks: Iterable[dict[str, Any]],
        *,
        exclude_plan_id: int | None = None,
        exclude_plan_code: str = "",
    ) -> list[dict[str, Any]]:
        locked_tables = self._active_plan_tables(exclude_plan_id)
        locked_tables.update(self._active_task_tables(exclude_plan_code))
        ranked: list[dict[str, Any]] = []
        for task in tasks:
            table = str(task.get("表名称") or "")
            item = {
                "任务编号": task.get("任务编号"),
                "表名称": table,
                "优化类型": task.get("优化类型"),
                "文件数": int(to_number(task.get("文件数"))),
                "容量GB": round(to_number(task.get("容量GB")), 2),
                "预算需求GB": round(to_number(task.get("预算需求GB")), 2),
                "预计收益GB": round(to_number(task.get("预计收益GB")), 2),
                "收益分": self._score(task),
                "登记顺序": int(to_number(task.get("登记顺序"))),
                "锁定表": table in locked_tables,
                "锁定原因": (
                    f"同表已有活动执行计划 {locked_tables[table]}" if table in locked_tables else ""
                ),
            }
            ranked.append(item)
        ranked.sort(
            key=lambda item: (
                -float(item["收益分"]),
                int(item["预算需求GB"]) if item["预算需求GB"] else 0,
                int(item["登记顺序"]),
                str(item["任务编号"]),
            )
        )
        for index, item in enumerate(ranked, start=1):
            item["排名"] = index
        return ranked

    def _select_within_budget(
        self, ranked: list[dict[str, Any]], budget_total: float
    ) -> tuple[list[dict[str, Any]], float]:
        """按收益分贪心选择；每一笔都先校验剩余预算，绝不允许超用。"""
        remaining = float(budget_total)
        selected_tables: set[str] = set()
        selected: list[dict[str, Any]] = []
        for item in ranked:
            reason = ""
            cost = float(item["预算需求GB"])
            if item["锁定表"]:
                reason = item["锁定原因"] or "同表已有活动任务"
            elif item["表名称"] in selected_tables:
                reason = "同表已有被选任务，只保留一个活动任务"
            elif cost <= 0:
                reason = "预算需求无效"
            elif cost > remaining + 1e-9:
                reason = f"超出剩余预算（剩余 {remaining:g}GB）"
            if reason:
                item["被选中"] = False
                item["未选中原因"] = reason
                continue
            item["被选中"] = True
            item["未选中原因"] = ""
            selected.append(item)
            selected_tables.add(str(item["表名称"]))
            remaining -= cost
        # “排名”始终是收益排序的名次，“执行排名”才是批次内的实际执行顺序，
        # 这样预算不足被跳过的任务不会把后面任务的名次挤乱。
        selected_codes = {str(item["任务编号"]) for item in selected}
        for index, item in enumerate(ranked, start=1):
            item["排名"] = index
            if str(item["任务编号"]) in selected_codes:
                item["执行排名"] = selected.index(item) + 1
            else:
                item["执行排名"] = None
        return selected, round(max(remaining, 0.0), 2)

    def _build_plan_payload(
        self,
        *,
        budget_total: float,
        selected: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        version: int = 1,
    ) -> dict[str, Any]:
        capacity = self._capacity_map()
        selected_details: list[dict[str, Any]] = []
        for item in selected:
            task = self._find_task(item["任务编号"])
            task = task or {}
            table = str(item["表名称"])
            cap = capacity.get(table, {})
            # 排序快照以容量统计为基线；统计缺失时才退回任务登记时的文件数/容量。
            before_files = int(
                to_number(cap.get("文件数"), to_number(task.get("文件数"), item.get("文件数", 0)))
            )
            before_capacity = round(
                to_number(
                    cap.get("容量GB"), to_number(task.get("容量GB"), item.get("容量GB", 0))
                ),
                2,
            )
            # 如果容量统计比任务快照新，排序快照里保留统计版本，方便识别版本冲突。
            selected_details.append(
                {
                    "任务编号": item["任务编号"],
                    "表名称": table,
                    "优化类型": item.get("优化类型"),
                    "预算需求GB": round(float(item["预算需求GB"]), 2),
                    "预计收益GB": round(float(item["预计收益GB"]), 2),
                    "收益分": item["收益分"],
                    "优化前文件数": before_files,
                    "优化前容量GB": before_capacity,
                    "预计优化后文件数": int(to_number(task.get("优化后文件数"))),
                    "预计优化后容量GB": round(to_number(task.get("优化后容量GB")), 2),
                    "容量统计版本": int(cap.get("版本", 0)),
                }
            )
        before_files = sum(int(item["优化前文件数"]) for item in selected_details)
        after_files = sum(int(item["预计优化后文件数"]) for item in selected_details)
        before_capacity = round(sum(float(item["优化前容量GB"]) for item in selected_details), 2)
        after_capacity = round(
            sum(float(item["预计优化后容量GB"]) for item in selected_details), 2
        )
        used = round(sum(float(item["预算需求GB"]) for item in selected_details), 2)
        # 防御式校验：任何情况下都不允许候选任务把预算总量撑破。
        if used > float(budget_total) + 1e-9:
            raise ValueError("预算占用超过预算总量")
        snapshot = {
            str(item["表名称"]): int(item["容量统计版本"]) for item in selected_details
        }
        return {
            "计划版本": version,
            "预算总量GB": round(float(budget_total), 2),
            "预算占用GB": used,
            "剩余预算GB": round(float(budget_total) - used, 2),
            "候选顺序": ranked,
            "被选任务": [item["任务编号"] for item in selected_details],
            "被选任务明细": selected_details,
            "表名称": "、".join(item["表名称"] for item in selected_details),
            "优化前文件数": before_files,
            "优化后文件数": after_files,
            "实际文件数": 0,
            "文件数变化": after_files - before_files,
            "优化前容量GB": before_capacity,
            "优化后容量GB": after_capacity,
            "实际容量GB": 0.0,
            "容量变化GB": round(after_capacity - before_capacity, 2),
            "预计收益GB": round(sum(float(item["预计收益GB"]) for item in selected_details), 2),
            "容量统计快照": snapshot,
            "容量统计版本": max(snapshot.values(), default=0),
            "快照版本": max(snapshot.values(), default=0),
        }

    def create_plan(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """基于现有优化任务与容量统计生成一个表优化执行计划批次。"""
        budget_raw = _first_value(
            values,
            "预算总量GB",
            "预算总量",
            "预算",
            "预算GB",
            "budget_total",
            "budgetTotal",
            "budget_gb",
            "budgetGB",
            "budget",
        )
        requested = _first_value(
            values, "task_ids", "任务ID", "任务id", "任务ids", "任务编号", "tasks"
        )
        requested_refs: list[Any] = []
        if isinstance(requested, (list, tuple)):
            requested_refs = list(requested)
        elif requested not in (None, ""):
            requested_refs = [part.strip() for part in str(requested).split(",") if part.strip()]

        table_hint_raw = _first_value(
            values, "表名称", "table", "table_name", "tableName", "tables"
        )
        if isinstance(table_hint_raw, (list, tuple)):
            table_hints = [str(item).strip() for item in table_hint_raw if str(item).strip()]
        else:
            table_hints = [
                part.strip() for part in str(table_hint_raw or "").split(",") if part.strip()
            ]

        with LOCK:
            if requested_refs:
                tasks = []
                for ref in requested_refs:
                    task = self._find_task(ref)
                    if task is None:
                        return None, f"优化任务 {ref} 不存在或已归档"
                    if task.get("status") != "待执行":
                        return None, f"优化任务 {task.get('任务编号')} 当前状态为{task.get('status')}，不能进入新执行计划"
                    tasks.append(task)
            else:
                tasks = [task for task in store.rows(MODULE) if task.get("status") == "待执行"]
            if table_hints:
                tasks = [
                    task for task in tasks if str(task.get("表名称") or "") in table_hints
                ]
                if not tasks:
                    return None, f"表 {'、'.join(table_hints)} 没有可参与排期的待执行优化任务"

            # 同表只能保留一个活动执行计划：并发提交时直接复用已有计划。
            active_plan_tables = self._active_plan_tables()
            for task in tasks:
                table = str(task.get("表名称") or "")
                if table in active_plan_tables:
                    existing = self._find_plan_by_code(active_plan_tables[table])
                    if requested_refs or table_hints:
                        return existing, f"表 {table} 已有活动执行计划 {active_plan_tables[table]}，只保留一个"
                    tasks = [item for item in tasks if str(item.get("表名称") or "") != table]

            if budget_raw in (None, ""):
                if requested_refs:
                    budget_total = sum(to_number(task.get("预算需求GB")) for task in tasks)
                else:
                    return None, "缺少必填字段：预算总量GB"
            else:
                budget_total = to_number(budget_raw, -1)
                if budget_total <= 0:
                    return None, "预算总量必须大于 0"

            ranked = self._rank_candidates(tasks)
            selected, remaining = self._select_within_budget(ranked, budget_total)
            if not selected:
                return None, (
                    f"预算总量 {budget_total:g}GB 不足以覆盖任何候选任务的预算需求，"
                    "请提高预算或调整候选任务"
                )
            version = 1
            try:
                payload = self._build_plan_payload(
                    budget_total=budget_total,
                    selected=selected,
                    ranked=ranked,
                    version=version,
                )
            except ValueError as exc:
                return None, str(exc)
            payload["剩余预算GB"] = remaining

            plans = store.rows(PLAN_MODULE)
            plan_id = max((int(plan.get("id", 0)) for plan in plans), default=0) + 1
            prefix = f"PLAN-{date.today().strftime('%Y%m%d')}"
            same_day = sum(1 for plan in plans if str(plan.get("批次编号", "")).startswith(prefix))
            code = f"{prefix}-{same_day + 1:02d}"
            while any(str(plan.get("批次编号")) == code for plan in plans):
                same_day += 1
                code = f"{prefix}-{same_day + 1:02d}"

            plan: dict[str, Any] = {
                "id": plan_id,
                "批次编号": code,
                "状态": PLAN_STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "版本冲突": False,
                "版本冲突说明": "",
                "冲突处理": "",
                "重排次数": 0,
                "发布版本": 0,
                "恢复结论": "",
                "优化效果": "",
                "创建时间": _now(),
                "更新时间": _now(),
                "发布时间": "",
                "失败环节": "",
                "失败原因": "",
            }
            plan.update(payload)
            plans.append(plan)
            self._assign_tasks(plan, selected)
            return plan, f"执行计划批次 {code} 已生成，预算占用 {payload['预算占用GB']:g}GB"

    def _find_plan_by_code(self, code: str) -> dict[str, Any] | None:
        for plan in store.rows(PLAN_MODULE):
            if str(plan.get("批次编号")) == str(code):
                return plan
        return None

    def _assign_tasks(self, plan: dict[str, Any], selected: list[dict[str, Any]]) -> None:
        code = str(plan.get("批次编号") or "")
        version = int(plan.get("计划版本") or 1)
        selected_codes = {str(item.get("任务编号")) for item in selected}
        for task in store.rows(MODULE):
            task_code = str(task.get("任务编号"))
            if task_code in selected_codes:
                task["批次编号"] = code
                task["计划版本"] = version
                task["执行计划"] = code
            elif task.get("批次编号") == code and task.get("status") == "待执行":
                task["批次编号"] = ""
                task["计划版本"] = 0
                task["执行计划"] = ""

    def _detect_conflict(self, plan: dict[str, Any]) -> bool:
        if plan.get("状态") not in ACTIVE_PLAN_STATUSES:
            return False
        snapshot = plan.get("容量统计快照") or {}
        if not snapshot:
            return False
        capacity = self._capacity_map()
        changed = []
        for table, version in snapshot.items():
            current = int(capacity.get(table, {}).get("版本", 0))
            if current != int(version):
                changed.append(f"{table} 容量统计 {version} → {current}")
        if changed:
            plan["版本冲突"] = True
            plan["版本冲突说明"] = "；".join(changed)
            plan["abnormal"] = True
            return True
        if plan.get("版本冲突"):
            # 版本已经对齐（例如按新统计重排后），冲突标记随之解除。
            plan["版本冲突"] = False
            plan["版本冲突说明"] = ""
            plan["abnormal"] = False
        return False

    def get_plan(self, plan_ref: int | str) -> dict[str, Any] | None:
        text = str(plan_ref)
        for plan in store.rows(PLAN_MODULE):
            if text == str(plan.get("id")) or text == str(plan.get("批次编号")):
                self._detect_conflict(plan)
                return plan
        return None

    def list_plans(
        self,
        *,
        keyword: str | None = None,
        table: str | None = None,
        min_benefit: float | None = None,
        sort: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        rows = list(store.rows(PLAN_MODULE))
        needle = (keyword or table or "").strip().lower()
        if needle:
            matched = []
            for plan in rows:
                haystack = [str(plan.get("批次编号") or ""), str(plan.get("表名称") or "")]
                haystack += [str(item.get("表名称") or "") for item in plan.get("被选任务明细") or []]
                haystack += [str(item.get("表名称") or "") for item in plan.get("候选顺序") or []]
                if needle in " ".join(haystack).lower():
                    matched.append(plan)
            rows = matched
        if status:
            rows = [plan for plan in rows if plan.get("状态") == status]
        for plan in rows:
            self._detect_conflict(plan)
        if min_benefit is not None:
            rows = [plan for plan in rows if to_number(plan.get("预计收益GB")) >= min_benefit]
        sort_key = (sort or "").strip().lower()
        if sort_key in ("capacity", "容量", "capacity_desc", "容量降序", "实际容量"):
            rows.sort(
                key=lambda plan: to_number(plan.get("实际容量GB") or plan.get("优化前容量GB")),
                reverse=True,
            )
        elif sort_key in ("capacity_asc", "容量升序"):
            rows.sort(key=lambda plan: to_number(plan.get("优化前容量GB")))
        elif sort_key in ("benefit", "收益", "预计收益"):
            rows.sort(key=lambda plan: to_number(plan.get("预计收益GB")), reverse=True)
        elif sort_key in ("budget", "预算"):
            rows.sort(key=lambda plan: to_number(plan.get("预算占用GB")), reverse=True)
        else:
            rows.sort(key=lambda plan: int(plan.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        paged = rows[start:start + size]
        summary = {
            "计划总数": total,
            "预算总量GB": round(sum(to_number(plan.get("预算总量GB")) for plan in rows), 2),
            "预算占用GB": round(sum(to_number(plan.get("预算占用GB")) for plan in rows), 2),
            "版本冲突计划": sum(1 for plan in rows if plan.get("版本冲突")),
        }
        return paged, total, summary

    def run_plan_action(
        self, plan_ref: int | str, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        values = values or {}
        normalized = PLAN_ACTION_ALIASES.get(str(action or "").strip())
        with LOCK:
            plan = self.get_plan(plan_ref)
            if plan is None:
                return None, f"执行计划 {plan_ref} 不存在", {
                    "调用链": "表优化管理 → 表优化执行计划 → 读取计划",
                    "失败环节": "读取执行计划",
                    "失败原因": "计划不存在或已归档",
                }
            if normalized is None:
                failure = self._fail(plan, "动作校验", f"动作「{action}」不属于表优化执行计划可执行范围")
                return plan, failure["失败原因"], failure

            if normalized in ("沿用冻结顺序", "按新统计重排"):
                if plan.get("状态") == "执行中" and normalized == "按新统计重排":
                    failure = self._fail(
                        plan, "版本冲突处理", "计划已在执行中，只能沿用冻结顺序，不能再重排候选任务"
                    )
                    return plan, failure["失败原因"], failure
                if plan.get("状态") not in ("草稿", "已确认", "执行中"):
                    failure = self._fail(
                        plan, "版本冲突处理", f"计划状态为{plan.get('状态')}，不能再调整候选顺序"
                    )
                    return plan, failure["失败原因"], failure
                if normalized == "沿用冻结顺序":
                    # 冻结的是候选顺序：任务和排位不动，只把容量统计版本对齐到当前版本，
                    # 这样后续推进不会被同一个冲突反复拦下。
                    capacity = self._capacity_map()
                    for detail in plan.get("被选任务明细") or []:
                        table = str(detail.get("表名称") or "")
                        detail["容量统计版本"] = int(capacity.get(table, {}).get("版本", 0))
                    plan["容量统计快照"] = {
                        str(detail.get("表名称")): int(detail.get("容量统计版本") or 0)
                        for detail in plan.get("被选任务明细") or []
                    }
                    plan["容量统计版本"] = max(
                        plan["容量统计快照"].values(), default=0
                    )
                    plan["版本冲突"] = False
                    plan["版本冲突说明"] = ""
                    plan["abnormal"] = False
                    plan["冲突处理"] = "沿用冻结顺序"
                    plan["更新时间"] = _now()
                    plan["失败环节"] = ""
                    plan["失败原因"] = ""
                    return plan, "已沿用冻结的候选顺序", None
                return self._resort_plan(plan)

            expected = PLAN_TRANSITIONS[normalized]
            if plan.get("状态") != expected[0]:
                failure = self._fail(
                    plan,
                    "状态推进",
                    f"计划当前状态为{plan.get('状态')}，{normalized}只允许从{expected[0]}推进到{expected[1]}",
                )
                return plan, failure["失败原因"], failure

            if normalized != "发布批次" and self._detect_conflict(plan):
                failure = self._fail(
                    plan,
                    "容量统计版本校验",
                    f"容量统计版本冲突未处理：{plan.get('版本冲突说明')}，请先沿用冻结顺序或按新统计重排",
                )
                return plan, failure["失败原因"], failure

            if normalized == "执行计划":
                failure = self._validate_before_execute(plan)
                if failure:
                    return plan, failure["失败原因"], failure

            if normalized == "完成计划":
                return self._complete_plan(plan, values)
            if normalized == "发布批次":
                return self._publish_plan(plan, values)

            plan["状态"] = expected[1]
            plan["pending"] = expected[1] not in FINAL_PLAN_STATUSES
            plan["abnormal"] = False
            plan["更新时间"] = _now()
            # 这次推进成功后，清掉上一次失败留下的排障标记，避免调用链诊断读到旧结论。
            plan["失败环节"] = ""
            plan["失败原因"] = ""
            if normalized == "执行计划":
                for detail in plan.get("被选任务明细") or []:
                    task = self._find_task(detail.get("任务编号"))
                    if task is not None:
                        task["status"] = "执行中"
                        task["任务状态"] = "执行中"
                        task["pending"] = True
            return plan, f"执行计划 {plan.get('批次编号')} 已{normalized}", None

    def _fail(
        self, plan: dict[str, Any], step: str, reason: str
    ) -> dict[str, Any]:
        plan["失败环节"] = step
        plan["失败原因"] = reason
        plan["abnormal"] = True
        plan["更新时间"] = _now()
        return {
            "调用链": "表优化管理 → 表优化执行计划 → " + step,
            "失败环节": step,
            "失败原因": reason,
            "批次编号": plan.get("批次编号"),
            "计划状态": plan.get("状态"),
        }

    def _validate_before_execute(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        selected = plan.get("被选任务明细") or []
        if not selected:
            return self._fail(plan, "被选任务校验", "执行计划没有选中任何任务，不能推进到执行中")
        used = sum(to_number(item.get("预算需求GB")) for item in selected)
        if used > to_number(plan.get("预算总量GB")) + 1e-9:
            return self._fail(
                plan,
                "预算占用校验",
                f"被选任务预算占用 {used:g}GB 超过预算总量 {plan.get('预算总量GB')}GB",
            )
        active = self._active_plan_tables(exclude_plan_id=int(plan.get("id", 0)))
        for item in selected:
            table = str(item.get("表名称") or "")
            if table in active:
                return self._fail(
                    plan, "同表活动任务校验", f"表 {table} 已被执行计划 {active[table]} 占用"
                )
        return None

    def _resort_plan(
        self, plan: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        code = str(plan.get("批次编号") or "")
        candidates = [task for task in store.rows(MODULE) if task.get("status") == "待执行"]
        if not candidates:
            failure = self._fail(plan, "候选任务读取", "没有可参与重排的待执行优化任务")
            return plan, failure["失败原因"], failure
        ranked = self._rank_candidates(
            candidates, exclude_plan_id=int(plan.get("id", 0)), exclude_plan_code=code
        )
        selected, remaining = self._select_within_budget(ranked, to_number(plan.get("预算总量GB")))
        version = int(plan.get("计划版本") or 1) + 1
        try:
            payload = self._build_plan_payload(
                budget_total=to_number(plan.get("预算总量GB")),
                selected=selected,
                ranked=ranked,
                version=version,
            )
        except ValueError as exc:
            failure = self._fail(plan, "预算占用校验", str(exc))
            return plan, str(exc), failure
        plan.update(payload)
        plan["剩余预算GB"] = remaining
        plan["版本冲突"] = False
        plan["版本冲突说明"] = ""
        plan["冲突处理"] = "按新统计重排"
        plan["重排次数"] = int(plan.get("重排次数") or 0) + 1
        plan["abnormal"] = False
        plan["更新时间"] = _now()
        plan["失败环节"] = ""
        plan["失败原因"] = ""
        self._assign_tasks(plan, selected)
        return plan, f"执行计划 {code} 已按新统计重排，计划版本 {version}", None

    def _result_map(self, values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = _first_value(values, "任务结果", "task_results", "results") or []
        result: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            raw = [raw]
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("任务编号") is not None:
                    result[str(item.get("任务编号"))] = item
        return result

    def _complete_plan(
        self, plan: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        selected = plan.get("被选任务明细") or []
        result_map = self._result_map(values)
        total_files = 0
        total_capacity = 0.0
        before_files = 0
        before_capacity = 0.0
        capacity = self._capacity_map()
        for item in selected:
            task = self._find_task(item.get("任务编号"))
            task = task or {}
            per_task = result_map.get(str(item.get("任务编号")), {})
            actual_files = int(
                to_number(
                    _first_value(per_task, "实际文件数", "文件数", "files"),
                    to_number(task.get("优化后文件数")),
                )
            )
            actual_capacity = round(
                to_number(
                    _first_value(per_task, "实际容量GB", "实际容量", "容量GB", "capacity"),
                    to_number(task.get("优化后容量GB")),
                ),
                2,
            )
            item["实际文件数"] = actual_files
            item["实际容量GB"] = actual_capacity
            item["文件数变化"] = actual_files - int(to_number(item.get("优化前文件数")))
            item["容量变化GB"] = round(
                actual_capacity - to_number(item.get("优化前容量GB")), 2
            )
            total_files += actual_files
            total_capacity += actual_capacity
            before_files += int(to_number(item.get("优化前文件数")))
            before_capacity += to_number(item.get("优化前容量GB"))
            if task:
                task["status"] = "已完成"
                task["任务状态"] = "已完成"
                task["pending"] = False
                task["异常"] = False
                task["实际文件数"] = actual_files
                task["实际容量GB"] = actual_capacity
                task["优化效果"] = (
                    f"文件数 {item.get('优化前文件数')} → {actual_files}，"
                    f"容量 {_pretty_gb(to_number(item.get('优化前容量GB')))} → {_pretty_gb(actual_capacity)}"
                )
                task["资源消耗"] = f"{_pretty_gb(to_number(item.get('预算需求GB')))}"
            self._set_capacity(str(item.get("表名称")), actual_files, actual_capacity)

        if "实际文件数" in values and values.get("实际文件数") not in (None, ""):
            total_files = int(to_number(values.get("实际文件数"), total_files))
        if "实际容量GB" in values and values.get("实际容量GB") not in (None, ""):
            total_capacity = round(to_number(values.get("实际容量GB"), total_capacity), 2)

        # 完成时容量统计由本批次自己写入，快照同步到最新版本，
        # 否则计划会把自己的回写误判成版本冲突。
        capacity = self._capacity_map()
        for item in selected:
            item["容量统计版本"] = int(
                capacity.get(str(item.get("表名称")), {}).get("版本", 0)
            )
        plan["容量统计快照"] = {
            str(item.get("表名称")): int(item.get("容量统计版本") or 0) for item in selected
        }
        plan["容量统计版本"] = max(plan["容量统计快照"].values(), default=0)
        plan["快照版本"] = plan["容量统计版本"]
        plan["版本冲突"] = False
        plan["版本冲突说明"] = ""

        plan["实际文件数"] = total_files
        plan["实际容量GB"] = round(total_capacity, 2)
        plan["文件数变化"] = total_files - before_files
        plan["容量变化GB"] = round(total_capacity - before_capacity, 2)
        plan["优化前文件数"] = before_files
        plan["优化前容量GB"] = round(before_capacity, 2)
        plan["优化效果"] = (
            f"文件数 {before_files} → {total_files}，"
            f"容量 {_pretty_gb(before_capacity)} → {_pretty_gb(total_capacity)}"
        )
        plan["恢复结论"] = str(
            _first_value(values, "恢复结论", "recovery", "conclusion")
            or "快照恢复校验通过，可回退至优化前版本"
        )
        plan["状态"] = "已完成"
        plan["pending"] = True
        plan["abnormal"] = False
        plan["更新时间"] = _now()
        plan["失败环节"] = ""
        plan["失败原因"] = ""
        # 完成即写入效果统计，切到效果详情或刷新后仍能看到完整批次结果。
        self._sync_effect(plan)
        self._sync_stats_report(plan)
        return plan, f"执行计划 {plan.get('批次编号')} 已完成，效果已回写", None

    def _publish_plan(
        self, plan: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        plan["状态"] = "已发布"
        plan["pending"] = False
        plan["abnormal"] = False
        plan["发布版本"] = int(plan.get("计划版本") or 1)
        plan["发布时间"] = _today()
        plan["更新时间"] = _now()
        self._sync_effect(plan)
        self._sync_stats_report(plan)
        for detail in plan.get("被选任务明细") or []:
            task = self._find_task(detail.get("任务编号"))
            if task is not None:
                task["status"] = "已完成"
                task["任务状态"] = "已完成"
                task["pending"] = False
                task["批次编号"] = plan.get("批次编号")
                task["计划版本"] = plan.get("计划版本")
        return plan, f"执行计划 {plan.get('批次编号')} 已发布，效果统计已同步", None

    def _set_capacity(self, table: str, files: int, capacity_gb: float) -> dict[str, Any]:
        row = next(
            (item for item in store.rows(CAPACITY_MODULE) if str(item.get("表名称")) == table),
            None,
        )
        if row is None:
            row = {"id": len(store.rows(CAPACITY_MODULE)) + 1, "表名称": table, "版本": 0}
            store.rows(CAPACITY_MODULE).append(row)
        row["文件数"] = int(files)
        row["容量GB"] = round(float(capacity_gb), 2)
        row["版本"] = int(row.get("版本") or 0) + 1
        row["更新时间"] = _now()
        return row

    def update_capacity(
        self, values: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """容量统计更新入口；版本号递增后，活动计划会进入版本冲突待处理状态。"""
        raw = _first_value(values, "items", "updates", "容量统计") or [values]
        if isinstance(raw, dict):
            raw = [raw]
        updated: list[dict[str, Any]] = []
        with LOCK:
            for item in raw:
                if not isinstance(item, dict):
                    continue
                table = str(_first_value(item, "表名称", "table", "table_name") or "").strip()
                if not table:
                    continue
                current = next(
                    (row for row in store.rows(CAPACITY_MODULE) if str(row.get("表名称")) == table),
                    None,
                )
                files = int(to_number(_first_value(item, "文件数", "files"), current.get("文件数", 0) if current else 0))
                capacity = round(
                    to_number(
                        _first_value(item, "容量GB", "容量", "capacity"),
                        current.get("容量GB", 0) if current else 0,
                    ),
                    2,
                )
                updated.append(self._set_capacity(table, files, capacity))
            conflicted = []
            for plan in store.rows(PLAN_MODULE):
                if self._detect_conflict(plan):
                    conflicted.append(plan)
        return updated, conflicted

    def list_capacity(self) -> list[dict[str, Any]]:
        return list(store.rows(CAPACITY_MODULE))

    # ------------------------------------------------------------ 效果统计
    def _sync_effect(self, plan: dict[str, Any]) -> dict[str, Any]:
        effects = store.rows(EFFECT_MODULE)
        code = str(plan.get("批次编号"))
        effect = next((item for item in effects if str(item.get("批次编号")) == code), None)
        if effect is None:
            effect = {"id": max((int(item.get("id", 0)) for item in effects), default=0) + 1}
            effects.append(effect)
        selected = plan.get("被选任务明细") or []
        effect.update(
            {
                "批次编号": code,
                "计划版本": int(plan.get("计划版本") or 1),
                "表名称": plan.get("表名称") or "",
                "被选任务": list(plan.get("被选任务") or []),
                "被选任务明细": [
                    {
                        "任务编号": item.get("任务编号"),
                        "表名称": item.get("表名称"),
                        "预算需求GB": item.get("预算需求GB"),
                        "实际文件数": item.get("实际文件数"),
                        "实际容量GB": item.get("实际容量GB"),
                    }
                    for item in selected
                ],
                "预算占用GB": plan.get("预算占用GB"),
                "优化前文件数": plan.get("优化前文件数"),
                "实际文件数": plan.get("实际文件数"),
                "文件数变化": plan.get("文件数变化"),
                "优化前容量GB": plan.get("优化前容量GB"),
                "实际容量GB": plan.get("实际容量GB"),
                "容量变化GB": plan.get("容量变化GB"),
                "实际效果": plan.get("优化效果") or "",
                "恢复结论": plan.get("恢复结论") or "",
                "状态": plan.get("状态"),
                "是否最终批次": plan.get("状态") in FINAL_PLAN_STATUSES,
                "发布版本": int(plan.get("发布版本") or 0),
                "统计版本": int(plan.get("计划版本") or 1),
                "完成时间": plan.get("更新时间"),
                "发布时间": plan.get("发布时间") or "",
            }
        )
        return effect

    def _sync_stats_report(self, plan: dict[str, Any]) -> dict[str, Any]:
        reports = store.rows("stats")
        code = str(plan.get("批次编号"))
        report = next(
            (
                item
                for item in reports
                if str(item.get("批次编号") or "") == code
            ),
            None,
        )
        if report is None:
            report_id = max((int(item.get("id", 0)) for item in reports), default=0) + 1
            report = {
                "id": report_id,
                "报告编号": f"STAT-{code.replace('PLAN-', '')}",
                "pending": False,
                "abnormal": False,
            }
            reports.append(report)
        report.update(
            {
                "表名称": plan.get("表名称") or "",
                "行总数": report.get("行总数", 0),
                "文件总数": plan.get("实际文件数"),
                "总容量": _pretty_gb(to_number(plan.get("实际容量GB"))),
                "列统计": (
                    f"被选任务 {len(plan.get('被选任务') or [])} 个，"
                    f"文件数变化 {plan.get('文件数变化')}"
                ),
                "统计时间": _today(),
                "报告状态": "已采集",
                "status": "已采集",
                "批次编号": code,
                "执行计划版本": int(plan.get("计划版本") or 1),
                "被选任务": list(plan.get("被选任务") or []),
                "被选任务明细": [
                    {
                        "任务编号": item.get("任务编号"),
                        "表名称": item.get("表名称"),
                        "预算需求GB": item.get("预算需求GB"),
                        "实际文件数": item.get("实际文件数"),
                        "实际容量GB": item.get("实际容量GB"),
                    }
                    for item in plan.get("被选任务明细") or []
                ],
                "预算占用GB": plan.get("预算占用GB"),
                "文件数变化": plan.get("文件数变化"),
                "容量变化GB": plan.get("容量变化GB"),
                "容量变化": _signed(to_number(plan.get("容量变化GB"))) + "GB",
                "实际文件数": plan.get("实际文件数"),
                "实际容量GB": plan.get("实际容量GB"),
                "恢复结论": plan.get("恢复结论") or "",
                "统计版本": int(plan.get("计划版本") or 1),
                "发布版本": int(plan.get("发布版本") or 0),
                "是否最终批次": True,
                "是否历史记录": False,
                "发布时间": plan.get("发布时间") or "",
            }
        )
        return report

    def list_effects(
        self,
        *,
        keyword: str | None = None,
        table: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [
            effect
            for effect in store.rows(EFFECT_MODULE)
            if effect.get("是否最终批次")
            and effect.get("状态") in FINAL_PLAN_STATUSES
        ]
        needle = (keyword or table or "").strip().lower()
        if needle:
            rows = [
                effect
                for effect in rows
                if needle in str(effect.get("批次编号", "")).lower()
                or needle in str(effect.get("表名称", "")).lower()
                or any(
                    needle in str(item.get("表名称", "")).lower()
                    for item in effect.get("被选任务明细") or []
                )
            ]
        rows.sort(key=lambda effect: int(effect.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def diagnose_plan(self, plan_ref: int | str) -> dict[str, Any] | None:
        plan = self.get_plan(plan_ref)
        if plan is None:
            return None
        self._detect_conflict(plan)
        steps = [
            ("读取优化任务", "成功", f"被选任务 {len(plan.get('被选任务') or [])} 个"),
            (
                "校验预算总量",
                "成功" if to_number(plan.get("预算占用GB")) <= to_number(plan.get("预算总量GB")) + 1e-9 else "失败",
                f"预算占用 {plan.get('预算占用GB')}GB / 总量 {plan.get('预算总量GB')}GB",
            ),
            (
                "校验同表活动任务",
                "失败" if plan.get("失败环节") == "同表活动任务校验" else "成功",
                "同表只保留一个活动执行计划",
            ),
            (
                "校验容量统计版本",
                "失败" if plan.get("版本冲突") or plan.get("失败环节") == "容量统计版本校验" else "成功",
                plan.get("版本冲突说明") or "排序快照与容量统计版本一致",
            ),
            (
                "推进执行计划状态",
                "失败" if plan.get("失败环节") == "状态推进" else "成功",
                f"当前状态 {plan.get('状态')}",
            ),
            (
                "同步优化效果统计",
                "成功" if plan.get("状态") in FINAL_PLAN_STATUSES else "待执行",
                f"效果批次 {plan.get('批次编号')}",
            ),
        ]
        return {
            "批次编号": plan.get("批次编号"),
            "计划版本": plan.get("计划版本"),
            "计划状态": plan.get("状态"),
            "失败环节": plan.get("失败环节"),
            "失败原因": plan.get("失败原因"),
            "调用链": [
                {"步骤": step, "结果": result, "说明": detail} for step, result, detail in steps
            ],
        }


def _first_value(values: dict[str, Any], *keys: str) -> Any:
    if not isinstance(values, dict):
        return None
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return None

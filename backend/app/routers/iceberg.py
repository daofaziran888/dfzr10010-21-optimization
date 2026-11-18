"""表优化管理接口：维护优化任务，并生成、推进、诊断表优化执行计划批次。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.iceberg import IcebergService

router = APIRouter(prefix="/api/iceberg", tags=["表优化管理"])

service = IcebergService()

LIST_FIELDS = ["任务编号", "表名称", "优化类型", "优化参数", "执行计划", "资源消耗", "优化效果", "任务状态"]
STATUSES = ["待执行", "执行中", "已完成", "已取消"]
PLAN_STATUSES = ["草稿", "已确认", "执行中", "已完成", "已发布"]


@router.get("/plans", response_model=PageResult[dict])
def list_plans(
    keyword: str | None = Query(default=None, description="按批次编号或表名检索"),
    table: str | None = Query(default=None, description="按表名检索"),
    min_benefit: float | None = Query(default=None, description="按预计收益过滤（GB）"),
    sort: str | None = Query(default=None, description="capacity=按容量降序，benefit=按收益"),
    status: str | None = Query(default=None, description="草稿、已确认、执行中、已完成、已发布"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """执行计划列表：支持收益筛选、容量排序、分页与表名检索。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total, summary = service.list_plans(
        keyword=keyword,
        table=table,
        min_benefit=min_benefit,
        sort=sort,
        status=status,
        page=page,
        size=size,
    )
    return PageResult(
        items=items, total=total, page=page, size=size, summary=summary
    )


@router.post("/plans", response_model=ActionResult)
def create_plan(payload: EntryPayload) -> ActionResult:
    """按收益排序在预算内生成执行计划批次，同表只保留一个活动计划。"""
    plan, message = service.create_plan(payload.all_values())
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)


@router.get("/plans/{plan_ref}", response_model=dict)
def get_plan(plan_ref: str) -> dict:
    """读取执行计划批次明细；plan_ref 支持 id 或批次编号。"""
    plan = service.get_plan(plan_ref)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"执行计划 {plan_ref} 不存在")
    service._detect_conflict(plan)
    return plan


@router.post("/plans/{plan_ref}/actions", response_model=ActionResult)
def run_plan_action(plan_ref: str, payload: EntryPayload) -> ActionResult:
    """推进执行计划：确认、执行、完成、发布，或处理容量统计版本冲突。"""
    action = str(payload.all_values().get("action") or "").strip()
    plan, message, failure = service.run_plan_action(plan_ref, action, payload.all_values())
    if failure is not None:
        return ActionResult(ok=False, message=message, entry=plan, data=failure)
    return ActionResult(ok=True, message=message, entry=plan)


@router.get("/plans/{plan_ref}/diagnose", response_model=dict)
def diagnose_plan(plan_ref: str) -> dict:
    """按调用链定位执行计划失败环节，供表优化管理排障使用。"""
    result = service.diagnose_plan(plan_ref)
    if result is None:
        raise HTTPException(status_code=404, detail=f"执行计划 {plan_ref} 不存在")
    return result


@router.get("/effects", response_model=PageResult[dict])
def list_effects(
    keyword: str | None = Query(default=None, description="按批次编号或表名检索"),
    table: str | None = Query(default=None, description="按表名检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """优化效果统计：只返回最终执行批次的实际效果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_effects(keyword=keyword, table=table, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/capacity", response_model=dict)
def list_capacity() -> dict[str, Any]:
    """容量统计当前版本，供排序快照与版本冲突核对。"""
    items = service.list_capacity()
    return {"module": "capacity", "total": len(items), "items": items}


@router.post("/capacity", response_model=ActionResult)
def update_capacity(payload: EntryPayload) -> ActionResult:
    """更新容量统计；活动计划的排序快照会因此标记版本冲突。"""
    updated, conflicted = service.update_capacity(payload.all_values())
    if not updated:
        return ActionResult(ok=False, message="缺少必填字段：表名称")
    message = f"已更新 {len(updated)} 张表的容量统计，版本冲突计划 {len(conflicted)} 个"
    return ActionResult(
        ok=True,
        message=message,
        entry={"updated": updated, "conflicted_plans": [plan.get("批次编号") for plan in conflicted]},
    )


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出表优化管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "iceberg", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条优化任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"优化任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条优化任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.all_values())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="优化任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条优化任务执行执行优化、取消优化、归档任务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.all_values().get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号或表名检索"),
    status: str | None = Query(default=None, description="待执行、执行中、已完成、已取消"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号、表名与状态过滤表优化管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)

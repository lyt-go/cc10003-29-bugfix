"""看板配置接口：维护看板页面，覆盖提交看板评审、发布看板、下线看板等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dashboard import dashboard_service

router = APIRouter(prefix="/api/dashboard", tags=["看板配置"])

LIST_FIELDS = ["看板编号", "看板名称", "包含指标", "布局模板", "刷新间隔", "可见范围", "所属业务线", "看板负责人"]
STATUSES = ["草稿", "看板评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按看板编号检索"),
    status: str | None = Query(default=None, description="草稿、看板评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按看板编号与状态过滤看板配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = dashboard_service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条看板页面明细；不存在时给出可读的错误说明。

    详情只做只读拼装（路径状态、默认层、版本、失效原因），不会覆盖存储字段。
    """
    entry = dashboard_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"看板页面 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/publish-check", response_model=dict)
def publish_check(entry_id: int) -> dict:
    """发布前门禁检查：返回 path_label、invalid_reason、max_layer 与最新可用版本。"""
    check = dashboard_service.publish_gate(entry_id)
    if check is None:
        raise HTTPException(status_code=404, detail=f"看板页面 {entry_id} 不存在或已归档")
    return check


@router.get("/{entry_id}/drill", response_model=dict)
def drill_detail(entry_id: int) -> dict:
    """从看板详情追路径版本：返回历史层级快照与当前下钻可用性。"""
    snapshot = dashboard_service.drill_snapshot(entry_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"看板页面 {entry_id} 不存在或已归档")
    return snapshot


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条看板页面，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = dashboard_service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="看板页面已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条看板页面执行提交看板评审、发布看板、下线看板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message, check = dashboard_service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message, detail=check)
    return ActionResult(ok=True, message=message, entry=entry, detail=check)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出看板配置清单：返回当前过滤条件下的全量数据。"""
    items, total = dashboard_service.list_entries(page=1, size=10000)
    return {"module": "dashboard", "total": total, "items": items}

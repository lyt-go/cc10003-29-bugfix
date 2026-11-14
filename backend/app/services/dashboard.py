"""看板配置业务规则：看板生命周期与下钻路径发布门禁。

发布看板前必须同时满足：
1. 关联下钻路径存在且状态为「已启用」（关闭/评审中/缺失一律拒绝）；
2. 默认展开层位于路径深度内（1 <= 默认展开层 <= 路径深度）；
3. 路径每层度量完整，且看板三层配置与路径层级对得上。

门禁失败时看板停留在「看板评审中」，原布局等字段原样保留；任何失败都不会
让看板或路径进入可用状态。发布成功会固化路径版本与层级快照，路径之后关闭
也不影响历史看板按快照读取，但新下钻会被阻止并展示失效原因。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.datadrill import (
    CLOSED_REASON,
    datadrill_service,
    default_layer,
    full_version_label,
    parse_int,
    path_depth,
    path_dimensions,
    path_measures,
    split_layers,
)
from app.store import store

MODULE = "dashboard"
REQUIRED_FIELDS = ["看板编号", "看板名称", "包含指标"]
STATUS_ORDER = ["草稿", "看板评审中", "已发布", "已下线"]
# 同样只允许逐档推进，禁止跳级、倒序与重复发布。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交看板评审": ("草稿", "看板评审中"),
    "发布看板": ("看板评审中", "已发布"),
    "下线看板": ("已发布", "已下线"),
}

# 门禁失败原因码：关闭路径优先级最高，其次路径缺失，再到默认层越界与度量缺失。
REASON_CLOSED_PATH = "CLOSED_PATH"
REASON_PATH_NOT_FOUND = "PATH_NOT_FOUND"
REASON_PATH_NOT_ENABLED = "PATH_NOT_ENABLED"
REASON_DEFAULT_LAYER_OUT_OF_RANGE = "DEFAULT_LAYER_OUT_OF_RANGE"
REASON_MEASURE_INCOMPLETE = "MEASURE_INCOMPLETE"
PATH_FIELD = "关联下钻路径"
DEFAULT_LAYER_FIELD = "默认展开层"


class DashboardService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---------- 读取入口 ----------

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
            rows = [row for row in rows if keyword in str(row.get("看板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._enrich_drill(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """看板详情入口：只做只读拼装，绝不回写覆盖存储里的关联字段。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._enrich_drill(entry)

    def drill_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        """供前端/调用方按看板读取历史三层配置与当前可下钻状态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        enriched = self._enrich_drill(entry)
        return {
            "看板编号": entry.get("看板编号"),
            "status": entry.get("status"),
            "路径版本": entry.get("路径版本"),
            "默认展开层": entry.get(DEFAULT_LAYER_FIELD),
            "层级快照": entry.get("层级快照") or [],
            "可下钻": enriched.get("可下钻", False),
            "失效原因": enriched.get("invalid_reason"),
        }

    def _enrich_drill(self, entry: dict[str, Any]) -> dict[str, Any]:
        """在不修改存储记录的前提下，拼出与路径列表一致的默认层/版本视图。"""
        view = dict(entry)
        path_ref = entry.get(PATH_FIELD) or entry.get("关联看板")
        path = datadrill_service.find_path(path_ref) if path_ref else None
        if path is None:
            view["path_label"] = "none"
            view["路径状态"] = None
            view["可下钻"] = False
            if path_ref:
                view["invalid_reason"] = REASON_PATH_NOT_FOUND
            return view
        view["path_label"] = full_version_label(path)
        view["路径状态"] = path.get("status")
        view["路径深度值"] = path_depth(path)
        view["默认展开层值"] = parse_int(entry.get(DEFAULT_LAYER_FIELD))
        view["可下钻"] = path.get("status") == "已启用" and entry.get("status") == "已发布"
        if path.get("status") == "已关闭":
            view["invalid_reason"] = CLOSED_REASON
            view["失效原因"] = path.get("失效原因") or CLOSED_REASON
            view["最新可用版本"] = datadrill_service.latest_usable_version(path)
        elif path.get("status") != "已启用":
            view["invalid_reason"] = REASON_PATH_NOT_ENABLED
        return view

    # ---------- 写入入口 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in [
                "看板编号", "看板名称", "包含指标", "布局模板", "刷新间隔", "可见范围",
                "所属业务线", "看板负责人", PATH_FIELD, DEFAULT_LAYER_FIELD,
            ]:
                if field in values:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["路径版本"] = ""
            entry["层级快照"] = []
            entry["发布检查"] = None
            rows.append(entry)
        return entry, []

    def run_action(
        self, entry_id: int, action: str
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于看板配置可执行范围", None
        expected, target = ACTION_RULES[action]
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"看板页面 {entry_id} 不存在或已归档", None
            current = str(entry.get("status") or "")
            if current == target:
                return None, f"看板当前已是「{target}」，{action}属于重复操作，已拒绝", None
            if current != expected:
                return (
                    None,
                    f"看板状态为「{current}」，只能从「{expected}」执行「{action}」，"
                    "生命周期不允许跳级或倒序切换",
                    None,
                )
            if action != "发布看板":
                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = action == "下线看板"
                return entry, f"看板页面已{action}", None

            # 发布门禁：先完整校验、再落库，任何一步失败都整体回滚。
            check = self._publish_check(entry)
            if not check["passed"]:
                entry["发布检查"] = self._public_check(check)
                return None, check["message"], self._public_check(check)
            path = check["path"]
            snapshot = self._commit_publish(entry, path)
            public_check = self._public_check(check)
            public_check["层级快照"] = snapshot
            entry["发布检查"] = public_check
            return entry, "看板页面已发布看板", public_check

    # ---------- 发布门禁 ----------

    def publish_gate(self, entry_id: int) -> dict[str, Any] | None:
        """发布前检查入口：只读返回门禁结论，不改变看板与路径状态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        check = self._publish_check(entry)
        return self._public_check(check)

    @staticmethod
    def _public_check(check: dict[str, Any]) -> dict[str, Any]:
        """剥离仅供服务内部使用的路径对象，避免门禁响应泄漏整行存储。"""
        return {key: value for key, value in check.items() if key != "path"}

    def _publish_check(self, entry: dict[str, Any]) -> dict[str, Any]:
        path_ref = entry.get(PATH_FIELD) or entry.get("关联看板")
        problems: list[dict[str, str]] = []
        primary: str | None = None
        path = datadrill_service.find_path(path_ref) if path_ref else None

        # 1) 路径状态门禁：关闭路径给出 CLOSED_PATH，并回传最新可用版本。
        latest_usable: str | None = None
        if path is None:
            primary = primary or REASON_PATH_NOT_FOUND
            problems.append({
                "reason": REASON_PATH_NOT_FOUND,
                "message": f"关联下钻路径「{path_ref or ''}」不存在或已归档，path_label=none",
            })
        else:
            latest_usable = datadrill_service.latest_usable_version(path)
            path_status = str(path.get("status") or "")
            if path_status == "已关闭":
                primary = primary or REASON_CLOSED_PATH
                problems.append({
                    "reason": REASON_CLOSED_PATH,
                    "message": f"关联路径已关闭（{CLOSED_REASON}），新下钻被阻止",
                })
            elif path_status != "已启用":
                primary = primary or REASON_PATH_NOT_ENABLED
                problems.append({
                    "reason": REASON_PATH_NOT_ENABLED,
                    "message": f"关联路径状态为「{path_status}」，发布前必须先完成启用",
                })

        depth = path_depth(path) if path is not None else None
        measures = path_measures(path) if path is not None else []
        dimensions = path_dimensions(path) if path is not None else []
        layer = parse_int(entry.get(DEFAULT_LAYER_FIELD))

        # 2) 默认展开层必须落在路径深度内。
        if depth is None or depth <= 0:
            primary = primary or REASON_DEFAULT_LAYER_OUT_OF_RANGE
            problems.append({
                "reason": REASON_DEFAULT_LAYER_OUT_OF_RANGE,
                "message": "路径深度缺失或不是正整数，无法确认默认展开层范围",
            })
        elif layer is None or layer < 1 or layer > depth:
            primary = primary or REASON_DEFAULT_LAYER_OUT_OF_RANGE
            problems.append({
                "reason": REASON_DEFAULT_LAYER_OUT_OF_RANGE,
                "message": f"默认展开层={entry.get(DEFAULT_LAYER_FIELD)} 越界，最大值只能是 {depth}",
            })

        # 3) 每层度量完整：度量数量与路径层级（深度/维度名）一致且无空项。
        expected_layers = depth or len(dimensions)
        missing_measure_layers = [
            index for index in range(1, expected_layers + 1)
            if index > len(measures) or not str(measures[index - 1]).strip()
        ]
        if expected_layers <= 0:
            primary = primary or REASON_MEASURE_INCOMPLETE
            problems.append({
                "reason": REASON_MEASURE_INCOMPLETE,
                "message": "路径层级为空，无法校验每层度量",
            })
        elif missing_measure_layers:
            primary = primary or REASON_MEASURE_INCOMPLETE
            problems.append({
                "reason": REASON_MEASURE_INCOMPLETE,
                "message": f"第 {'、'.join(map(str, missing_measure_layers))} 层缺少度量，每层度量必须完整",
            })

        passed = not problems
        # 只有已启用路径才算可用标签；关闭/评审中的绑定路径对外呈现 path_label=none，
        # 具体绑定与替代版本通过失效原因和「最新可用版本」追溯。
        usable_label = full_version_label(path) if path is not None and path.get("status") == "已启用" else "none"
        check: dict[str, Any] = {
            "passed": passed,
            "path_label": usable_label,
            "invalid_reason": None if passed else primary,
            "max_layer": depth,
            "默认展开层": layer,
            "最新可用版本": latest_usable,
            "problems": problems,
        }
        if passed:
            check["message"] = "发布门禁通过：路径已启用、默认展开层在深度内、每层度量完整"
        else:
            check["message"] = "发布门禁未通过：" + "；".join(item["message"] for item in problems)
        if path is not None:
            check["path"] = path
        return check

    def _commit_publish(self, entry: dict[str, Any], path: dict[str, Any]) -> list[dict[str, Any]]:
        """门禁通过后的唯一写入口：固化共享的默认展开层与路径版本、保存层级快照。"""
        depth = path_depth(path)
        measures = path_measures(path)
        dimensions = path_dimensions(path)
        snapshot: list[dict[str, Any]] = []
        for index in range(depth or 0):
            snapshot.append({
                "层级": index + 1,
                "维度名": dimensions[index] if index < len(dimensions) else "",
                "度量": measures[index] if index < len(measures) else "",
            })
        # 先备份再写入，异常时恢复，保证看板与路径都不会停在半成品状态。
        backup = {
            "status": entry.get("status"),
            "pending": entry.get("pending"),
            "abnormal": entry.get("abnormal"),
            "路径版本": entry.get("路径版本"),
            "层级快照": entry.get("层级快照"),
            DEFAULT_LAYER_FIELD: entry.get(DEFAULT_LAYER_FIELD),
            PATH_FIELD: entry.get(PATH_FIELD),
        }
        try:
            entry["status"] = "已发布"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["路径版本"] = full_version_label(path)
            entry["路径版本号"] = path.get("版本号")
            entry["层级快照"] = snapshot
            entry[DEFAULT_LAYER_FIELD] = parse_int(entry.get(DEFAULT_LAYER_FIELD))
            entry[PATH_FIELD] = path.get("id")
            # 路径侧只登记引用看板，不覆盖路径自身的默认展开层与版本。
            linked = split_layers(path.get("关联看板"))
            code = str(entry.get("看板编号") or "")
            if code and code not in linked:
                linked.append(code)
                path["关联看板"] = ",".join(linked)
        except Exception:
            entry.update(backup)
            raise
        return snapshot


dashboard_service = DashboardService()

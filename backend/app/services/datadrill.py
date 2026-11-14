"""多维下钻业务规则：路径生命周期状态机、版本谱系、关闭保留与下钻门禁。

生命周期只能按 草稿 -> 路径评审中 -> 已启用 -> 已关闭 单向推进：
禁止跳级、倒序、关闭后恢复或重复归档。启用时在同一系列内递增版本号，
关闭后路径数据与历史看板层级继续保留，但新下钻会被阻止并给出失效原因。
"""
from __future__ import annotations

import threading
from typing import Any

from app.store import store

MODULE = "datadrill"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级"]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已关闭"]
# 每个动作只允许从唯一前置状态触发，天然杜绝跳级与倒序。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交路径评审": ("草稿", "路径评审中"),
    "启用路径": ("路径评审中", "已启用"),
    "关闭路径": ("已启用", "已关闭"),
}
CLOSED_REASON = "CLOSED_PATH"
# 同一系列里可作为替代的最新可用版本查找范围。
_SERIES_FIELD = "路径系列"
_VERSION_FIELD = "版本号"


def parse_int(value: Any) -> int | None:
    """宽松解析整数；空串、占位文本、非数字内容一律返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def split_layers(value: Any) -> list[str]:
    """把逗号/顿号分隔的层级字段拆成列表，去掉空白项。"""
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    for separator in ("，", "、", ";", "；", "|"):
        text = text.replace(separator, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def path_depth(entry: dict[str, Any]) -> int | None:
    return parse_int(entry.get("路径深度"))


def default_layer(entry: dict[str, Any]) -> int | None:
    return parse_int(entry.get("默认展开层"))


def path_measures(entry: dict[str, Any]) -> list[str]:
    return split_layers(entry.get("每层度量"))


def path_dimensions(entry: dict[str, Any]) -> list[str]:
    return split_layers(entry.get("每层维度名"))


def full_version_label(entry: dict[str, Any]) -> str:
    """完整版本标签，例如 DATA-REGION_CHANNEL@v2。"""
    label = entry.get("完整版本标签")
    if label:
        return str(label)
    code = str(entry.get("路径编号") or "")
    version = entry.get("路径版本") or ""
    return f"{code}@{version}" if version else code


class DatadrillService:
    """下钻路径领域服务：所有写操作都在同一把锁内完成状态复核。"""

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
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 已关闭路径重复归档时会出现同编号同版本的重复行，列表按 (编号, 版本) 去重，
        # 保留首次出现的记录，避免“已关闭路径仍重复显示”。
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row.get("路径编号") or ""), str(row.get("路径版本") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(self._with_readonly_flags(row))
        total = len(deduped)
        start = max(page - 1, 0) * size
        return deduped[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        detail = self._with_readonly_flags(entry)
        # 从看板详情追路径版本：附带同一系列的全部版本，便于核对最新可用版本。
        detail["同系列版本"] = self.series_versions(entry)
        return detail

    def find_path(self, entry_id: Any) -> dict[str, Any] | None:
        """供看板服务按关联字段解析路径，关联值可能是 id 或路径编号。"""
        numeric_id = parse_int(entry_id)
        if numeric_id is not None:
            entry = store.find(MODULE, numeric_id)
            if entry is not None:
                return entry
        code = str(entry_id or "").strip()
        if code:
            for row in store.rows(MODULE):
                if str(row.get("路径编号") or "") == code:
                    return row
        return None

    def series_versions(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        series = entry.get(_SERIES_FIELD) or entry.get("路径编号")
        versions = [
            {
                "路径编号": row.get("路径编号"),
                "路径版本": row.get("路径版本"),
                "完整版本标签": full_version_label(row),
                "status": row.get("status"),
            }
            for row in store.rows(MODULE)
            if (row.get(_SERIES_FIELD) or row.get("路径编号")) == series
        ]
        versions.sort(key=lambda item: parse_int(str(item["路径版本"]).lstrip("v")) or 0)
        return versions

    def latest_usable_version(self, entry: dict[str, Any]) -> str | None:
        """关闭/评审路径的替代版本：同系列中版本号最大的已启用路径。"""
        series = entry.get(_SERIES_FIELD) or entry.get("路径编号")
        candidates = [
            row for row in store.rows(MODULE)
            if (row.get(_SERIES_FIELD) or row.get("路径编号")) == series
            and row.get("status") == "已启用"
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda row: parse_int(row.get(_VERSION_FIELD)) or 0)
        return full_version_label(latest)

    def _with_readonly_flags(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表/详情的只读派生标记，不落库、不覆盖任何已有字段。"""
        view = dict(entry)
        depth = path_depth(entry)
        stored_layer = default_layer(entry)
        view["路径深度值"] = depth
        view["默认展开层值"] = stored_layer
        view["可下钻"] = entry.get("status") == "已启用"
        if entry.get("status") == "已关闭":
            view["失效原因"] = entry.get("失效原因") or CLOSED_REASON
            view["最新可用版本"] = self.latest_usable_version(entry)
            # 旧默认层可能已越界，只给出修正建议值，绝不改写存储字段。
            if depth is not None and stored_layer is not None and stored_layer > depth:
                view["有效默认展开层"] = depth
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
                "路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量",
                "路径深度", "默认展开层", "关联看板", "路径系列",
            ]:
                if field in values:
                    entry[field] = values.get(field)
            entry.setdefault("路径系列", values.get("路径编号"))
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["版本号"] = None
            entry["路径版本"] = ""
            entry["完整版本标签"] = ""
            entry["失效原因"] = ""
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """执行生命周期动作，返回 (记录, 说明, 附加信息)。

        状态机在锁内二次复核：并发启用同一路径时只有一个请求能完成状态推进，
        其余请求拿到失败结果且路径版本不会被重复递增。
        """
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围", None
        expected, target = ACTION_RULES[action]
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"下钻路径 {entry_id} 不存在或已归档", None
            current = str(entry.get("status") or "")
            if current == target:
                return None, f"路径当前已是「{target}」，{action}属于重复操作，已拒绝", None
            if current != expected:
                hint = self._next_action_hint(current)
                return (
                    None,
                    f"路径状态为「{current}」，仅允许{hint}，不能直接执行「{action}」"
                    f"（生命周期只能按 草稿→路径评审中→已启用→已关闭 推进）",
                    None,
                )
            if action == "启用路径":
                self._assign_version(entry)
            if action == "关闭路径":
                entry["失效原因"] = CLOSED_REASON
                entry["abnormal"] = True
            else:
                entry["abnormal"] = False
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            detail = {
                "路径编号": entry.get("路径编号"),
                "路径版本": entry.get("路径版本"),
                "完整版本标签": full_version_label(entry),
                "status": target,
            }
            return entry, f"下钻路径已{action}", detail

    def _assign_version(self, entry: dict[str, Any]) -> None:
        """在同一系列内单调递增版本号，并发调用由外层锁串行化。"""
        series = entry.get(_SERIES_FIELD) or entry.get("路径编号")
        used = [
            parse_int(row.get(_VERSION_FIELD)) or 0
            for row in store.rows(MODULE)
            if (row.get(_SERIES_FIELD) or row.get("路径编号")) == series
        ]
        next_version = max(used, default=0) + 1
        entry[_VERSION_FIELD] = next_version
        entry["路径版本"] = f"v{next_version}"
        entry["完整版本标签"] = f"{entry.get('路径编号')}@v{next_version}"

    @staticmethod
    def _next_action_hint(status: str) -> str:
        hints = {
            "草稿": "提交路径评审",
            "路径评审中": "启用路径",
            "已启用": "关闭路径",
            "已关闭": "无后续动作（已关闭路径禁止恢复或重复归档）",
        }
        return hints.get(status, "按生命周期顺序执行动作")


# 路由与看板服务共用同一个单例，保证锁与版本序列全局一致。
datadrill_service = DatadrillService()

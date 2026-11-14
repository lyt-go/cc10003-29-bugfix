"""下钻路径生命周期验收：状态机门禁、版本谱系、关闭保留与并发启用。"""
from __future__ import annotations

import concurrent.futures

from app.services.datadrill import CLOSED_REASON, datadrill_service
from app.store import store


def _action(client, entry_id: int, action: str):
    return client.post(
        f"/api/datadrill/{entry_id}/actions",
        json={"values": {"action": action}},
    )


def test_lifecycle_only_moves_forward(client):
    """草稿路径禁止跳级启用，必须先评审再启用，关闭后不能恢复。"""
    # id=4 是草稿路径
    jump = _action(client, 4, "启用路径")
    assert jump.status_code == 200
    assert jump.json()["ok"] is False
    assert "草稿" in jump.json()["message"]

    review = _action(client, 4, "提交路径评审")
    assert review.json()["ok"] is True
    # 倒序：评审中不能再提交评审
    duplicate = _action(client, 4, "提交路径评审")
    assert duplicate.json()["ok"] is False
    assert "重复操作" in duplicate.json()["message"]

    enable = _action(client, 4, "启用路径")
    assert enable.json()["ok"] is True
    assert enable.json()["detail"]["完整版本标签"] == "DATA-DRAFT-PATH@v1"

    close = _action(client, 4, "关闭路径")
    assert close.json()["ok"] is True
    # 关闭后恢复/重复归档全部拒绝
    for action in ("启用路径", "提交路径评审", "关闭路径"):
        refused = _action(client, 4, action)
        assert refused.json()["ok"] is False, action
        assert "已关闭" in refused.json()["message"]


def test_enable_assigns_monotonic_versions_in_series(client):
    """同一系列（ORDER-REGION）内启用新版本，版本号在 v2 基础上递增到 v3。"""
    # 先准备一条同系列评审中路径：把 id=4 改成同系列并推进到评审中
    path4 = store.find("datadrill", 4)
    path4["路径系列"] = "ORDER-REGION"
    path4["路径编号"] = "DATA-ORDER_LEVEL"
    assert _action(client, 4, "提交路径评审").json()["ok"]
    result = _action(client, 4, "启用路径")
    assert result.json()["ok"] is True
    assert result.json()["entry"]["完整版本标签"] == "DATA-ORDER_LEVEL@v3"
    assert result.json()["entry"]["版本号"] == 3


def test_concurrent_enable_only_one_succeeds(client):
    """并发启用同一条评审路径：只接受一个完整结果，版本只递增一次。"""
    # id=3 处于路径评审中
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: _action(client, 3, "启用路径"), range(8)))
    successes = [item for item in responses if item.json()["ok"]]
    failures = [item for item in responses if not item.json()["ok"]]
    assert len(successes) == 1
    assert len(failures) == 7
    entry = store.find("datadrill", 3)
    assert entry["status"] == "已启用"
    assert entry["版本号"] == 1
    assert all("重复操作" in item.json()["message"] for item in failures)


def test_closed_path_keeps_history_and_blocks_new_drill(client):
    """关闭路径保留数据与原层级，列表只显示一次，详情带 CLOSED_PATH 与最新可用版本。"""
    listing = client.get("/api/datadrill?status=已关闭").json()
    assert listing["total"] == 1
    closed = listing["items"][0]
    assert closed["路径编号"] == "DATA-ORDER_LEVEL"
    assert closed["可下钻"] is False
    assert closed["失效原因"] == CLOSED_REASON
    assert closed["最新可用版本"] == "DATA-REGION_CHANNEL@v2"
    # 原层级与默认层原样保留，不被覆盖
    assert closed["路径深度"] == 3
    assert closed["默认展开层"] == 2

    detail = client.get("/api/datadrill/1").json()
    assert detail["失效原因"] == CLOSED_REASON
    labels = [item["完整版本标签"] for item in detail["同系列版本"]]
    assert "DATA-ORDER_LEVEL@v1" in labels
    assert "DATA-REGION_CHANNEL@v2" in labels


def test_closed_path_is_not_duplicated_in_list(client):
    """模拟重复归档产生同编号同版本重复行时，列表按 (编号, 版本) 去重。"""
    rows = store.rows("datadrill")
    duplicate = dict(rows[0])
    rows.append(duplicate)
    listing = client.get("/api/datadrill?status=已关闭").json()
    assert listing["total"] == 1

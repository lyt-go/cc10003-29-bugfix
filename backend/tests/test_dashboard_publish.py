"""看板发布门禁验收：关闭路径、默认层越界、度量缺失、回滚与版本追溯。"""
from __future__ import annotations

import concurrent.futures


def _action(client, entry_id: int, action: str):
    return client.post(
        f"/api/dashboard/{entry_id}/actions",
        json={"values": {"action": action}},
    )


def test_closed_path_blocks_publish_with_required_payload(client):
    """异常样例：DASH-FINANCE 引用已关闭路径且默认层 4 越界，发布被门禁拦下。"""
    check = client.get("/api/dashboard/2/publish-check").json()
    assert check["passed"] is False
    assert check["path_label"] == "none"
    assert check["invalid_reason"] == "CLOSED_PATH"
    assert check["max_layer"] == 3
    assert check["最新可用版本"] == "DATA-REGION_CHANNEL@v2"
    reasons = {item["reason"] for item in check["problems"]}
    assert {"CLOSED_PATH", "DEFAULT_LAYER_OUT_OF_RANGE"} <= reasons

    result = _action(client, 2, "发布看板")
    assert result.json()["ok"] is False
    # 看板停在看板评审中，原布局与三层配置保留
    detail = client.get("/api/dashboard/2").json()
    assert detail["status"] == "看板评审中"
    assert detail["布局模板"] == "grid-2x2"
    assert len(detail["层级快照"]) == 3
    assert detail["默认展开层"] == 4
    # 详情给出失效原因、替代版本与“最大值只能是 3”的可读提示
    assert detail["invalid_reason"] == "CLOSED_PATH"
    assert detail["最新可用版本"] == "DATA-REGION_CHANNEL@v2"
    assert "最大值只能是 3" in result.json()["message"]
    # 关闭路径不允许新下钻
    drill = client.get("/api/dashboard/2/drill").json()
    assert drill["可下钻"] is False
    assert drill["失效原因"] == "CLOSED_PATH"
    # 旧看板的三层配置继续可按历史读取
    assert [layer["层级"] for layer in drill["层级快照"]] == [1, 2, 3]


def test_default_layer_out_of_range_keeps_dashboard_in_review(client):
    """把看板改挂到已启用路径，但默认层仍越界：只报越界，看板不发布。"""
    # id=4 看板挂在已启用路径 DATA-REGION_CHANNEL（深度 3），先制造越界
    dashboard_rows = client.get("/api/dashboard").json()["items"]
    dash4 = next(item for item in dashboard_rows if item["看板编号"] == "DASH-REGION")
    assert dash4["默认展开层"] == 2
    # 直接通过存储模拟表单把默认层改成 5
    from app.store import store
    entry = store.find("dashboard", 4)
    entry["默认展开层"] = 5

    check = client.get("/api/dashboard/4/publish-check").json()
    assert check["passed"] is False
    assert check["invalid_reason"] == "DEFAULT_LAYER_OUT_OF_RANGE"
    assert check["max_layer"] == 3
    result = _action(client, 4, "发布看板")
    assert result.json()["ok"] is False
    assert "最大值只能是 3" in result.json()["message"]
    assert client.get("/api/dashboard/4").json()["status"] == "看板评审中"


def test_missing_measure_blocks_publish(client):
    """每层度量缺失时门禁返回 MEASURE_INCOMPLETE，且看板与路径都不会变可用。"""
    from app.store import store
    # 草稿路径 id=4 的第三层度量为空，推进到启用后再挂看板
    path = store.find("datadrill", 4)
    path["status"] = "路径评审中"
    enabled = client.post(
        "/api/datadrill/4/actions", json={"values": {"action": "启用路径"}}
    )
    assert enabled.json()["ok"] is True

    entry = store.find("dashboard", 4)
    entry["关联下钻路径"] = "DATA-DRAFT-PATH"
    entry["默认展开层"] = 1

    check = client.get("/api/dashboard/4/publish-check").json()
    assert check["passed"] is False
    assert check["invalid_reason"] == "MEASURE_INCOMPLETE"
    assert "第 3 层缺少度量" in check["message"]
    result = _action(client, 4, "发布看板")
    assert result.json()["ok"] is False
    # 看板未发布，路径状态不被回改
    assert client.get("/api/dashboard/4").json()["status"] == "看板评审中"
    assert store.find("datadrill", 4)["status"] == "已启用"


def test_successful_publish_snapshots_version_and_layers(client):
    """门禁全过：发布固化路径版本与三层快照，看板详情可追到路径版本。"""
    result = _action(client, 4, "发布看板")
    assert result.json()["ok"] is True, result.text
    detail = result.json()["detail"]
    assert detail["path_label"] == "DATA-REGION_CHANNEL@v2"
    assert detail["max_layer"] == 3

    detail = client.get("/api/dashboard/4").json()
    assert detail["status"] == "已发布"
    assert detail["路径版本"] == "DATA-REGION_CHANNEL@v2"
    assert detail["path_label"] == "DATA-REGION_CHANNEL@v2"
    assert [layer["层级"] for layer in detail["层级快照"]] == [1, 2, 3]
    # 路径与看板共享默认展开层
    assert detail["默认展开层"] == 2

    drill = client.get("/api/dashboard/4/drill").json()
    assert drill["可下钻"] is True
    assert drill["路径版本"] == "DATA-REGION_CHANNEL@v2"
    assert len(drill["层级快照"]) == 3


def test_duplicate_publish_is_rejected_and_idempotent(client):
    """重复发布同一看板只接受一个完整结果，第二次直接被状态机拒绝。"""
    assert _action(client, 4, "发布看板").json()["ok"] is True
    again = _action(client, 4, "发布看板")
    assert again.json()["ok"] is False
    assert "重复操作" in again.json()["message"]
    assert client.get("/api/dashboard/4").json()["status"] == "已发布"


def test_concurrent_publish_only_one_succeeds(client):
    """并发发布同一评审看板：只有一个请求完成发布，其余全部失败且无脏状态。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: _action(client, 4, "发布看板"), range(8)))
    successes = [item for item in responses if item.json()["ok"]]
    assert len(successes) == 1
    assert all(not item.json()["ok"] for item in responses if item not in successes)
    detail = client.get("/api/dashboard/4").json()
    assert detail["status"] == "已发布"
    assert detail["路径版本"] == "DATA-REGION_CHANNEL@v2"


def test_failed_publish_rolls_back_and_path_stays_usable(client):
    """门禁失败整体回滚：看板留在评审中，已启用路径版本与状态不受影响。"""
    from app.store import store
    before = dict(store.find("datadrill", 2))
    # id=2 看板必失败（关闭路径 + 越界）
    result = _action(client, 2, "发布看板")
    assert result.json()["ok"] is False
    after = store.find("datadrill", 2)
    assert after["status"] == before["status"] == "已启用"
    assert after["完整版本标签"] == before["完整版本标签"] == "DATA-REGION_CHANNEL@v2"
    dash = store.find("dashboard", 2)
    assert dash["status"] == "看板评审中"
    assert dash["路径版本"] == ""


def test_review_in_review_path_cannot_be_published(client):
    """引用评审中路径同样不允许发布，invalid_reason=PATH_NOT_ENABLED。"""
    from app.store import store
    entry = store.find("dashboard", 4)
    entry["关联下钻路径"] = "DATA-REVIEW_PATH"
    check = client.get("/api/dashboard/4/publish-check").json()
    assert check["passed"] is False
    assert check["invalid_reason"] == "PATH_NOT_ENABLED"
    assert check["path_label"] == "none"
    assert _action(client, 4, "发布看板").json()["ok"] is False


def test_dashboard_lifecycle_rejects_skipping(client):
    """看板草稿不能直接发布，必须先提交评审。"""
    jump = _action(client, 1, "发布看板")
    assert jump.json()["ok"] is False
    assert "草稿" in jump.json()["message"]
    review = _action(client, 1, "提交看板评审")
    assert review.json()["ok"] is True
    # 已下线看板不能再发布
    assert _action(client, 3, "下线看板").json()["ok"] is True
    republish = _action(client, 3, "发布看板")
    assert republish.json()["ok"] is False
    assert "已下线" in republish.json()["message"]


def test_detail_read_does_not_overwrite_link_fields(client):
    """反复读取看板详情不会覆盖关联路径与默认展开层（修复“重进详情字段被覆盖”）。"""
    from app.store import store
    entry_before = dict(store.find("dashboard", 4))
    for _ in range(3):
        client.get("/api/dashboard/4")
    entry_after = store.find("dashboard", 4)
    assert entry_after["关联下钻路径"] == entry_before["关联下钻路径"]
    assert entry_after["默认展开层"] == entry_before["默认展开层"]
    assert entry_after["路径版本"] == entry_before["路径版本"]

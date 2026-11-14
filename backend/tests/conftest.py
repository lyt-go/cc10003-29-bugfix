"""测试夹具：每个用例前把内存仓库重置回种子数据，保证用例相互独立。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    return TestClient(app)


@pytest.fixture()
def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    return store

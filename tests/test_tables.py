"""表格级（跨设备）查询接口测试。

覆盖 /api/tables/* ：领用记录、APP 绑定、设备凭证、设备状态、事件日志、
员工清单与概览聚合，并验证租户隔离与非管理员拦截。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def register(client: TestClient, sn: str, **extra):
    payload = {"sn": sn, "model": "VH-1", "manufacturer": "嘉环声枢"}
    payload.update(extra)
    return client.post("/api/devices", json=payload)


def _seed(client: TestClient, auth) -> tuple[int, int]:
    """造两台设备：一台领用并绑定，一台闲置。返回 (设备1 id, 设备2 id)。

    绑定必须由领用员工本人发起（README §7.6），因此中途切换一次登录身份。
    """
    d1 = register(client, "SN-T1").json()["id"]
    d2 = register(client, "SN-T2").json()["id"]

    client.post(f"/api/devices/{d1}/assign", json={"user_id": "u-employee-1"})
    auth("u-employee-1")
    client.post(f"/api/devices/{d1}/bind", json={"nickname": "会议录音笔"})
    auth()  # 切回管理员
    return d1, d2


# ---------------------------------------------------------------------------
# 领用记录
# ---------------------------------------------------------------------------
def test_assignments_table_lists_across_devices(client: TestClient, auth):
    auth()
    d1, d2 = _seed(client, auth)
    client.post(f"/api/devices/{d2}/assign", json={"user_id": "u-employee-2"})

    response = client.get("/api/tables/assignments")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    sn_set = {item["device_sn"] for item in body["items"]}
    assert sn_set == {"SN-T1", "SN-T2"}
    assert all(item["is_current"] is True for item in body["items"])


def test_assignments_table_filters_by_state_and_keyword(client: TestClient, auth):
    auth()
    d1, _ = _seed(client, auth)
    client.post(f"/api/devices/{d1}/return", json={"reason": "设备损坏"})

    current = client.get("/api/tables/assignments", params={"state": "current"}).json()
    assert current["total"] == 0

    returned = client.get("/api/tables/assignments", params={"state": "returned"}).json()
    assert returned["total"] == 1
    assert returned["items"][0]["return_reason"] == "设备损坏"

    matched = client.get("/api/tables/assignments", params={"keyword": "SN-T"}).json()
    assert matched["total"] == 1

    missed = client.get("/api/tables/assignments", params={"keyword": "SN-ZZZ"}).json()
    assert missed["total"] == 0


# ---------------------------------------------------------------------------
# APP 绑定
# ---------------------------------------------------------------------------
def test_bindings_table_lists_with_nickname(client: TestClient, auth):
    auth()
    d1, _ = _seed(client, auth)

    response = client.get("/api/tables/bindings")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["device_sn"] == "SN-T1"
    assert item["nickname"] == "会议录音笔"
    assert item["status"] == "active"
    assert item["user_id"] == "u-employee-1"


def test_bindings_table_filters_by_status(client: TestClient, auth):
    auth()
    d1, _ = _seed(client, auth)
    client.post(f"/api/devices/{d1}/unbind")

    active = client.get("/api/tables/bindings", params={"status": "active"}).json()
    assert active["total"] == 0

    unbound = client.get("/api/tables/bindings", params={"status": "unbound"}).json()
    assert unbound["total"] == 1


# ---------------------------------------------------------------------------
# 设备凭证 / 状态 / 事件
# ---------------------------------------------------------------------------
def test_credentials_table_never_exposes_plaintext(client: TestClient, auth):
    auth()
    register(client, "SN-CRED")

    response = client.get("/api/tables/credentials")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["auth_key_version"] == 1
    assert item["status"] == "active"
    assert item["device_sn"] == "SN-CRED"
    assert "auth_key" not in item


def test_statuses_table_lists_state(client: TestClient, auth):
    auth()
    register(client, "SN-STAT")

    response = client.get("/api/tables/statuses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["online"] is False
    assert body["items"][0]["device_sn"] == "SN-STAT"

    online = client.get("/api/tables/statuses", params={"online": True}).json()
    assert online["total"] == 0


def test_events_table_lists_across_devices(client: TestClient, auth):
    auth()
    register(client, "SN-E1")
    register(client, "SN-E2")

    response = client.get("/api/tables/events")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 2
    assert all(item["event_type"] == "device_registered" for item in body["items"])

    filtered = client.get("/api/tables/events", params={"event_type": "device_disabled"}).json()
    assert filtered["total"] == 0


# ---------------------------------------------------------------------------
# 员工清单与概览
# ---------------------------------------------------------------------------
def test_employees_table_lists_known_assignees(client: TestClient, auth):
    auth()
    _seed(client, auth)

    response = client.get("/api/tables/employees")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["user_id"] == "u-employee-1"
    assert item["device_count"] == 1
    assert item["binding_count"] == 1


def test_overview_aggregates_metrics(client: TestClient, auth):
    auth()
    d1, d2 = _seed(client, auth)

    # 禁用前：设备1 已领用并绑定（绑定即激活 → active），设备2 闲置且可分配
    before = client.get("/api/tables/overview").json()
    assert before["total_devices"] == 2
    assert before["lifecycle"]["registered"] == 1
    assert before["lifecycle"]["active"] == 1
    assert before["assigned_devices"] == 1
    assert before["bound_devices"] == 1
    assert before["idle_devices"] == 1
    assert before["unassigned_employee_count"] == 1

    client.post(f"/api/devices/{d2}/disable")

    response = client.get("/api/tables/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["total_devices"] == 2
    assert body["lifecycle"]["registered"] == 0
    assert body["lifecycle"]["active"] == 1
    assert body["lifecycle"]["disabled"] == 1
    assert body["assigned_devices"] == 1
    assert body["bound_devices"] == 1
    # 设备1 已领用、设备2 已禁用，均不在「在库可领用」之列
    assert body["idle_devices"] == 0
    assert body["active_credentials"] == 2
    assert body["events_today"] >= 2
    assert body["top_assignees"][0]["user_id"] == "u-employee-1"


# ---------------------------------------------------------------------------
# 权限与租户隔离
# ---------------------------------------------------------------------------
def test_tables_require_admin(client: TestClient, auth):
    auth(is_admin=False)
    for path in (
        "/api/tables/assignments",
        "/api/tables/bindings",
        "/api/tables/credentials",
        "/api/tables/statuses",
        "/api/tables/events",
        "/api/tables/employees",
        "/api/tables/overview",
    ):
        response = client.get(path)
        assert response.status_code == 403, path


def test_tables_require_authentication(client: TestClient):
    for path in ("/api/tables/assignments", "/api/tables/overview"):
        assert client.get(path).status_code == 401, path


def test_tables_isolate_tenants(client: TestClient, auth):
    """跨租户管理员可见全部；普通管理员只看到本企业数据。"""
    auth(is_global_admin=True, tenant_id="acme")
    register(client, "SN-GLOBAL")

    auth(is_global_admin=False, tenant_id="acme")
    register(client, "SN-ACME")

    auth(is_global_admin=False, tenant_id="globex")
    register(client, "SN-GLOBEX")

    # globex 管理员看不到 acme 与跨组织的数据
    globex_view = client.get("/api/tables/assignments").json()
    assert globex_view["total"] == 0

    globex_devices = client.get("/api/devices").json()
    assert globex_devices["total"] == 1
    assert globex_devices["items"][0]["sn"] == "SN-GLOBEX"

    # 跨组织管理员可见全部
    auth(is_global_admin=True, tenant_id="acme")
    global_view = client.get("/api/devices").json()
    assert global_view["total"] == 3

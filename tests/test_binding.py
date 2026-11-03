"""APP 绑定 / 解绑 / 昵称测试（README §5.3 / §6 / §7）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def prepare(client: TestClient, auth, sn: str = "SN-B-1", employee: str = "emp-001") -> int:
    """管理员注册并分配设备给员工。"""
    auth("u-admin", is_admin=True)
    device_id = client.post("/api/devices", json={"sn": sn, "model": "VH-1"}).json()["id"]
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": employee})
    return device_id


def test_employee_binds_own_device(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)

    response = client.post(f"/api/devices/{device_id}/bind", json={"nickname": "会议录音卡"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == "emp-001"
    assert body["nickname"] == "会议录音卡"
    assert body["status"] == "active"

    detail = client.get(f"/api/devices/{device_id}")
    assert detail.status_code == 403  # 管理接口需管理员

    # 员工侧可通过「我的设备」查看
    mine = client.get(f"/api/my/devices/{device_id}")
    assert mine.status_code == 200
    assert mine.json()["nickname"] == "会议录音卡"

    auth("u-admin", is_admin=True)
    detail = client.get(f"/api/devices/{device_id}").json()
    assert detail["bound"] is True
    assert detail["nickname"] == "会议录音卡"
    # 首次绑定即激活设备
    assert detail["lifecycle_status"] == "active"
    assert detail["activated_at"] is not None


def test_non_holder_cannot_bind(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-other", is_admin=False)

    response = client.post(f"/api/devices/{device_id}/bind", json={})
    assert response.status_code == 403
    assert "领用" in response.json()["message"]


def test_double_bind_conflicts(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)
    assert client.post(f"/api/devices/{device_id}/bind", json={}).status_code == 200
    second = client.post(f"/api/devices/{device_id}/bind", json={})
    assert second.status_code == 409


def test_disabled_device_cannot_be_bound(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("u-admin", is_admin=True)
    client.post(f"/api/devices/{device_id}/disable")

    auth("emp-001", is_admin=False)
    response = client.post(f"/api/devices/{device_id}/bind", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_device_state"


def test_nickname_update_and_history(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)
    client.post(f"/api/devices/{device_id}/bind", json={"nickname": "旧昵称"})

    response = client.put(f"/api/devices/{device_id}/nickname", json={"nickname": "新昵称"})
    assert response.status_code == 200
    assert response.json()["nickname"] == "新昵称"

    # 其它员工不能改昵称
    auth("emp-other", is_admin=False)
    denied = client.put(f"/api/devices/{device_id}/nickname", json={"nickname": "hack"})
    assert denied.status_code == 403


def test_unbind_by_owner_and_by_admin(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)
    client.post(f"/api/devices/{device_id}/bind", json={"nickname": "卡"})

    # 员工自助解绑
    response = client.post(f"/api/devices/{device_id}/unbind")
    assert response.status_code == 200
    assert response.json()["status"] == "unbound"

    # 重新绑定后由管理员撤销
    client.post(f"/api/devices/{device_id}/bind", json={"nickname": "卡"})
    auth("u-admin", is_admin=True)
    revoked = client.post(f"/api/devices/{device_id}/unbind")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "unbound"


def test_unbind_requires_binding(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)
    response = client.post(f"/api/devices/{device_id}/unbind")
    assert response.status_code == 409


def test_binding_history_preserved(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)
    client.post(f"/api/devices/{device_id}/bind", json={"nickname": "第一次"})
    client.post(f"/api/devices/{device_id}/unbind")
    client.post(f"/api/devices/{device_id}/bind", json={"nickname": "第二次"})

    auth("u-admin", is_admin=True)
    history = client.get(f"/api/devices/{device_id}/bindings").json()
    assert history["total"] == 2
    nicknames = sorted(item["nickname"] for item in history["items"])
    assert nicknames == sorted(["第一次", "第二次"])


def test_binding_records_events(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-001", is_admin=False)
    client.post(f"/api/devices/{device_id}/bind", json={"nickname": "卡"})
    client.post(f"/api/devices/{device_id}/unbind")

    auth("u-admin", is_admin=True)
    types = [e["event_type"] for e in client.get(f"/api/devices/{device_id}/history").json()["items"]]
    assert "device_bound" in types
    assert "device_unbound" in types
    assert "device_activated" in types
    assert "device_assigned" in types

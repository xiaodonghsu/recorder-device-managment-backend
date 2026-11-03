"""设备领用与归还测试（README §5.2 / §5.4）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def register(client: TestClient, sn: str = "SN-A-1") -> int:
    return client.post("/api/devices", json={"sn": sn, "model": "VH-1"}).json()["id"]


def test_assign_device(client: TestClient, auth):
    auth()
    device_id = register(client)

    response = client.post(
        f"/api/devices/{device_id}/assign", json={"user_id": "emp-001", "note": "新员工入职"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == "emp-001"
    assert body["returned_at"] is None
    assert body["tenant_id"] == "acme"

    detail = client.get(f"/api/devices/{device_id}").json()
    assert detail["assigned"] is True
    assert detail["assigned_user_id"] == "emp-001"


def test_assign_twice_conflicts(client: TestClient, auth):
    auth()
    device_id = register(client)
    assert client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-1"}).status_code == 200
    second = client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-2"})
    assert second.status_code == 409
    assert "已被领用" in second.json()["message"]


def test_assign_disabled_device_rejected(client: TestClient, auth):
    auth()
    device_id = register(client)
    client.post(f"/api/devices/{device_id}/disable")
    response = client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-1"})
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_device_state"


def test_return_device_closes_assignment_and_binding(client: TestClient, auth):
    auth()
    device_id = register(client)
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"})

    # 员工绑定
    auth("emp-001", is_admin=False)
    bound = client.post(f"/api/devices/{device_id}/bind", json={"nickname": "我的录音卡"})
    assert bound.status_code == 200, bound.text

    # 管理员归还
    auth("u-admin", is_admin=True)
    response = client.post(f"/api/devices/{device_id}/return", json={"reason": "项目结束"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["returned_at"] is not None
    assert body["return_reason"] == "项目结束"

    detail = client.get(f"/api/devices/{device_id}").json()
    assert detail["assigned"] is False
    assert detail["bound"] is False


def test_returned_device_can_be_reassigned(client: TestClient, auth):
    auth()
    device_id = register(client)
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-1"})
    client.post(f"/api/devices/{device_id}/return", json={"reason": "调岗"})

    response = client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-2"})
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == "emp-2"

    # 历史记录保留
    history = client.get(f"/api/devices/{device_id}/assignments").json()
    assert history["total"] == 2


def test_return_unassigned_device_conflicts(client: TestClient, auth):
    auth()
    device_id = register(client)
    response = client.post(f"/api/devices/{device_id}/return", json={})
    assert response.status_code == 409


def test_returned_employee_loses_access(client: TestClient, auth):
    auth()
    device_id = register(client)
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"})

    auth("emp-001", is_admin=False)
    assert client.post(f"/api/devices/{device_id}/bind", json={}).status_code == 200

    auth("u-admin", is_admin=True)
    client.post(f"/api/devices/{device_id}/return", json={"reason": "离职回收"})

    # 归还后原员工不能再绑定
    auth("emp-001", is_admin=False)
    response = client.post(f"/api/devices/{device_id}/bind", json={})
    assert response.status_code == 403

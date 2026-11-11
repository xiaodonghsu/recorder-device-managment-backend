"""安全约束测试（README §7）。"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db.models import DeviceBinding, DeviceCredential


def test_missing_token_rejected(client: TestClient):
    response = client.get("/api/devices")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_non_admin_cannot_use_admin_api(client: TestClient, auth):
    auth("emp-001", is_admin=False)
    response = client.get("/api/devices")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_non_admin_cannot_register_device(client: TestClient, auth):
    auth("emp-001", is_admin=False)
    response = client.post("/api/devices", json={"sn": "SN-X-1"})
    assert response.status_code == 403


def test_cross_tenant_device_is_invisible(client: TestClient, auth):
    auth("u-admin", tenant_id="acme", is_admin=True)
    device_id = client.post("/api/devices", json={"sn": "SN-T-1"}).json()["id"]

    # 切换到另一个企业的管理员
    auth("u-admin-2", tenant_id="globex", is_admin=True)
    assert client.get(f"/api/devices/{device_id}").status_code == 404
    assert client.get("/api/devices").json()["total"] == 0

    # 也不能分配 / 绑定该设备
    assert client.post(f"/api/devices/{device_id}/assign", json={"user_id": "x"}).status_code == 404
    assert client.post(f"/api/devices/{device_id}/bind", json={}).status_code == 404


def test_cross_tenant_assignment_rejected(client: TestClient, auth):
    auth("u-admin", tenant_id="acme", is_admin=True)
    device_id = client.post("/api/devices", json={"sn": "SN-T-2"}).json()["id"]

    auth("u-admin", tenant_id="acme", is_admin=True)
    response = client.post(
        f"/api/devices/{device_id}/assign", json={"user_id": "emp", "tenant_id": "globex"}
    )
    assert response.status_code == 403


def test_global_admin_sees_all_tenants(client: TestClient, auth):
    auth("u-admin", tenant_id="acme", is_admin=True)
    client.post("/api/devices", json={"sn": "SN-G-1"})

    auth("root", tenant_id="globex", is_admin=True, is_global_admin=True)
    assert client.get("/api/devices").json()["total"] == 1


def test_admin_listings_omit_auth_key(client: TestClient, auth):
    auth()
    created = client.post("/api/devices", json={"sn": "SN-K-1"}).json()
    listed = client.get(f"/api/devices/{created['id']}/credential").json()
    assert all("auth_key" not in item for item in listed)
    assert "auth_key" not in client.get("/api/devices").json()["items"][0]


def test_credential_rotation(client: TestClient, auth, session):
    auth()
    device_id = client.post("/api/devices", json={"sn": "SN-K-2"}).json()["id"]
    assert client.post(
        f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"}
    ).status_code == 200

    response = client.post(f"/api/devices/{device_id}/credential/rotate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_key_version"] == 2
    assert body["auth_key"].startswith("vhk_")

    credentials = client.get(f"/api/devices/{device_id}/credential").json()
    assert len(credentials) == 2
    active = [c for c in credentials if c["status"] == "active"]
    assert len(active) == 1
    assert active[0]["auth_key_version"] == 2

    auth("emp-001", is_admin=False)
    assert client.get("/api/my/devices").json()["items"][0]["auth_key"] == body["auth_key"]

    auth("emp-001", tenant_id="globex", is_admin=False)
    assert client.get("/api/my/devices").json()["items"] == []

    auth()
    assert client.post(f"/api/devices/{device_id}/credential/revoke").status_code == 200
    auth("emp-001", is_admin=False)
    assert client.get("/api/my/devices").json()["items"][0]["auth_key"] is None


@pytest.mark.parametrize("legacy", [False, True], ids=["expired", "legacy"])
def test_my_devices_keeps_device_without_usable_key(client: TestClient, auth, session, legacy):
    auth()
    device_id = client.post("/api/devices", json={"sn": "SN-K-OLD"}).json()["id"]
    assert client.post(
        f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"}
    ).status_code == 200

    credential = session.query(DeviceCredential).filter_by(device_id=device_id).one()
    if legacy:
        credential.auth_key = None
    else:
        credential.expires_at = datetime(2000, 1, 1)
    session.commit()

    auth("emp-001", is_admin=False)
    body = client.get("/api/my/devices").json()
    assert body["total"] == 1
    assert body["items"][0]["auth_key"] is None


def test_only_one_active_credential_per_device(client: TestClient, auth, session):
    """数据库层保证「每台设备同时最多一个有效凭证」。"""
    auth()
    device_id = client.post("/api/devices", json={"sn": "SN-K-3"}).json()["id"]

    from sqlalchemy.exc import IntegrityError

    session.add(
        DeviceCredential(
            device_id=device_id,
            auth_key="x" * 64,
            auth_key_version=99,
            status="active",
        )
    )
    try:
        session.commit()
        raise AssertionError("应当触发 uk_credential_active 唯一约束")
    except IntegrityError:
        session.rollback()


def test_only_one_active_binding_per_device(client: TestClient, auth, session):
    """数据库层保证「一台设备同时只能存在一个有效绑定」。"""
    auth()
    device_id = client.post("/api/devices", json={"sn": "SN-B-9"}).json()["id"]
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"})

    auth("emp-001", is_admin=False)
    assert client.post(f"/api/devices/{device_id}/bind", json={}).status_code == 200

    from sqlalchemy.exc import IntegrityError

    existing = session.query(DeviceBinding).filter_by(device_id=device_id).one()
    session.add(
        DeviceBinding(
            device_id=device_id,
            assignment_id=existing.assignment_id,
            user_id=existing.user_id,
            status="active",
        )
    )
    try:
        session.commit()
        raise AssertionError("应当触发 uk_binding_device 唯一约束")
    except IntegrityError:
        session.rollback()


def test_sn_is_immutable(client: TestClient, auth):
    auth()
    device_id = client.post("/api/devices", json={"sn": "SN-IMM"}).json()["id"]
    response = client.put(f"/api/devices/{device_id}", json={"sn": "SN-CHANGED"})
    # SN 不在可更新字段内，被忽略
    assert response.status_code == 200
    assert response.json()["sn"] == "SN-IMM"


def test_binding_rejects_sn_mismatch(client: TestClient, auth):
    auth("u-admin", is_admin=True)
    device_id = client.post("/api/devices", json={"sn": "SN-M-1"}).json()["id"]
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"})

    auth("emp-001", is_admin=False)
    response = client.post(f"/api/devices/{device_id}/bind", json={"sn": "SN-OTHER"})
    assert response.status_code == 409


def test_403_response_carries_admin_diagnostics(client: TestClient, auth):
    """403 要能看出当前用户的角色 / 用户组 / 权限，便于自助定位。"""
    auth("emp-001", is_admin=False, roles=["auditor"], groups=["acme"])
    response = client.get("/api/devices")
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "forbidden"
    details = body.get("details") or {}
    assert "reason" in details
    assert "auditor" in details["reason"]
    assert "acme" in details["reason"]


def test_profile_endpoint_exposes_admin_policy(client: TestClient, auth):
    auth("emp-001", is_admin=False, roles=["auditor"], groups=["acme"])
    response = client.get("/api/my/profile")
    assert response.status_code == 200
    body = response.json()
    assert body["is_admin"] is False
    assert body["roles"] == ["auditor"]
    assert body["groups"] == ["acme"]
    assert "admin_policy" in body
    assert "admin_roles" in body["admin_policy"]

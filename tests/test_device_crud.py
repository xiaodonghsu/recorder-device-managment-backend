"""设备入库、批量入库、档案维护相关测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def register(client: TestClient, sn: str = "SN-0001", **extra):
    payload = {"sn": sn, "bluetooth_name": "VoiceHub", "model": "VH-1", "manufacturer": "嘉环声枢"}
    payload.update(extra)
    return client.post("/api/devices", json=payload)


def test_register_device_returns_plaintext_authkey(client: TestClient, auth):
    auth()
    response = register(client)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["sn"] == "SN-0001"
    assert body["lifecycle_status"] == "registered"
    assert body["tenant_id"] == "acme"
    assert body["assigned"] is False
    assert body["bound"] is False
    # AuthKey 明文仅返回一次
    assert body["auth_key"].startswith("vhk_")
    assert len(body["auth_key"]) > 20


def test_register_duplicate_sn_conflicts(client: TestClient, auth):
    auth()
    assert register(client, "SN-DUP").status_code == 201
    response = register(client, "SN-DUP")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_imported_authkey_is_accepted(client: TestClient, auth):
    auth()
    response = register(client, "SN-IMPORT", auth_key="my-legacy-authkey-0001")
    assert response.status_code == 201
    # 导入的 AuthKey 不再回显
    assert response.json()["auth_key"] is None


def test_device_list_and_detail(client: TestClient, auth):
    auth()
    device_id = register(client, "SN-LIST").json()["id"]

    listing = client.get("/api/devices")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == device_id

    detail = client.get(f"/api/devices/{device_id}")
    assert detail.status_code == 200
    assert detail.json()["sn"] == "SN-LIST"


def test_batch_register_continues_on_error(client: TestClient, auth):
    auth()
    register(client, "SN-B-1")
    response = client.post(
        "/api/devices/batch",
        json={
            "items": [
                {"sn": "SN-B-1"},
                {"sn": "SN-B-2", "model": "VH-2"},
                {"sn": "SN-B-3"},
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    failed = [i for i in body["items"] if not i["success"]][0]
    assert failed["sn"] == "SN-B-1"


def test_update_device_profile_keeps_sn(client: TestClient, auth):
    auth()
    device_id = register(client, "SN-UPD").json()["id"]
    response = client.put(f"/api/devices/{device_id}", json={"firmware_version": "1.2.3"})
    assert response.status_code == 200
    assert response.json()["firmware_version"] == "1.2.3"
    assert response.json()["sn"] == "SN-UPD"


def test_registration_creates_credential_and_status_rows(client: TestClient, auth, session):
    from app.db.models import DeviceCredential, DeviceStatus

    auth()
    device_id = register(client, "SN-ROWS").json()["id"]

    credential = session.query(DeviceCredential).filter_by(device_id=device_id).one()
    assert credential.auth_key_version == 1
    assert credential.status == "active"
    # 数据库不保存明文 AuthKey
    assert "vhk_" not in credential.auth_key_hash
    assert len(credential.auth_key_hash) == 64

    status = session.query(DeviceStatus).filter_by(device_id=device_id).one()
    assert status.online is False


def test_lifecycle_transitions_and_events(client: TestClient, auth):
    auth()
    device_id = register(client, "SN-LIFE").json()["id"]

    assert client.post(f"/api/devices/{device_id}/disable").status_code == 200
    assert client.post(f"/api/devices/{device_id}/enable").status_code == 200
    # retired 是终态
    assert client.post(f"/api/devices/{device_id}/retire").status_code == 200
    assert client.post(f"/api/devices/{device_id}/enable").status_code == 409

    history = client.get(f"/api/devices/{device_id}/history").json()
    types = [e["event_type"] for e in history["items"]]
    assert "device_registered" in types
    assert "device_disabled" in types
    assert "device_retired" in types

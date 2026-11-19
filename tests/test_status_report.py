"""设备状态上报测试（APP 代报 / README §5.4 / APP 对接说明 §6.3）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import DeviceStatus


def _iso(moment: datetime | None = None) -> str:
    """生成带 Z 的 ISO 8601 采集时刻。"""
    value = moment or datetime.now(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def payload(**overrides):
    body = {
        "sampled_at": _iso(),
        "connected": True,
        "battery_level": 78,
        "charging": True,
        "recording": False,
    }
    body.update(overrides)
    return body


def prepare(client: TestClient, auth, *, sn: str = "SN-STATUS-1", bind: bool = True) -> int:
    """管理员入库 + 领用，员工按需完成绑定。"""
    auth("u-admin", is_admin=True)
    device_id = client.post("/api/devices", json={"sn": sn, "model": "VH-1"}).json()["id"]
    client.post(f"/api/devices/{device_id}/assign", json={"user_id": "emp-001"})
    if bind:
        auth("emp-001", is_admin=False)
        assert client.post(f"/api/devices/{device_id}/bind", json={}).status_code == 200
    return device_id


def put(client: TestClient, device_id: int, **overrides):
    return client.put(f"/api/my/devices/{device_id}/status", json=payload(**overrides))


def test_report_updates_all_read_paths(client: TestClient, auth):
    device_id = prepare(client, auth)

    response = put(client, device_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["online"] is True
    assert body["battery_level"] == 78
    assert body["charging"] is True
    assert body["recording"] is False
    assert body["last_seen_at"] is not None
    assert body["last_sync_at"] is None
    assert body["server_time"] and not body["server_time"].endswith("Z")
    assert body["heartbeat_interval_seconds"] == 60

    mine = client.get(f"/api/my/devices/{device_id}").json()["status"]
    assert mine["online"] is True and mine["battery_level"] == 78

    auth("u-admin", is_admin=True)
    detail = client.get(f"/api/devices/{device_id}/status").json()
    assert detail["online"] is True and detail["battery_level"] == 78
    online_rows = client.get("/api/tables/statuses?online=true").json()["items"]
    assert any(row["device_id"] == device_id for row in online_rows)


def test_optional_fields_keep_previous_values(client: TestClient, auth):
    """心跳只带变化项时，未传字段不能把旧值清掉。"""
    device_id = prepare(client, auth)
    assert put(client, device_id).status_code == 200

    second = client.put(
        f"/api/my/devices/{device_id}/status",
        json={"sampled_at": _iso(), "connected": True, "battery_level": 51},
    )
    assert second.status_code == 200, second.text
    assert second.json()["battery_level"] == 51
    assert second.json()["charging"] is True

    cleared = put(client, device_id, battery_level=None)
    assert cleared.status_code == 200
    assert cleared.json()["battery_level"] is None


def test_non_holder_unbound_returned_and_disabled_are_rejected(client: TestClient, auth):
    device_id = prepare(client, auth)

    auth("emp-other", is_admin=False)
    response = put(client, device_id)
    assert response.status_code == 403
    assert "领用" in response.json()["message"]

    unbound_id = prepare(client, auth, sn="SN-STATUS-2", bind=False)
    auth("emp-001", is_admin=False)
    unbound = put(client, unbound_id)
    assert unbound.status_code == 403
    assert "绑定" in unbound.json()["message"]

    auth("u-admin", is_admin=True)
    assert client.post(f"/api/devices/{unbound_id}/disable").status_code == 200
    auth("emp-001", is_admin=False)
    assert put(client, unbound_id).json()["code"] == "invalid_device_state"

    auth("u-admin", is_admin=True)
    assert client.post(f"/api/devices/{device_id}/return", json={}).status_code == 200
    auth("emp-001", is_admin=False)
    assert put(client, device_id).status_code == 403


def test_cross_tenant_report_is_404(client: TestClient, auth):
    device_id = prepare(client, auth)
    auth("emp-other", tenant_id="globex", is_admin=False)

    response = put(client, device_id)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_disconnect_marks_offline_without_refreshing_last_seen(client: TestClient, auth):
    device_id = prepare(client, auth)
    first = put(client, device_id).json()

    second = put(client, device_id, connected=False).json()
    assert second["online"] is False
    assert second["last_seen_at"] == first["last_seen_at"]
    assert second["battery_level"] == 78


def test_online_expires_on_read(client: TestClient, auth, session: Session):
    """APP 掉线后没有后续心跳，读取时也必须判定离线。"""
    device_id = prepare(client, auth)
    assert put(client, device_id).json()["online"] is True

    row = session.get(DeviceStatus, device_id)
    row.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    session.commit()

    auth("u-admin", is_admin=True)
    assert client.get(f"/api/devices/{device_id}/status").json()["online"] is False
    online_rows = client.get("/api/tables/statuses?online=true").json()["items"]
    assert not any(item["device_id"] == device_id for item in online_rows)
    offline_rows = client.get("/api/tables/statuses?online=false").json()["items"]
    assert any(item["device_id"] == device_id for item in offline_rows)
    overview = client.get("/api/tables/overview").json()
    assert overview["online_devices"] == 0


def test_stale_and_future_snapshots_are_rejected(client: TestClient, auth):
    device_id = prepare(client, auth)
    assert put(client, device_id).status_code == 200

    now = datetime.now(timezone.utc)
    stale = put(client, device_id, sampled_at=_iso(now - timedelta(hours=2)))
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_status_report"

    future = put(client, device_id, sampled_at=_iso(now + timedelta(hours=2)))
    assert future.status_code == 422
    assert future.json()["code"] == "invalid_sample_time"


def test_server_owned_fields_and_battery_range(client: TestClient, auth):
    device_id = prepare(client, auth)

    forged = put(client, device_id, online=True)
    assert forged.status_code == 422
    assert forged.json()["code"] == "validation_error"
    assert put(client, device_id, updated_at=_iso()).status_code == 422
    assert put(client, device_id, battery_level=101).status_code == 422
    assert put(client, device_id, battery_level=-1).status_code == 422


def test_events_record_only_state_transitions(client: TestClient, auth):
    device_id = prepare(client, auth)

    assert put(client, device_id).status_code == 200  # online / charging 跃迁
    assert put(client, device_id).status_code == 200  # 值未变，不写事件
    assert put(client, device_id, recording=True).status_code == 200  # recording 跃迁

    auth("u-admin", is_admin=True)
    events = client.get(
        f"/api/devices/{device_id}/history?event_type=device_status_changed"
    ).json()
    assert events["total"] == 2
    assert events["items"][0]["event_data"]["reporter"] == "emp-001"


def test_admin_who_is_not_holder_cannot_report(client: TestClient, auth):
    """管理员也不是领用人时同样不能代报，权限口径与员工一致。"""
    device_id = prepare(client, auth)
    auth("u-admin", is_admin=True)

    response = put(client, device_id)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# V1.3：容量 / 固件 / 增益 / metrics 扩展槽
# ---------------------------------------------------------------------------
FREE = 12 * 1024**3
TOTAL = 32 * 1024**3
METRICS = {"rssi_dbm": -61, "codec": "opus"}


def extended_payload(**overrides):
    body = {
        "sampled_at": _iso(),
        "connected": True,
        "storage_free_bytes": FREE,
        "storage_total_bytes": TOTAL,
        "firmware_version": "1.4.0",
        "gain": 2,
        "metrics": dict(METRICS),
    }
    body.update(overrides)
    return body


def put_extended(client: TestClient, device_id: int, **overrides):
    return client.put(
        f"/api/my/devices/{device_id}/status", json=extended_payload(**overrides)
    )


def test_extended_fields_reach_all_read_paths(client: TestClient, auth):
    """新字段写库后，响应 / 我的设备 / 管理员详情 / 状态列表都要能看到。"""
    device_id = prepare(client, auth)
    response = put_extended(client, device_id)
    assert response.status_code == 200, response.text
    ack = response.json()
    assert ack["storage_free_bytes"] == FREE
    assert ack["storage_total_bytes"] == TOTAL
    assert ack["firmware_version"] == "1.4.0"
    assert ack["gain"] == 2
    assert ack["metrics"] == METRICS

    mine = client.get(f"/api/my/devices/{device_id}").json()["status"]
    assert mine["storage_free_bytes"] == FREE and mine["gain"] == 2
    assert mine["metrics"] == METRICS

    auth("u-admin", is_admin=True)
    detail = client.get(f"/api/devices/{device_id}/status").json()
    assert detail["firmware_version"] == "1.4.0"
    assert detail["storage_total_bytes"] == TOTAL

    rows = client.get("/api/tables/statuses").json()["items"]
    row = next(item for item in rows if item["device_id"] == device_id)
    assert row["storage_free_bytes"] == FREE
    assert row["firmware_version"] == "1.4.0"
    assert row["gain"] == 2
    assert row["metrics"] == METRICS


def test_extended_fields_keep_previous_values_and_null_clears(client: TestClient, auth):
    """心跳只带变化项时不能清掉旧值；显式 null 才清空。"""
    device_id = prepare(client, auth)
    assert put_extended(client, device_id).status_code == 200

    second = client.put(
        f"/api/my/devices/{device_id}/status",
        json={"sampled_at": _iso(), "connected": True, "gain": 3},
    )
    assert second.status_code == 200, second.text
    assert second.json()["gain"] == 3
    assert second.json()["storage_free_bytes"] == FREE
    assert second.json()["storage_total_bytes"] == TOTAL
    assert second.json()["firmware_version"] == "1.4.0"
    assert second.json()["metrics"] == METRICS

    cleared = client.put(
        f"/api/my/devices/{device_id}/status",
        json={
            "sampled_at": _iso(),
            "connected": True,
            "storage_free_bytes": None,
            "firmware_version": None,
            "gain": None,
            "metrics": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["storage_free_bytes"] is None
    assert body["firmware_version"] is None
    assert body["gain"] is None
    assert body["metrics"] is None
    assert body["storage_total_bytes"] == TOTAL  # 未传的字段继续保留


def test_metrics_object_replaces_previous_snapshot(client: TestClient, auth):
    device_id = prepare(client, auth)
    assert put_extended(client, device_id).status_code == 200

    replaced = put_extended(client, device_id, metrics={"sdk_version": "2.1.0"})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["metrics"] == {"sdk_version": "2.1.0"}


def test_reported_firmware_does_not_touch_device_profile(client: TestClient, auth):
    """实测固件只写 device_status，不改 device 主表的档案字段。"""
    device_id = prepare(client, auth)
    assert put_extended(client, device_id, firmware_version="9.9.9").status_code == 200

    auth("u-admin", is_admin=True)
    profile = client.get(f"/api/devices/{device_id}").json()
    assert profile["firmware_version"] is None
    assert profile["status"]["firmware_version"] == "9.9.9"


def test_extended_field_validation(client: TestClient, auth):
    device_id = prepare(client, auth)

    assert put_extended(client, device_id, gain=0).status_code == 422
    assert put_extended(client, device_id, gain=4).status_code == 422
    assert put_extended(client, device_id, storage_free_bytes=-1).status_code == 422
    assert put_extended(client, device_id, storage_total_bytes=-1).status_code == 422
    assert put_extended(client, device_id, firmware_version="   ").status_code == 422
    assert put_extended(client, device_id, firmware_version="x" * 33).status_code == 422

    # 本次请求内 free > total：Pydantic 的跨字段校验
    same_request = put_extended(
        client, device_id, storage_free_bytes=11, storage_total_bytes=10
    )
    assert same_request.status_code == 422

    # metrics 结构约束
    assert put_extended(client, device_id, metrics={"gain": 3}).status_code == 422
    assert put_extended(client, device_id, metrics={"bad-key": 1}).status_code == 422
    assert put_extended(client, device_id, metrics={"nested": {"a": 1}}).status_code == 422
    assert put_extended(client, device_id, metrics={"big": "x" * 3000}).status_code == 422
    assert put_extended(
        client, device_id, metrics={f"k{i}": i for i in range(21)}
    ).status_code == 422
    assert put_extended(client, device_id, metrics=[1, 2]).status_code == 422


def test_capacity_checked_against_merged_values(client: TestClient, auth):
    """APP 只更新 free、total 留在上次心跳时也必须拒绝，且不能写脏库。"""
    device_id = prepare(client, auth)
    first = client.put(
        f"/api/my/devices/{device_id}/status",
        json={
            "sampled_at": _iso(),
            "connected": True,
            "storage_free_bytes": 100,
            "storage_total_bytes": 1000,
        },
    )
    assert first.status_code == 200, first.text

    conflict = client.put(
        f"/api/my/devices/{device_id}/status",
        json={"sampled_at": _iso(), "connected": True, "storage_free_bytes": 2000},
    )
    assert conflict.status_code == 422
    assert conflict.json()["code"] == "invalid_storage_capacity"
    assert conflict.json()["details"]["storage_total_bytes"] == 1000

    auth("u-admin", is_admin=True)
    detail = client.get(f"/api/devices/{device_id}/status").json()
    assert detail["storage_free_bytes"] == 100
    assert detail["storage_total_bytes"] == 1000


def test_status_ext_migration_is_idempotent():
    """存量 SQLite 库只有旧 8 列时，启动升级应补列且不丢数据。"""
    from sqlalchemy import create_engine, inspect, text

    from app.db.session import _upgrade_device_status_table

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE device_status ("
            "device_id INTEGER PRIMARY KEY, online BOOLEAN NOT NULL DEFAULT 0, "
            "battery_level SMALLINT, charging BOOLEAN NOT NULL DEFAULT 0, "
            "recording BOOLEAN NOT NULL DEFAULT 0, last_seen_at DATETIME, "
            "last_sync_at DATETIME, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.exec_driver_sql("INSERT INTO device_status (device_id) VALUES (1)")

    _upgrade_device_status_table(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("device_status")}
    assert {
        "storage_free_bytes",
        "storage_total_bytes",
        "firmware_version",
        "gain",
        "metrics",
    } <= columns

    _upgrade_device_status_table(engine)  # 再跑一次不能报错
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM device_status")).scalar_one() == 1
    engine.dispose()

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


def test_my_devices_matches_employee_no(client: TestClient, auth):
    """回归：领用记录按工号写入，员工 Token 里 user_id 是 UUID、username 是工号。

    preferred_username（工号）才是业务唯一标识；name（姓名）只能用于展示。
    """
    auth("u-admin", is_admin=True)
    created = client.post("/api/devices", json={"sn": "SN-ALIAS-1"}).json()
    device_id = created["id"]
    assigned = client.post(f"/api/devices/{device_id}/assign", json={"user_id": "JH000059"})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["user_id"] == "JH000059"

    # 模拟生产 /api/userinfo：sub=UUID，preferred_username=工号，name=姓名
    auth(
        "7994b554-b159-4a6b-bdba-0703193e6ed8",
        is_admin=False,
        username="JH000059",
        display_name="胥晓冬",
    )

    listing = client.get("/api/my/devices")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [device_id]
    assert body["items"][0]["auth_key"] == created["auth_key"]
    assert listing.headers["Cache-Control"] == "no-store"
    assert client.get(f"/api/my/devices/{device_id}").status_code == 200

    # 绑定关系必须沿用领用记录里的标识，否则组合外键 (assignment_id, device_id,
    # user_id) 不成立，且解绑 / 改昵称也会因标识不匹配被拒绝。
    bound = client.post(f"/api/devices/{device_id}/bind", json={"nickname": "我的录音卡"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["user_id"] == "JH000059"

    renamed = client.put(f"/api/devices/{device_id}/nickname", json={"nickname": "新昵称"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["nickname"] == "新昵称"
    assert client.post(f"/api/devices/{device_id}/unbind").status_code == 200


def test_my_devices_total_counts_all_matches(client: TestClient, auth):
    """回归：total 是全部匹配记录数，不是当前页行数。"""
    auth("u-admin", is_admin=True)
    for index in range(3):
        device_id = client.post("/api/devices", json={"sn": f"SN-PAGE-{index}"}).json()["id"]
        assert client.post(
            f"/api/devices/{device_id}/assign", json={"user_id": "emp-page"}
        ).status_code == 200

    auth("emp-page", is_admin=False)
    body = client.get("/api/my/devices?page=1&page_size=2").json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_user_refs_excludes_display_name():
    """姓名可能重名，绝不能作为员工唯一标识参与匹配。"""
    from app.api.deps import CurrentUser
    from app.integrations.casdoor import CasdoorIdentity

    identity = CasdoorIdentity(
        user_id="7994b554-b159-4a6b-bdba-0703193e6ed8",
        username="JH000059",
        display_name="胥晓冬",
        tenant_id="bestlink",
    )
    user = CurrentUser(identity=identity)
    assert user.employee_no == "JH000059"
    assert user.user_refs == ["7994b554-b159-4a6b-bdba-0703193e6ed8", "JH000059"]


def test_my_profile_exposes_employee_no_and_display_name(client: TestClient, auth):
    auth(
        "7994b554-b159-4a6b-bdba-0703193e6ed8",
        is_admin=False,
        username="JH000059",
        display_name="胥晓冬",
    )
    body = client.get("/api/my/profile").json()
    assert body["user_id"] == "7994b554-b159-4a6b-bdba-0703193e6ed8"
    assert body["username"] == "JH000059"
    assert body["preferred_username"] == "JH000059"
    assert body["employee_no"] == "JH000059"
    assert body["display_name"] == "胥晓冬"


def test_my_devices_ignores_assignment_stored_with_display_name(client: TestClient, auth):
    """误把姓名写进领用记录时，不能被当成同一员工命中。"""
    auth("u-admin", is_admin=True)
    device_id = client.post("/api/devices", json={"sn": "SN-NAME-1"}).json()["id"]
    assert client.post(
        f"/api/devices/{device_id}/assign", json={"user_id": "胥晓冬"}
    ).status_code == 200

    auth(
        "7994b554-b159-4a6b-bdba-0703193e6ed8",
        is_admin=False,
        username="JH000059",
        display_name="胥晓冬",
    )
    assert client.get("/api/my/devices").json()["total"] == 0

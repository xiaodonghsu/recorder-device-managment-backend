"""工号（preferred_username）存在性校验回归测试。

覆盖分配链路的三种查询结果、三档策略，以及 Casdoor 响应解析的真实形态。
所有 HTTP 测试通过 conftest 的 FakeCasdoorClient 隔离，不会访问生产 Casdoor。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.casdoor import CasdoorClient, EmployeeLookup

BASE = "https://casdoor.test/auth"

# 生产实调形态：/auth/api/get-user?owner=bestlink&id=bestlink/JH000059
USER_PAYLOAD = {
    "status": "ok",
    "msg": "",
    "data": {
        "owner": "bestlink",
        "name": "JH000059",
        "id": "7994b554-b159-4a6b-bdba-0703193e6ed8",
        "displayName": "胥晓冬",
        "email": "xxd@bestlink.com.cn",
        "isForbidden": False,
        "isDeleted": False,
        "deletedTime": "",
    },
}


def _device(client: TestClient, sn: str) -> int:
    response = client.post("/api/devices", json={"sn": sn})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _assign(client: TestClient, device_id: int, user_id: str):
    return client.post(f"/api/devices/{device_id}/assign", json={"user_id": user_id})


# ---------------------------------------------------------------------------
# 分配链路
# ---------------------------------------------------------------------------
def test_assign_checks_employee_before_writing(client: TestClient, auth, casdoor):
    """默认策略就会真的发起工号查询，命中才落库。"""
    auth("u-admin", is_admin=True)
    device_id = _device(client, "SN-LOOKUP-OK")

    response = _assign(client, device_id, "JH000059")
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == "JH000059"
    assert casdoor.lookup_calls[0][:2] == ("acme", "JH000059")


def test_assign_normalizes_case_using_casdoor_name(client: TestClient, auth, casdoor):
    auth("u-admin", is_admin=True)
    device_id = _device(client, "SN-LOOKUP-CASE")
    casdoor.lookup_result = EmployeeLookup(
        "found", employee_no="JH000059", display_name="胥晓冬", owner="acme"
    )

    response = _assign(client, device_id, "jh000059")
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == "JH000059"


@pytest.mark.parametrize(
    "reason",
    ["组织 acme 中不存在工号 'NOBODY'", "该用户属于组织 globex，不属于 acme", "该账号已禁用或已删除"],
)
def test_assign_rejects_absent_employee(client: TestClient, auth, casdoor, reason):
    """查无此人 / 组织不符 / 账号禁用都必须拒绝，且不产生领用记录。"""
    auth("u-admin", is_admin=True)
    device_id = _device(client, "SN-ABSENT")
    casdoor.lookup_result = EmployeeLookup("absent", reason=reason)

    response = _assign(client, device_id, "NOBODY")
    assert response.status_code == 422
    assert response.json()["code"] == "employee_not_found"
    assert reason in response.json()["message"]
    assert client.get(f"/api/devices/{device_id}").json()["assigned"] is False


def test_assign_unavailable_degrades_under_optional(client: TestClient, auth, casdoor):
    """Casdoor 查询不可用时不能误判成「工号不存在」，optional 下放行。"""
    auth("u-admin", is_admin=True)
    device_id = _device(client, "SN-LOOKUP-OPTIONAL")
    casdoor.lookup_result = EmployeeLookup("unavailable", reason="connect timeout")

    response = _assign(client, device_id, "JH000059")
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == "JH000059"


def test_assign_unavailable_rejected_under_required(
    client: TestClient, auth, casdoor, monkeypatch
):
    auth("u-admin", is_admin=True)
    monkeypatch.setattr(casdoor.cfg, "casdoor_verify_employee", "required")
    device_id = _device(client, "SN-LOOKUP-REQUIRED")
    casdoor.lookup_result = EmployeeLookup("unavailable", reason="connect timeout")

    response = _assign(client, device_id, "JH000059")
    assert response.status_code == 502
    assert response.json()["code"] == "casdoor_error"
    assert "connect timeout" in response.json()["message"]


def test_assign_off_skips_casdoor_query(client: TestClient, auth, casdoor, monkeypatch):
    auth("u-admin", is_admin=True)
    monkeypatch.setattr(casdoor.cfg, "casdoor_verify_employee", "off")
    device_id = _device(client, "SN-LOOKUP-OFF")

    response = _assign(client, device_id, "JH000059")
    assert response.status_code == 200, response.text
    assert casdoor.lookup_calls == []


def test_assign_rejects_casdoor_uuid(client: TestClient, auth, casdoor):
    """UUID 不是工号；反查不到时明确报错，绝不静默写进业务表。"""
    auth("u-admin", is_admin=True)
    device_id = _device(client, "SN-LOOKUP-UUID")

    response = _assign(client, device_id, "7994b554-b159-4a6b-bdba-0703193e6ed8")
    assert response.status_code == 422
    assert response.json()["code"] == "employee_no_required"
    assert "preferred_username" in response.json()["message"]
    assert client.get(f"/api/devices/{device_id}").json()["assigned"] is False


def test_assign_accepts_uuid_when_casdoor_resolves_it(client: TestClient, auth, casdoor):
    """其它 Casdoor 版本若支持按 UUID 反查，反查成功后再走存在性校验。"""
    auth("u-admin", is_admin=True)
    device_id = _device(client, "SN-LOOKUP-UUID-OK")
    casdoor.user_object = {"name": "JH000059", "id": "7994b554-b159-4a6b-bdba-0703193e6ed8"}
    casdoor.lookup_result = EmployeeLookup("found", employee_no="JH000059", owner="acme")

    response = _assign(client, device_id, "7994b554-b159-4a6b-bdba-0703193e6ed8")
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == "JH000059"
    assert casdoor.lookup_calls[0][1] == "JH000059"


def test_assign_passes_admin_access_token_to_casdoor(client: TestClient, auth, casdoor):
    """管理员 Token 只用于代查 Casdoor，不落库、不进响应。"""
    auth("u-admin", is_admin=True, access_token="admin-token")
    device_id = _device(client, "SN-LOOKUP-TOKEN")

    response = _assign(client, device_id, "JH000059")
    assert response.status_code == 200, response.text
    assert casdoor.lookup_calls[0][2] == "admin-token"
    assert "admin-token" not in response.text


# ---------------------------------------------------------------------------
# Casdoor 响应解析（纯函数，用生产实调样例）
# ---------------------------------------------------------------------------
def test_classify_found_payload():
    result = CasdoorClient._classify_user_payload(USER_PAYLOAD, "bestlink", "JH000059")
    assert result.status == "found"
    assert result.employee_no == "JH000059"
    assert result.display_name == "胥晓冬"
    assert result.owner == "bestlink"
    assert result.user_id == "7994b554-b159-4a6b-bdba-0703193e6ed8"


def test_classify_ignores_owner_query_param_and_checks_returned_owner():
    """Casdoor 会忽略 owner 参数，必须靠返回对象的 owner 拦截跨组织工号。"""
    result = CasdoorClient._classify_user_payload(USER_PAYLOAD, "globex", "JH000059")
    assert result.status == "absent"
    assert "不属于 globex" in result.reason


def test_classify_absent_states():
    assert CasdoorClient._classify_user_payload(
        {"status": "ok", "data": None}, "bestlink", "NOBODY"
    ).status == "absent"

    forbidden = {"status": "ok", "data": {**USER_PAYLOAD["data"], "isForbidden": True}}
    assert CasdoorClient._classify_user_payload(
        forbidden, "bestlink", "JH000059"
    ).status == "absent"

    deleted = {"status": "ok", "data": {**USER_PAYLOAD["data"], "deletedTime": "2026-11-10"}}
    assert CasdoorClient._classify_user_payload(
        deleted, "bestlink", "JH000059"
    ).status == "absent"


def test_classify_unavailable_states():
    cases = [
        {"status": "error", "msg": "Unauthorized operation", "data": None},
        {"msg": "broken"},
        None,
    ]
    for payload in cases:
        assert (
            CasdoorClient._classify_user_payload(payload, "bestlink", "JH000059").status
            == "unavailable"
        )


def test_classify_accepts_unwrapped_user_object():
    """部分版本不套 data 包装。"""
    result = CasdoorClient._classify_user_payload(USER_PAYLOAD["data"], "bestlink", "JH000059")
    assert result.status == "found"
    assert result.employee_no == "JH000059"


# ---------------------------------------------------------------------------
# 查询身份链：无 clientSecret 时匿名只读接口可用
# ---------------------------------------------------------------------------
def test_lookup_employee_falls_back_to_anonymous_when_token_rejected():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "authorization" in request.headers:
            return httpx.Response(401, json={"status": "error", "msg": "Unauthorized"})
        assert request.url.params["id"] == "bestlink/JH000059"
        return httpx.Response(200, json=USER_PAYLOAD)

    cfg = Settings(casdoor_base_url=BASE, casdoor_client_secret="")
    client = CasdoorClient(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = asyncio.run(client.lookup_employee("bestlink", "JH000059", access_token="admin-token"))
    assert result.found
    assert result.employee_no == "JH000059"
    assert len(seen) == 2  # 管理员 Token 被拒 → 匿名成功
    assert seen[-1].headers.get("authorization") is None


def test_lookup_employee_reports_absent_not_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": None})

    cfg = Settings(casdoor_base_url=BASE, casdoor_client_secret="")
    client = CasdoorClient(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = asyncio.run(client.lookup_employee("bestlink", "NOBODY"))
    assert result.status == "absent"
    assert "不存在" in result.reason


def test_verify_employee_config_rejects_unknown_value():
    with pytest.raises(ValueError):
        Settings(casdoor_verify_employee="sometimes")

"""设备凭证接口（README §6 POST /devices/{id}/credential/rotate）。

AuthKey 明文仅在签发 / 轮换的响应中返回一次，数据库只保存 HMAC 摘要。
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_admin
from app.db.session import get_db
from app.schemas.device import CredentialIssuedOut, CredentialOut
from app.services import credential_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["设备凭证"])


@router.get(
    "/devices/{device_id}/credential",
    response_model=List[CredentialOut],
    summary="查询凭证历史",
    description="返回该设备的凭证版本记录（不含 AuthKey 明文）。",
)
async def list_credentials(
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> List[CredentialOut]:
    rows = credential_service.list_credentials(db, device_id, user.tenant_scope)
    return [
        CredentialOut(
            device_id=row.device_id,
            auth_key_version=row.auth_key_version,
            status=row.status,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post(
    "/devices/{device_id}/credential/rotate",
    response_model=CredentialIssuedOut,
    summary="轮换 AuthKey",
    description="撤销当前有效凭证并签发新版本；响应中的 AuthKey 明文仅此一次可见。",
)
async def rotate_credential(
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> CredentialIssuedOut:
    credential, plaintext = credential_service.rotate_credential(
        db, device_id, tenant_scope=user.tenant_scope, operator_id=user.user_id
    )
    return CredentialIssuedOut(
        device_id=credential.device_id,
        auth_key=plaintext,
        auth_key_version=credential.auth_key_version,
        issued_at=credential.issued_at,
        expires_at=credential.expires_at,
    )


@router.post(
    "/devices/{device_id}/credential/revoke",
    response_model=CredentialOut,
    summary="撤销当前凭证",
    description="仅撤销，不签发新凭证。",
)
async def revoke_credential(
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> CredentialOut:
    credential = credential_service.revoke_credential(
        db, device_id, tenant_scope=user.tenant_scope, operator_id=user.user_id
    )
    return CredentialOut(
        device_id=credential.device_id,
        auth_key_version=credential.auth_key_version,
        status=credential.status,
        issued_at=credential.issued_at,
        expires_at=credential.expires_at,
        revoked_at=credential.revoked_at,
        created_at=credential.created_at,
    )

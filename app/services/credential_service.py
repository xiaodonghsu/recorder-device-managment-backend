"""设备凭证（AuthKey）服务（README §3.2 / §7.3）。

- AuthKey 只在签发时以明文出现一次，数据库仅保存 HMAC-SHA256 摘要；
- 支持轮换：撤销旧凭证 → 签发新版本，历史凭证保留可追溯；
- 每台设备同时最多一个有效凭证（由 uk_credential_active 生成列唯一索引保证）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import CredentialStatus, EventType
from app.core.errors import NotFoundError
from app.core.security import generate_auth_key, hash_auth_key
from app.db.models import DeviceCredential
from app.services.device_service import get_device_row
from app.services.event_service import record_event
from app.services.transaction import transaction

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _expiry(ttl_days: int) -> Optional[datetime]:
    from datetime import timedelta

    if ttl_days <= 0:
        return None
    return _utcnow() + timedelta(days=ttl_days)


def get_active_credential(db: Session, device_id: int) -> Optional[DeviceCredential]:
    return db.execute(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device_id,
            DeviceCredential.status == CredentialStatus.ACTIVE.value,
        )
    ).scalar_one_or_none()


def list_credentials(
    db: Session,
    device_id: int,
    tenant_scope: Optional[Sequence[str]],
) -> list[DeviceCredential]:
    """凭证历史（不含明文）。"""
    get_device_row(db, device_id, tenant_scope, with_status=False)
    return list(
        db.execute(
            select(DeviceCredential)
            .where(DeviceCredential.device_id == device_id)
            .order_by(DeviceCredential.auth_key_version.desc())
        )
        .scalars()
        .all()
    )


def rotate_credential(
    db: Session,
    device_id: int,
    *,
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    ttl_days: Optional[int] = None,
) -> Tuple[DeviceCredential, str]:
    """轮换 AuthKey（README §6 POST /devices/{id}/credential/rotate）。

    返回 (新凭证, 新 AuthKey 明文)。明文仅本次响应返回。
    """
    device = get_device_row(db, device_id, tenant_scope, with_status=False)

    with transaction(db):
        old = get_active_credential(db, device.id)
        revoked_version: Optional[int] = None
        if old is not None:
            old.status = CredentialStatus.REVOKED.value
            old.revoked_at = _utcnow()
            revoked_version = old.auth_key_version
            db.flush()  # 必须先撤销，保证 uk_credential_active 不冲突

        next_version = (
            db.execute(
                select(func.coalesce(func.max(DeviceCredential.auth_key_version), 0)).where(
                    DeviceCredential.device_id == device.id
                )
            ).scalar_one()
            or 0
        ) + 1

        plaintext = generate_auth_key()
        credential = DeviceCredential(
            device_id=device.id,
            auth_key_hash=hash_auth_key(plaintext),
            auth_key_version=next_version,
            status=CredentialStatus.ACTIVE.value,
            issued_at=_utcnow(),
            expires_at=_expiry(ttl_days if ttl_days is not None else _default_ttl()),
        )
        db.add(credential)
        db.flush()

        record_event(
            db,
            device.id,
            EventType.CREDENTIAL_ROTATED,
            operator_id=operator_id,
            event_data={
                "auth_key_version": next_version,
                "revoked_version": revoked_version,
                "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            },
        )

    return credential, plaintext


def _default_ttl() -> int:
    from app.core.config import settings

    return settings.auth_key_default_ttl_days


def revoke_credential(
    db: Session,
    device_id: int,
    *,
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
) -> DeviceCredential:
    """撤销当前有效凭证（不签发新的）。"""
    device = get_device_row(db, device_id, tenant_scope, with_status=False)
    credential = get_active_credential(db, device.id)
    if credential is None:
        raise NotFoundError("该设备当前没有有效凭证")

    with transaction(db):
        credential.status = CredentialStatus.REVOKED.value
        credential.revoked_at = _utcnow()
        db.flush()
        record_event(
            db,
            device.id,
            EventType.CREDENTIAL_REVOKED,
            operator_id=operator_id,
            event_data={"auth_key_version": credential.auth_key_version},
        )
    return credential

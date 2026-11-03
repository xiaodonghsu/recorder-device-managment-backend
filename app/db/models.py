"""数据库模型。

严格对齐 README《嘉环声枢：录音卡设备管理系统开发说明》§4 的 MySQL 建表脚本：

- 主键统一 BIGINT UNSIGNED AUTO_INCREMENT；
- 时间戳统一 DATETIME(3)（MySQL 使用 fsp=3 保留毫秒）；
- 「当前有效记录唯一」约束使用 STORED 生成列 + UNIQUE 索引实现，
  MySQL 8.0.16+ 与 SQLite 3.31+ 均支持，SQLAlchemy 通过 Computed 统一表达；
- device_binding 通过组合外键 (assignment_id, device_id, user_id)
  引用 device_assignment，保证绑定员工与领用员工一致。

数据库方言差异通过 with_variant 处理，业务代码无需感知。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement

# MySQL 使用 BIGINT UNSIGNED / DATETIME(3)；SQLite 的主键需退化为 INTEGER
# （SQLite 仅对 INTEGER PRIMARY KEY 使用 rowid 自增），其余列保持通用类型
BIGINT_PK = (
    BigInteger()
    .with_variant(Integer, "sqlite")
    .with_variant(mysql.BIGINT(unsigned=True), "mysql")
)
TIMESTAMP = DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")
TINYINT = Integer().with_variant(mysql.TINYINT(unsigned=True), "mysql")
# 布尔列：MySQL 用 TINYINT(1)，SQLite 保持 BOOLEAN 以便 ORM 返回 Python bool
BOOL = Boolean().with_variant(mysql.TINYINT(unsigned=True), "mysql")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _utcnow() -> datetime:
    """当前 UTC 时间（ naive，与 MySQL DATETIME 保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _ServerNow(ColumnElement):
    """方言感知的时间戳默认值。

    MySQL 需要 ``CURRENT_TIMESTAMP(3)``（该版本不接受 ``DEFAULT (now())`` 表达式），
    SQLite 只支持无参数的 ``CURRENT_TIMESTAMP``。
    """

    inherit_cache = True
    type = DateTime()


@compiles(_ServerNow)
def _compile_server_now(element, compiler, **kw):  # pragma: no cover - DDL 编译
    if compiler.dialect.name == "mysql":
        return "CURRENT_TIMESTAMP(3)"
    return "CURRENT_TIMESTAMP"


class _ServerNowOnUpdate(ColumnElement):
    """带自动刷新语义的时间戳默认值（对应 MySQL 的 ON UPDATE CURRENT_TIMESTAMP）。"""

    inherit_cache = True
    type = DateTime()


@compiles(_ServerNowOnUpdate)
def _compile_server_now_on_update(element, compiler, **kw):  # pragma: no cover - DDL 编译
    if compiler.dialect.name == "mysql":
        return "CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"
    return "CURRENT_TIMESTAMP"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> Dict[str, Any]:
        return {
            column.name: getattr(self, column.name)
            for column in self.__table__.columns
        }


# ---------------------------------------------------------------------------
# 3.1 device：设备主表
# ---------------------------------------------------------------------------
class Device(Base):
    __tablename__ = "device"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sn: Mapped[str] = mapped_column(String(64), nullable=False)
    bluetooth_name: Mapped[Optional[str]] = mapped_column(String(128))
    model: Mapped[Optional[str]] = mapped_column(String(64))
    manufacturer: Mapped[Optional[str]] = mapped_column(String(100))
    hardware_version: Mapped[Optional[str]] = mapped_column(String(32))
    firmware_version: Mapped[Optional[str]] = mapped_column(String(32))
    production_batch: Mapped[Optional[str]] = mapped_column(String(64))
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="registered", server_default="registered"
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=_ServerNowOnUpdate(),
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("sn", name="uk_device_sn"),
        Index("idx_device_tenant", "tenant_id"),
        Index("idx_device_status", "tenant_id", "lifecycle_status"),
    )

    credential: Mapped[Optional["DeviceCredential"]] = relationship(
        back_populates="device", uselist=False
    )
    status: Mapped[Optional["DeviceStatus"]] = relationship(
        back_populates="device", uselist=False
    )
    assignments: Mapped[List["DeviceAssignment"]] = relationship(
        back_populates="device", order_by="DeviceAssignment.id.desc()"
    )
    # device_binding 通过组合外键指向 device_assignment，不存在指向 device 的直接外键，
    # 因此显式声明连接条件，并标记为只读关系（写入由服务层显式控制）。
    bindings: Mapped[List["DeviceBinding"]] = relationship(
        primaryjoin="Device.id == foreign(DeviceBinding.device_id)",
        foreign_keys="DeviceBinding.device_id",
        viewonly=True,
        order_by="DeviceBinding.id.desc()",
    )
    events: Mapped[List["DeviceEvent"]] = relationship(
        back_populates="device", order_by="DeviceEvent.id.desc()"
    )


# ---------------------------------------------------------------------------
# 3.2 device_credential：设备认证凭证
# ---------------------------------------------------------------------------
class DeviceCredential(Base):
    __tablename__ = "device_credential"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        ForeignKey("device.id", ondelete="CASCADE"),
        nullable=False,
    )
    auth_key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )

    # 生成列：status 为 active 时等于 device_id，否则为 NULL。
    # UNIQUE 索引允许多个 NULL，因此天然保证「每台设备同时最多一个有效凭证」。
    active_device_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        Computed("CASE WHEN status = 'active' THEN device_id ELSE NULL END", persisted=True),
    )

    __table_args__ = (
        UniqueConstraint("device_id", "auth_key_version", name="uk_credential_version"),
        UniqueConstraint("active_device_id", name="uk_credential_active"),
        Index("idx_credential_hash", "auth_key_hash"),
    )

    device: Mapped["Device"] = relationship(back_populates="credential")


# ---------------------------------------------------------------------------
# 3.3 device_assignment：设备领用表
# ---------------------------------------------------------------------------
class DeviceAssignment(Base):
    __tablename__ = "device_assignment"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        ForeignKey("device.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assigned_by: Mapped[Optional[str]] = mapped_column(String(64))
    assigned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )
    returned_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    return_reason: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )

    # 生成列：未归还时等于 device_id，保证「一台设备同时只能分配给一个员工」
    current_device_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        Computed("CASE WHEN returned_at IS NULL THEN device_id ELSE NULL END", persisted=True),
    )

    __table_args__ = (
        UniqueConstraint("current_device_id", name="uk_assignment_current"),
        UniqueConstraint("id", "device_id", "user_id", name="uk_assignment_relation"),
        Index("idx_assignment_user", "tenant_id", "user_id"),
    )

    device: Mapped["Device"] = relationship(
        back_populates="assignments"
    )
    bindings: Mapped[List["DeviceBinding"]] = relationship(
        primaryjoin="foreign(DeviceBinding.assignment_id) == DeviceAssignment.id",
        foreign_keys="DeviceBinding.assignment_id",
        viewonly=True,
        order_by="DeviceBinding.id.desc()",
    )


# ---------------------------------------------------------------------------
# 3.4 device_binding：APP 绑定表
# ---------------------------------------------------------------------------
class DeviceBinding(Base):
    __tablename__ = "device_binding"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False
    )
    assignment_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nickname: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    bound_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )
    unbound_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )

    active_device_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        Computed("CASE WHEN status = 'active' THEN device_id ELSE NULL END", persisted=True),
    )
    active_assignment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        Computed("CASE WHEN status = 'active' THEN assignment_id ELSE NULL END", persisted=True),
    )

    __table_args__ = (
        UniqueConstraint("active_device_id", name="uk_binding_device"),
        UniqueConstraint("active_assignment_id", name="uk_binding_assignment"),
        Index("idx_binding_user", "user_id"),
        # 组合外键：绑定员工必须与领用员工一致（README §4 实现注意 5）
        ForeignKeyConstraint(
            ["assignment_id", "device_id", "user_id"],
            ["device_assignment.id", "device_assignment.device_id", "device_assignment.user_id"],
            name="fk_binding_assignment",
        ),
    )

    device: Mapped["Device"] = relationship(
        primaryjoin="foreign(DeviceBinding.device_id) == Device.id",
        foreign_keys="DeviceBinding.device_id",
        viewonly=True,
    )
    assignment: Mapped["DeviceAssignment"] = relationship(
        primaryjoin="foreign(DeviceBinding.assignment_id) == DeviceAssignment.id",
        foreign_keys="DeviceBinding.assignment_id",
        viewonly=True,
    )


# ---------------------------------------------------------------------------
# 3.5 device_status：设备最新状态
# ---------------------------------------------------------------------------
class DeviceStatus(Base):
    __tablename__ = "device_status"

    device_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        ForeignKey("device.id", ondelete="CASCADE"),
        primary_key=True,
    )
    online: Mapped[bool] = mapped_column(
        BOOL, nullable=False, default=False, server_default=text("0")
    )
    battery_level: Mapped[Optional[int]] = mapped_column(SmallInteger)
    charging: Mapped[bool] = mapped_column(BOOL, nullable=False, default=False, server_default=text("0"))
    recording: Mapped[bool] = mapped_column(BOOL, nullable=False, default=False, server_default=text("0"))
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )

    __table_args__ = (
        CheckConstraint(
            "battery_level IS NULL OR battery_level BETWEEN 0 AND 100",
            name="device_status_battery_range",
        ),
    )

    device: Mapped["Device"] = relationship(back_populates="status")


# ---------------------------------------------------------------------------
# 3.6 device_event：设备事件表
# ---------------------------------------------------------------------------
class DeviceEvent(Base):
    __tablename__ = "device_event"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        ForeignKey("device.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    operator_id: Mapped[Optional[str]] = mapped_column(String(64))
    event_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=_ServerNow()
    )

    __table_args__ = (
        Index("idx_event_device_time", "device_id", "created_at"),
        Index("idx_event_type_time", "event_type", "created_at"),
    )

    device: Mapped["Device"] = relationship(back_populates="events")


__all__ = [
    "Base",
    "Device",
    "DeviceCredential",
    "DeviceAssignment",
    "DeviceBinding",
    "DeviceStatus",
    "DeviceEvent",
]

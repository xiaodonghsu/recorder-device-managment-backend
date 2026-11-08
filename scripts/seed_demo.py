#!/usr/bin/env python
"""演示数据生成脚本。

用于管理后台的本地联调与演示：造出一批设备、领用记录、APP 绑定、
设备凭证、设备状态与生命周期事件，让各表格页面有可浏览的数据。

用法：

    python scripts/seed_demo.py                 # 生成 24 台设备
    python scripts/seed_demo.py --count 50      # 自定义数量
    python scripts/seed_demo.py --reset         # 先清空再生成
    python scripts/seed_demo.py --url mysql+pymysql://...   # 指定数据库

> ⚠️ 仅供开发与演示环境使用，不要在生产库上执行。
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dotenv

dotenv.load_dotenv(dotenv.find_dotenv())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.enums import CredentialStatus, LifecycleStatus  # noqa: E402
from app.core.security import generate_auth_key, hash_auth_key  # noqa: E402
from app.db.models import (  # noqa: E402
    Base,
    Device,
    DeviceAssignment,
    DeviceBinding,
    DeviceCredential,
    DeviceEvent,
    DeviceStatus,
)
from app.db.session import create_engine_for_url, get_engine, init_db  # noqa: E402

# 演示数据的租户跟随 Casdoor 组织配置（CASDOOR_ORGANIZATION）。
# 组织改名后 tenant_id 必须同步，否则播种出来的设备对当前登录租户不可见。
TENANT = settings.casdoor_organization

MODELS = ["VH-1 Pro", "VH-1", "VH-2 Mini", "VH-3 Studio"]
MANUFACTURERS = ["嘉环声枢"]
OPERATORS = ["u-admin", "u-admin", "u-admin", "ops.zhang", "ops.li", "ops.chen"]
EMPLOYEES = [
    "2f1c8a44-1d3e-4c9a-8b77-0a1e2c3d4e5f",
    "3a2b9c55-2e4f-4d0b-9c88-1b2f3d4e5a6b",
    "4b3cad66-3f50-4e1c-8d99-2c3e4f5a6b7c",
    "5c4dbe77-4061-4f2d-9eaa-3d4f5a6b7c8d",
    "6d5ecf88-5172-4a3e-8fbb-4e5a6b7c8d9e",
    "7e6dfa99-6283-4b4f-8acc-5f6b7c8d9e0f",
]
NICKNAMES = ["会议录音笔", "客户拜访", "评审专用", "口袋机", "备用机", None, None]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成录音卡管理后台演示数据")
    parser.add_argument("--count", type=int, default=24, help="设备数量，默认 24")
    parser.add_argument("--reset", action="store_true", help="生成前清空现有数据")
    parser.add_argument("--url", default=None, help="数据库连接串，默认取 DATABASE_URL")
    args = parser.parse_args()

    url = args.url or settings.database_url
    engine = get_engine() if url == settings.database_url else create_engine_for_url(url)
    init_db(engine, drop_first=False)

    with Session(engine) as db:
        if args.reset:
            for model in (
                DeviceEvent,
                DeviceBinding,
                DeviceAssignment,
                DeviceCredential,
                DeviceStatus,
                Device,
            ):
                db.execute(delete(model))
            db.commit()
            print("已清空原有数据。")

        existing = db.execute(select(Device.id)).scalars().all()
        if existing:
            print(f"库中已有 {len(existing)} 台设备；如需重置请加 --reset。")
            return 0

        random.seed(20261104)
        base_time = utcnow() - timedelta(days=90)

        for index in range(1, args.count + 1):
            created_at = base_time + timedelta(hours=random.randint(0, 24 * 80))
            model = random.choice(MODELS)
            device = Device(
                tenant_id=TENANT,
                sn=f"VH-2026-{index:04d}",
                bluetooth_name=f"VoiceHub-{index:04d}",
                model=model,
                manufacturer=random.choice(MANUFACTURERS),
                hardware_version="1.0",
                firmware_version=f"1.{random.randint(0, 4)}.0",
                production_batch=f"B{2026}{index % 6:02d}",
                lifecycle_status=LifecycleStatus.REGISTERED.value,
                created_at=created_at,
                updated_at=created_at,
            )
            db.add(device)
            db.flush()

            # 设备凭证
            credential = DeviceCredential(
                device_id=device.id,
                auth_key_hash=hash_auth_key(generate_auth_key()),
                auth_key_version=1,
                status=CredentialStatus.ACTIVE.value,
                issued_at=created_at,
                created_at=created_at,
            )
            db.add(credential)

            # 设备状态
            battery = random.randint(12, 100)
            online = random.random() > 0.35
            db.add(
                DeviceStatus(
                    device_id=device.id,
                    online=online,
                    battery_level=battery,
                    charging=random.random() > 0.6,
                    recording=online and random.random() > 0.75,
                    last_seen_at=created_at + timedelta(hours=random.randint(1, 72)) if online else None,
                    last_sync_at=created_at + timedelta(hours=random.randint(1, 48)),
                    updated_at=created_at,
                )
            )

            # 生命周期事件：入库
            db.add(
                DeviceEvent(
                    device_id=device.id,
                    event_type="device_registered",
                    operator_id="u-admin",
                    event_data={
                        "sn": device.sn,
                        "tenant_id": TENANT,
                        "model": model,
                        "manufacturer": device.manufacturer,
                        "production_batch": device.production_batch,
                        "auth_key_source": "generated",
                        "auth_key_version": 1,
                    },
                    created_at=created_at,
                )
            )

            roll = random.random()

            # 一部分设备走完整生命周期：分配 → 绑定 → 归还
            if roll < 0.45:
                employee = random.choice(EMPLOYEES)
                assigned_at = created_at + timedelta(hours=random.randint(2, 72))
                returned = random.random() > 0.5

                assignment = DeviceAssignment(
                    device_id=device.id,
                    tenant_id=TENANT,
                    user_id=employee,
                    assigned_by="u-admin",
                    assigned_at=assigned_at,
                    returned_at=assigned_at + timedelta(days=random.randint(3, 40)) if returned else None,
                    return_reason="离职回收" if returned and random.random() > 0.6 else None,
                    created_at=assigned_at,
                )
                db.add(assignment)
                db.flush()

                bound_at = assigned_at + timedelta(hours=random.randint(1, 24))
                db.add(
                    DeviceBinding(
                        device_id=device.id,
                        assignment_id=assignment.id,
                        user_id=employee,
                        nickname=random.choice(NICKNAMES),
                        status="unbound" if returned else "active",
                        bound_at=bound_at,
                        unbound_at=(assignment.returned_at + timedelta(hours=1)) if returned else None,
                        created_at=bound_at,
                    )
                )
                db.add(
                    DeviceEvent(
                        device_id=device.id,
                        event_type="device_assigned",
                        operator_id="u-admin",
                        event_data={"user_id": employee, "device_sn": device.sn},
                        created_at=assigned_at,
                    )
                )
                db.add(
                    DeviceEvent(
                        device_id=device.id,
                        event_type="device_bound",
                        operator_id=employee,
                        event_data={"device_sn": device.sn, "user_id": employee},
                        created_at=bound_at,
                    )
                )

                if returned:
                    device.lifecycle_status = LifecycleStatus.ACTIVE.value
                    device.activated_at = bound_at
                    db.add(
                        DeviceEvent(
                            device_id=device.id,
                            event_type="device_returned",
                            operator_id="u-admin",
                            event_data={"reason": assignment.return_reason or "正常归还"},
                            created_at=assignment.returned_at,
                        )
                    )
                    db.add(
                        DeviceEvent(
                            device_id=device.id,
                            event_type="device_unbound",
                            operator_id="u-admin",
                            event_data={"user_id": employee},
                            created_at=assignment.returned_at + timedelta(hours=1),
                        )
                    )
                else:
                    device.lifecycle_status = LifecycleStatus.ACTIVE.value
                    device.activated_at = bound_at

            # 一部分设备停留在已登记状态（可领用）
            elif roll < 0.8:
                device.lifecycle_status = LifecycleStatus.REGISTERED.value

            # 其余进入维修 / 挂失 / 禁用 / 报废
            else:
                target = random.choice(
                    [LifecycleStatus.REPAIR, LifecycleStatus.LOST, LifecycleStatus.DISABLED, LifecycleStatus.RETIRED]
                )
                device.lifecycle_status = target.value
                event_map = {
                    LifecycleStatus.REPAIR: "device_repaired",
                    LifecycleStatus.LOST: "device_lost",
                    LifecycleStatus.DISABLED: "device_disabled",
                    LifecycleStatus.RETIRED: "device_retired",
                }
                db.add(
                    DeviceEvent(
                        device_id=device.id,
                        event_type=event_map[target],
                        operator_id=random.choice(OPERATORS),
                        event_data={"from": "registered", "to": target.value, "reason": "演示数据"},
                        created_at=created_at + timedelta(hours=48),
                    )
                )

        db.commit()

        total = db.execute(select(Device.id)).scalars().all()
        print(f"演示数据生成完成：{len(total)} 台设备。")
        print("可到管理后台查看：设备 / 领用记录 / APP 绑定 / 设备凭证 / 设备状态 / 事件日志。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

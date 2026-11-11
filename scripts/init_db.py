#!/usr/bin/env python
"""数据库初始化与旧版凭证表升级脚本。

用法：

    # 按 ORM 模型建表（SQLite / MySQL 通用）
    python scripts/init_db.py

    # 先删表再建表（危险，仅限开发环境）
    python scripts/init_db.py --drop

    # 指定数据库连接
    DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/voicehub_device \
        python scripts/init_db.py

MySQL 生产环境也可以直接执行 sql/schema.mysql.sql（两者结构等价）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import dotenv

dotenv.load_dotenv(dotenv.find_dotenv())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import create_engine_for_url, get_engine, init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化录音卡设备管理数据库")
    parser.add_argument("--drop", action="store_true", help="先删除所有表再重建（危险）")
    parser.add_argument("--url", default=None, help="数据库连接串，默认取 DATABASE_URL")
    args = parser.parse_args()

    url = args.url or settings.database_url
    print(f"目标数据库：{url.split('@')[-1]}")

    if "mysql" in url:
        print("提示：MySQL 8.0.16+ 支持 CHECK 约束与生成列索引，版本过低请升级。")

    engine = get_engine() if url == settings.database_url else create_engine_for_url(url)
    init_db(engine, drop_first=args.drop)

    print("表结构已就绪：")
    for table in Base.metadata.sorted_tables:
        print(f"  - {table.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

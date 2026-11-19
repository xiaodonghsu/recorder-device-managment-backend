-- ============================================================================
-- device_status 增量迁移：APP 代报容量 / 固件 / 增益 / 遥测扩展槽
--
-- 适用：已经用旧版 schema.mysql.sql 建过库的生产 MySQL 8.0.16+。
--       新装环境直接用 schema.mysql.sql 即可，无需执行本文件。
--       本地 SQLite dev.db 由 app.db.session.init_db 自动补列，也不用执行本文件。
--
-- 用法（执行前请先备份）：
--   mysql -u root -p voicehub_device < migration_status_ext.sql
--
-- 幂等性：MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS。若部分列已存在，
--         请按实际情况删掉对应的 ADD COLUMN 行后再执行。
-- ============================================================================

ALTER TABLE device_status
    ADD COLUMN storage_free_bytes  BIGINT UNSIGNED NULL
        COMMENT 'APP 实测剩余容量（字节）' AFTER recording,
    ADD COLUMN storage_total_bytes BIGINT UNSIGNED NULL
        COMMENT 'APP 实测总容量（字节）' AFTER storage_free_bytes,
    ADD COLUMN firmware_version   VARCHAR(32) NULL
        COMMENT 'APP 蓝牙实测固件版本，非 device 主表档案值' AFTER storage_total_bytes,
    ADD COLUMN gain               TINYINT UNSIGNED NULL
        COMMENT '录音增益档位 1-3' AFTER firmware_version,
    ADD COLUMN metrics            JSON NULL
        COMMENT '稀疏遥测扩展槽，正式字段不放在这里' AFTER gain,
    ADD CONSTRAINT ck_device_status_storage_free_non_negative
        CHECK (storage_free_bytes IS NULL OR storage_free_bytes >= 0),
    ADD CONSTRAINT ck_device_status_storage_total_non_negative
        CHECK (storage_total_bytes IS NULL OR storage_total_bytes >= 0),
    ADD CONSTRAINT ck_device_status_storage_free_le_total
        CHECK (storage_free_bytes IS NULL OR storage_total_bytes IS NULL
               OR storage_free_bytes <= storage_total_bytes),
    ADD CONSTRAINT ck_device_status_gain_range
        CHECK (gain IS NULL OR gain BETWEEN 1 AND 3);

-- 存量行无需回填：新增列允许 NULL，管理后台会显示「—」，
-- 等 APP 下一次心跳上报后自然有值。

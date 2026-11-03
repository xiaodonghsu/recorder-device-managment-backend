-- ============================================================================
-- 嘉环声枢录音卡设备管理系统 —— MySQL 8.0.16+ 建表脚本
--
-- 与 README §4 的设计一致；相对 README 补充了 ON UPDATE CURRENT_TIMESTAMP(3)
-- 以保证 updated_at 自动刷新。
--
-- 用法：
--   mysql -u root -p < schema.mysql.sql
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `voicehub_device`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_general_ci;

USE `voicehub_device`;

-- ---------------------------------------------------------------------------
-- 3.1 device：设备主表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    sn VARCHAR(64) NOT NULL,
    bluetooth_name VARCHAR(128),
    model VARCHAR(64),
    manufacturer VARCHAR(100),
    hardware_version VARCHAR(32),
    firmware_version VARCHAR(32),
    production_batch VARCHAR(64),

    lifecycle_status VARCHAR(20) NOT NULL DEFAULT 'registered',

    activated_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    UNIQUE KEY uk_device_sn (sn),
    KEY idx_device_tenant (tenant_id),
    KEY idx_device_status (tenant_id, lifecycle_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备基本档案';

-- ---------------------------------------------------------------------------
-- 3.2 device_credential：设备认证凭证
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_credential (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id BIGINT UNSIGNED NOT NULL,
    auth_key_hash VARCHAR(128) NOT NULL,
    auth_key_version INT NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'active',

    issued_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    expires_at DATETIME(3),
    revoked_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    active_device_id BIGINT UNSIGNED
        GENERATED ALWAYS AS (
            CASE WHEN status = 'active' THEN device_id ELSE NULL END
        ) STORED,

    UNIQUE KEY uk_credential_version (device_id, auth_key_version),
    UNIQUE KEY uk_credential_active (active_device_id),
    KEY idx_credential_hash (auth_key_hash),

    CONSTRAINT fk_credential_device
        FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备认证凭证（只存 HMAC 摘要）';

-- ---------------------------------------------------------------------------
-- 3.3 device_assignment：设备领用表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_assignment (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id BIGINT UNSIGNED NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    assigned_by VARCHAR(64),
    assigned_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    returned_at DATETIME(3),
    return_reason VARCHAR(255),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    current_device_id BIGINT UNSIGNED
        GENERATED ALWAYS AS (
            CASE WHEN returned_at IS NULL THEN device_id ELSE NULL END
        ) STORED,

    UNIQUE KEY uk_assignment_current (current_device_id),
    UNIQUE KEY uk_assignment_relation (id, device_id, user_id),
    KEY idx_assignment_user (tenant_id, user_id),

    CONSTRAINT fk_assignment_device
        FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备领用记录';

-- ---------------------------------------------------------------------------
-- 3.4 device_binding：APP 绑定表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_binding (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id BIGINT UNSIGNED NOT NULL,
    assignment_id BIGINT UNSIGNED NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    nickname VARCHAR(64),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    bound_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    unbound_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    active_device_id BIGINT UNSIGNED
        GENERATED ALWAYS AS (
            CASE WHEN status = 'active' THEN device_id ELSE NULL END
        ) STORED,

    active_assignment_id BIGINT UNSIGNED
        GENERATED ALWAYS AS (
            CASE WHEN status = 'active' THEN assignment_id ELSE NULL END
        ) STORED,

    UNIQUE KEY uk_binding_device (active_device_id),
    UNIQUE KEY uk_binding_assignment (active_assignment_id),
    KEY idx_binding_user (user_id),

    CONSTRAINT fk_binding_assignment
        FOREIGN KEY (assignment_id, device_id, user_id)
        REFERENCES device_assignment(id, device_id, user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='APP 绑定关系（含员工自定义昵称）';

-- ---------------------------------------------------------------------------
-- 3.5 device_status：设备最新状态
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_status (
    device_id BIGINT UNSIGNED PRIMARY KEY,
    online TINYINT(1) NOT NULL DEFAULT 0,
    battery_level SMALLINT,
    charging TINYINT(1) NOT NULL DEFAULT 0,
    recording TINYINT(1) NOT NULL DEFAULT 0,
    last_seen_at DATETIME(3),
    last_sync_at DATETIME(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    CONSTRAINT fk_status_device
        FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE CASCADE,

    CONSTRAINT ck_device_status_battery_range
        CHECK (battery_level IS NULL OR battery_level BETWEEN 0 AND 100)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备最新状态';

-- ---------------------------------------------------------------------------
-- 3.6 device_event：设备事件表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_event (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id BIGINT UNSIGNED NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    operator_id VARCHAR(64),
    event_data JSON,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    KEY idx_event_device_time (device_id, created_at),
    KEY idx_event_type_time (event_type, created_at),

    CONSTRAINT fk_event_device
        FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备生命周期事件';

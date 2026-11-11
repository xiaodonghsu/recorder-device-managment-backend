from sqlalchemy import inspect

from app.db.session import init_db


def test_upgrade_legacy_credential_table_preserves_history(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE device_credential DROP COLUMN auth_key")
        conn.exec_driver_sql(
            "ALTER TABLE device_credential ADD COLUMN auth_key_hash VARCHAR(128) NOT NULL"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_credential_hash ON device_credential (auth_key_hash)"
        )
        conn.exec_driver_sql("INSERT INTO device (id, tenant_id, sn) VALUES (1, 'acme', 'SN-OLD')")
        conn.exec_driver_sql(
            "INSERT INTO device_credential (device_id, auth_key_hash, auth_key_version) "
            "VALUES (1, 'legacy-digest', 3)"
        )

    init_db(engine)
    init_db(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("device_credential")}
    assert "auth_key_hash" not in columns
    with engine.begin() as conn:
        assert conn.exec_driver_sql(
            "SELECT device_id, auth_key, auth_key_version, active_device_id FROM device_credential"
        ).one() == (1, None, 3, 1)
        conn.exec_driver_sql("UPDATE device_credential SET status = 'revoked' WHERE device_id = 1")
        conn.exec_driver_sql(
            "INSERT INTO device_credential (device_id, auth_key, auth_key_version) "
            "VALUES (1, 'recording-card-key', 4)"
        )

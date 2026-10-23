from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE source_states ADD COLUMN network_attempts JSON NOT NULL DEFAULT '[]'")
    op.execute(
        "CREATE TABLE registry_snapshots (id VARCHAR NOT NULL PRIMARY KEY, registry_id VARCHAR NOT NULL, digest VARCHAR NOT NULL, source_url VARCHAR NOT NULL, fetched_at VARCHAR NOT NULL, entries JSON NOT NULL, raw_path VARCHAR NOT NULL, UNIQUE(registry_id,digest))"
    )
    op.execute("CREATE INDEX ix_registry_snapshots_registry_id ON registry_snapshots(registry_id)")
    op.execute(
        "CREATE TABLE registry_states (id VARCHAR NOT NULL PRIMARY KEY, active_snapshot_id VARCHAR REFERENCES registry_snapshots(id), status VARCHAR NOT NULL, last_attempt VARCHAR, last_success VARCHAR, last_error VARCHAR NOT NULL)"
    )


def downgrade():
    raise RuntimeError("覆盖底册保留版本历史，不支持有损降级；请恢复备份。")

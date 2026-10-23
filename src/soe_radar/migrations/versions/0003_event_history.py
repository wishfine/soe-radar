"""Keep repeated corrections of one document as separate audit events."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE events_new (id VARCHAR NOT NULL PRIMARY KEY, "
        "opportunity_id VARCHAR NOT NULL REFERENCES opportunities(id), "
        "document_id VARCHAR NOT NULL REFERENCES documents(id), kind VARCHAR NOT NULL, "
        "old_value VARCHAR, new_value VARCHAR, created_at VARCHAR NOT NULL)"
    )
    op.execute(
        "INSERT INTO events_new SELECT id, opportunity_id, document_id, kind, "
        "old_value, new_value, created_at FROM events"
    )
    op.drop_table("events")
    op.rename_table("events_new", "events")


def downgrade():
    raise RuntimeError("审计历史不支持有损降级，请恢复备份。")

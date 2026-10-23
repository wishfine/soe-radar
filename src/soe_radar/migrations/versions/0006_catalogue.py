from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE registry_snapshots ADD COLUMN provenance JSON NOT NULL DEFAULT '{}'")


def downgrade():
    raise RuntimeError("企业底册来源元数据不支持有损降级，请恢复备份。")

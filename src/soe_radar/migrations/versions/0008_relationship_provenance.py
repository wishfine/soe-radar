from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE relationships ADD COLUMN provenance JSON NOT NULL DEFAULT '[]'")


def downgrade():
    raise RuntimeError("关系证据审计不支持有损降级；请恢复备份。")

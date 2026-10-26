from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE coverage_tasks ADD COLUMN owner VARCHAR NOT NULL DEFAULT ''")


def downgrade():
    raise RuntimeError("覆盖任务运行归属保留审计，不支持有损降级；请恢复备份。")

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE source_employer_scopes ADD COLUMN provenance JSON NOT NULL DEFAULT '[]'")


def downgrade():
    raise RuntimeError("共享范围证据审计不支持有损降级；请恢复备份。")

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE documents ADD COLUMN parse_attempt_at VARCHAR")
    op.execute("ALTER TABLE opportunities ADD COLUMN needs_review BOOLEAN NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE opportunities ADD COLUMN merged_into_id VARCHAR REFERENCES opportunities(id)")
    op.execute(
        "CREATE TABLE reviews (id VARCHAR NOT NULL PRIMARY KEY, opportunity_id VARCHAR NOT NULL REFERENCES opportunities(id), document_id VARCHAR REFERENCES documents(id), decision VARCHAR NOT NULL, payload JSON NOT NULL, created_at VARCHAR NOT NULL)"
    )
    op.execute("CREATE INDEX ix_reviews_opportunity_id ON reviews(opportunity_id)")


def downgrade():
    raise RuntimeError("人工审核审计记录不支持有损降级，请恢复备份。")

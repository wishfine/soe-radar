from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE relationships (id VARCHAR NOT NULL PRIMARY KEY, parent_id VARCHAR NOT NULL REFERENCES employers(id), child_id VARCHAR NOT NULL REFERENCES employers(id), relation_type VARCHAR NOT NULL, evidence_url VARCHAR NOT NULL, quote TEXT NOT NULL, evidence_date VARCHAR NOT NULL, status VARCHAR NOT NULL, created_at VARCHAR NOT NULL, UNIQUE(parent_id,child_id))"
    )
    op.execute("CREATE INDEX ix_relationships_parent_id ON relationships(parent_id)")
    op.execute("CREATE INDEX ix_relationships_child_id ON relationships(child_id)")


def downgrade():
    raise RuntimeError("企业隶属关系证据不支持有损降级；请恢复备份。")

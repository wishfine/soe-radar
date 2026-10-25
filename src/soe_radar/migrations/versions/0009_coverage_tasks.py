from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE coverage_tasks (id VARCHAR NOT NULL PRIMARY KEY, task_key VARCHAR NOT NULL UNIQUE, scope_type VARCHAR NOT NULL, scope_id VARCHAR NOT NULL, action VARCHAR NOT NULL, priority VARCHAR NOT NULL, status VARCHAR NOT NULL, attempts INTEGER NOT NULL, last_error VARCHAR NOT NULL, next_action VARCHAR NOT NULL, evidence JSON NOT NULL, period_key VARCHAR NOT NULL, due_at VARCHAR, last_run_at VARCHAR, created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL)"
    )
    op.execute("CREATE INDEX ix_coverage_tasks_status ON coverage_tasks(status)")
    op.execute(
        "CREATE TABLE source_candidates (id VARCHAR NOT NULL PRIMARY KEY, candidate_key VARCHAR NOT NULL UNIQUE, employer_id VARCHAR REFERENCES employers(id), employer_name VARCHAR NOT NULL, url VARCHAR NOT NULL, domain VARCHAR NOT NULL, entry_type VARCHAR NOT NULL, discovery_method VARCHAR NOT NULL, identity_evidence VARCHAR NOT NULL, scope_employer_ids JSON NOT NULL, status VARCHAR NOT NULL, list_result JSON NOT NULL, detail_result JSON NOT NULL, attachment_result JSON NOT NULL, evidence_path VARCHAR NOT NULL, last_checked VARCHAR, created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL)"
    )
    op.execute("CREATE INDEX ix_source_candidates_employer_id ON source_candidates(employer_id)")
    op.execute("CREATE INDEX ix_source_candidates_status ON source_candidates(status)")
    op.execute(
        "CREATE TABLE source_employer_scopes (id VARCHAR NOT NULL PRIMARY KEY, source_id VARCHAR NOT NULL, employer_id VARCHAR NOT NULL REFERENCES employers(id), mode VARCHAR NOT NULL, evidence_url VARCHAR NOT NULL, quote TEXT NOT NULL, cohorts JSON NOT NULL, batch_scope VARCHAR NOT NULL, status VARCHAR NOT NULL, verified_at VARCHAR, UNIQUE(source_id, employer_id, mode, batch_scope))"
    )
    op.execute("CREATE INDEX ix_source_employer_scopes_source_id ON source_employer_scopes(source_id)")
    op.execute("CREATE INDEX ix_source_employer_scopes_employer_id ON source_employer_scopes(employer_id)")


def downgrade():
    raise RuntimeError("覆盖任务和候选来源保留审计，不支持有损降级；请恢复备份。")

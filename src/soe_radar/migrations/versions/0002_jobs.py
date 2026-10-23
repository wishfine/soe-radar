from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE job_postings (\n\tid VARCHAR NOT NULL, \n\topportunity_id VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tlocations JSON NOT NULL, \n\tmajors JSON NOT NULL, \n\teducation JSON NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(opportunity_id) REFERENCES opportunities (id)\n)"
    )
    op.execute("CREATE INDEX ix_job_postings_opportunity_id ON job_postings (opportunity_id)")


def downgrade():
    op.drop_table("job_postings")

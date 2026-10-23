"""Initial SQLite schema, frozen at revision creation."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    'CREATE TABLE discovery_calls (\n\tid VARCHAR NOT NULL, \n\tcreated_at VARCHAR NOT NULL, \n\t"query" VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tPRIMARY KEY (id)\n)',
    "CREATE TABLE employers (\n\tid VARCHAR NOT NULL, \n\tname VARCHAR NOT NULL, \n\taliases JSON NOT NULL, \n\tparent_id VARCHAR, \n\torg_type VARCHAR NOT NULL, \n\tproof_url VARCHAR NOT NULL, \n\tverified_at VARCHAR, \n\tindustry VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (name), \n\tFOREIGN KEY(parent_id) REFERENCES employers (id)\n)",
    "CREATE TABLE leases (\n\tname VARCHAR NOT NULL, \n\towner VARCHAR NOT NULL, \n\texpires_at VARCHAR NOT NULL, \n\tPRIMARY KEY (name)\n)",
    "CREATE TABLE model_calls (\n\tid VARCHAR NOT NULL, \n\tprovider VARCHAR NOT NULL, \n\tmodel VARCHAR NOT NULL, \n\tdigest VARCHAR NOT NULL, \n\tprompt_version VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tcreated_at VARCHAR NOT NULL, \n\tusage JSON NOT NULL, \n\tcost_usd FLOAT NOT NULL, \n\tresult JSON, \n\terror VARCHAR NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_model_calls_digest ON model_calls (digest)",
    "CREATE TABLE provider_states (\n\tname VARCHAR NOT NULL, \n\tpaused_until VARCHAR, \n\treason VARCHAR NOT NULL, \n\tPRIMARY KEY (name)\n)",
    "CREATE TABLE runs (\n\tid VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tsource_ids JSON, \n\tdiscover BOOLEAN NOT NULL, \n\tstarted_at VARCHAR, \n\tfinished_at VARCHAR, \n\tcreated_at VARCHAR NOT NULL, \n\tsummary JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_runs_status ON runs (status)",
    "CREATE TABLE source_states (\n\tid VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tlast_success VARCHAR, \n\tlast_attempt VARCHAR, \n\tlast_error VARCHAR NOT NULL, \n\tfailures INTEGER NOT NULL, \n\tlast_count INTEGER NOT NULL, \n\tpeak_count INTEGER NOT NULL, \n\tcursor JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE activities (\n\tid VARCHAR NOT NULL, \n\temployer_id VARCHAR NOT NULL, \n\tidentity VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tcohorts JSON NOT NULL, \n\tseason VARCHAR NOT NULL, \n\tcreated_at VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(employer_id) REFERENCES employers (id), \n\tUNIQUE (identity)\n)",
    "CREATE TABLE batches (\n\tid VARCHAR NOT NULL, \n\tactivity_id VARCHAR NOT NULL, \n\tname VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (activity_id, name), \n\tFOREIGN KEY(activity_id) REFERENCES activities (id)\n)",
    "CREATE TABLE opportunities (\n\tid VARCHAR NOT NULL, \n\tactivity_id VARCHAR NOT NULL, \n\tbatch_id VARCHAR NOT NULL, \n\temployer_id VARCHAR NOT NULL, \n\tscope VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tofficial_url VARCHAR NOT NULL, \n\tapply_url VARCHAR NOT NULL, \n\tcohorts JSON NOT NULL, \n\tseason VARCHAR NOT NULL, \n\trecruitment_type VARCHAR NOT NULL, \n\tlocations JSON NOT NULL, \n\tmajors JSON NOT NULL, \n\teducation JSON NOT NULL, \n\tpositions JSON NOT NULL, \n\treview_status VARCHAR NOT NULL, \n\tlifecycle VARCHAR NOT NULL, \n\tlatest_deadline_id VARCHAR, \n\textraction JSON NOT NULL, \n\treview_reasons JSON NOT NULL, \n\tpublished_at VARCHAR, \n\tfirst_seen VARCHAR NOT NULL, \n\tlast_checked VARCHAR NOT NULL, \n\tupdated_at VARCHAR NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (batch_id, scope), \n\tFOREIGN KEY(activity_id) REFERENCES activities (id), \n\tFOREIGN KEY(batch_id) REFERENCES batches (id), \n\tFOREIGN KEY(employer_id) REFERENCES employers (id)\n)",
    "CREATE INDEX ix_opportunities_review_status ON opportunities (review_status)",
    "CREATE INDEX ix_opportunities_source_id ON opportunities (source_id)",
    "CREATE TABLE documents (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\turl VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tdigest VARCHAR NOT NULL, \n\ttext TEXT NOT NULL, \n\tpublished_at VARCHAR, \n\tfetched_at VARCHAR NOT NULL, \n\tattachments JSON NOT NULL, \n\tsnapshot_path VARCHAR NOT NULL, \n\textraction JSON, \n\tparser_version VARCHAR NOT NULL, \n\topportunity_id VARCHAR, \n\tparse_state VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (url, digest), \n\tFOREIGN KEY(opportunity_id) REFERENCES opportunities (id)\n)",
    "CREATE INDEX ix_documents_source_id ON documents (source_id)",
    "CREATE INDEX ix_documents_url ON documents (url)",
    "CREATE TABLE personal_states (\n\topportunity_id VARCHAR NOT NULL, \n\tsaved BOOLEAN NOT NULL, \n\tapplied BOOLEAN NOT NULL, \n\tnotes TEXT NOT NULL, \n\tPRIMARY KEY (opportunity_id), \n\tFOREIGN KEY(opportunity_id) REFERENCES opportunities (id)\n)",
    'CREATE TABLE deadlines (\n\tid VARCHAR NOT NULL, \n\topportunity_id VARCHAR NOT NULL, \n\tdocument_id VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tvalue VARCHAR, \n\tquote TEXT NOT NULL, \n\tstart INTEGER NOT NULL, \n\t"end" INTEGER NOT NULL, \n\tscope VARCHAR NOT NULL, \n\tyear_basis VARCHAR NOT NULL, \n\tverified_by VARCHAR NOT NULL, \n\tcreated_at VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(opportunity_id) REFERENCES opportunities (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id)\n)',
    "CREATE TABLE events (\n\tid VARCHAR NOT NULL, \n\topportunity_id VARCHAR NOT NULL, \n\tdocument_id VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\told_value VARCHAR, \n\tnew_value VARCHAR, \n\tcreated_at VARCHAR NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (opportunity_id, document_id, kind), \n\tFOREIGN KEY(opportunity_id) REFERENCES opportunities (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id)\n)",
]


def upgrade():
    for sql in STATEMENTS:
        op.execute(sql)


def downgrade():
    op.drop_table("events")
    op.drop_table("deadlines")
    op.drop_table("personal_states")
    op.drop_table("documents")
    op.drop_table("opportunities")
    op.drop_table("batches")
    op.drop_table("activities")
    op.drop_table("source_states")
    op.drop_table("runs")
    op.drop_table("provider_states")
    op.drop_table("model_calls")
    op.drop_table("leases")
    op.drop_table("employers")
    op.drop_table("discovery_calls")

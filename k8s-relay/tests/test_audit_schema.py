from lease_scheduler.audit import postgres_url
from lease_scheduler.models import Base


def test_audit_schema_keeps_output_and_ownership_normalized():
    tables = Base.metadata.tables
    assert {
        "repositories",
        "identities",
        "git_commits",
        "scripts",
        "scheduled_jobs",
        "script_executions",
        "execution_output",
        "transactions",
    } <= tables.keys()
    assert "line" not in tables["script_executions"].columns
    assert "line" in tables["execution_output"].columns
    assert {
        "author_id",
        "committer_id",
    } <= set(tables["git_commits"].columns.keys())
    assert tables["git_commits"].c.author_id.foreign_keys
    assert tables["git_commits"].c.committer_id.foreign_keys
    assert tables["script_executions"].c.commit_id.foreign_keys
    assert tables["execution_output"].c.execution_id.foreign_keys
    assert tables["transactions"].c.execution_id.foreign_keys


def test_postgres_url_escapes_credentials():
    url = postgres_url("audit-user", "p@ss:/word", "db.internal", 5432, "scheduler")
    assert url.password == "p@ss:/word"
    assert "p%40ss%3A%2Fword" in url.render_as_string(hide_password=False)

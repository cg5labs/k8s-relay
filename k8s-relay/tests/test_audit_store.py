from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lease_scheduler.audit import AuditStore
from lease_scheduler.config import Job
from lease_scheduler.models import (
    ExecutionOutput,
    GitCommit,
    Identity,
    Repository,
    ScriptExecution,
    Transaction,
)


def test_execution_audit_uses_normalized_records(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from lease_scheduler.models import Base

    Base.metadata.create_all(engine)
    store = AuditStore(
        "sqlite+pysqlite:///:memory:",
        "https://example.com/scripts.git",
        "production",
        "pod-a",
        scheduler_id="ops/scheduler",
        engine=engine,
    )
    store._migrated = True
    job = Job(
        name="nightly",
        schedule="0 1 * * *",
        script=Path("/tmp/k8s-deployer-snapshots/abc123/jobs/nightly.sh"),
        script_relative_path="jobs/nightly.sh",
        args=("--safe",),
        timeout=30,
        commit_sha="a" * 40,
        commit_author_name="Author",
        commit_author_email="author@example.com",
        commit_author_timestamp="2026-01-01T01:00:00+00:00",
        commit_committer_name="Committer",
        commit_committer_email="committer@example.com",
        commit_timestamp="2026-01-01T02:00:00+00:00",
    )

    execution_id = store.begin_execution(job)
    store.add_output(execution_id, "stdout", 1, "started")
    store.finish_execution(execution_id, "succeeded", 0)

    with Session(engine) as session:
        execution = session.get(ScriptExecution, execution_id)
        output = session.scalar(
            select(ExecutionOutput).where(ExecutionOutput.execution_id == execution_id)
        )
        transaction = session.scalar(select(Transaction))
        commit = session.scalar(select(GitCommit))
        repository = session.scalar(select(Repository))
        identities = session.scalars(select(Identity)).all()

    assert execution is not None
    assert execution.status == "succeeded"
    assert execution.exit_code == 0
    assert output is not None and output.line == "started"
    assert transaction is not None
    assert transaction.repository_name == "scripts"
    assert transaction.git_commit_sha == "a" * 40
    assert transaction.script_path == "jobs/nightly.sh"
    assert transaction.script_log_output == "stdout: started\n"
    assert (transaction.name, transaction.email) == (
        "Author",
        "author@example.com",
    )
    assert commit is not None and commit.sha == "a" * 40
    assert repository is not None and repository.branch == "production"
    assert {identity.name for identity in identities} == {"Author", "Committer"}

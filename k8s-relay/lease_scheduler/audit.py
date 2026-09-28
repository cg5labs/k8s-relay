"""Persist scheduler execution audits in normalized PostgreSQL tables."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import URL, create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .config import Job
from .models import (
    ExecutionOutput,
    GitCommit,
    Identity,
    Repository,
    ScheduledJob,
    Script,
    ScriptExecution,
    Transaction,
)

log = logging.getLogger("scheduler.audit")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
ALEMBIC_CONFIG = Path(__file__).resolve().parent.parent / "alembic.ini"


class AuditPersistenceError(RuntimeError):
    """Raised when an audit record cannot be persisted."""


def postgres_url(
    username: str, password: str, host: str, port: int, database: str
) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    )


class AuditStore:
    def __init__(
        self,
        database_url: str | URL,
        repository_url: str,
        branch: str,
        pod_identity: str,
        scheduler_id: str = "default",
        engine: Engine | None = None,
    ) -> None:
        self.repository_url = repository_url
        self.branch = branch
        self.pod_identity = pod_identity
        self.scheduler_id = scheduler_id
        self.engine = engine or create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
            connect_args={"connect_timeout": 5},
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._migration_lock = threading.Lock()
        self._migrated = False

    def is_ready(self) -> bool:
        try:
            if not self._migrated:
                with self._migration_lock:
                    if not self._migrated:
                        self._upgrade_schema()
                        self._migrated = True
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, CommandError) as exc:
            log.warning("audit database unavailable", extra={"error": str(exc)})
            return False
        return True

    def begin_execution(self, job: Job) -> int:
        if not self.is_ready():
            raise AuditPersistenceError("audit database is unavailable")
        if not all(
            (
                job.commit_sha,
                job.commit_author_name,
                job.commit_author_timestamp,
                job.commit_committer_name,
                job.commit_timestamp,
            )
        ):
            raise AuditPersistenceError(
                f"job {job.name!r} has no Git revision metadata"
            )

        committed_at = self._timestamp(job.commit_timestamp)
        authored_at = self._timestamp(job.commit_author_timestamp)
        with self._transaction() as session:
            repository = self._get_or_create(
                session,
                Repository,
                {"url": self.repository_url, "branch": self.branch},
                {"created_at": datetime.now(timezone.utc)},
            )
            author = self._get_or_create(
                session,
                Identity,
                {
                    "name": job.commit_author_name,
                    "email": job.commit_author_email or "",
                },
            )
            committer = self._get_or_create(
                session,
                Identity,
                {
                    "name": job.commit_committer_name,
                    "email": job.commit_committer_email or "",
                },
            )
            commit = self._get_or_create(
                session,
                GitCommit,
                {
                    "repository_id": repository.id,
                    "sha": job.commit_sha,
                },
                {
                    "author_id": author.id,
                    "committer_id": committer.id,
                    "authored_at": authored_at,
                    "committed_at": committed_at,
                },
            )
            if job.script_relative_path is None:
                raise AuditPersistenceError(
                    f"job {job.name!r} has no repository-relative script path"
                )
            script_path = job.script_relative_path
            script = self._get_or_create(
                session,
                Script,
                {"repository_id": repository.id, "path": script_path},
            )
            scheduled_job = session.scalar(
                select(ScheduledJob).where(
                    ScheduledJob.scheduler_id == self.scheduler_id,
                    ScheduledJob.name == job.name,
                )
            )
            if scheduled_job is None:
                scheduled_job = ScheduledJob(
                    scheduler_id=self.scheduler_id,
                    name=job.name,
                    schedule=job.schedule,
                    script_id=script.id,
                    args=list(job.args),
                    timeout_seconds=job.timeout,
                )
                session.add(scheduled_job)
                session.flush()
            else:
                scheduled_job.schedule = job.schedule
                scheduled_job.script_id = script.id
                scheduled_job.args = list(job.args)
                scheduled_job.timeout_seconds = job.timeout
            execution = ScriptExecution(
                job_id=scheduled_job.id,
                script_id=script.id,
                commit_id=commit.id,
                pod_identity=self.pod_identity,
                started_at=datetime.now(timezone.utc),
            )
            session.add(execution)
            session.flush()
            session.add(
                Transaction(
                    execution_id=execution.id,
                    repository_name=self._repository_name(self.repository_url),
                    git_commit_sha=job.commit_sha,
                    script_path=script_path,
                    name=job.commit_author_name or "",
                    email=job.commit_author_email or "",
                )
            )
            return execution.id

    def set_process_id(self, execution_id: int, process_id: int) -> None:
        with self._transaction() as session:
            execution = session.get(ScriptExecution, execution_id)
            if execution is not None:
                execution.process_id = process_id

    def add_output(
        self, execution_id: int, stream: str, line_number: int, line: str
    ) -> None:
        with self._transaction() as session:
            session.add(
                ExecutionOutput(
                    execution_id=execution_id,
                    stream=stream,
                    line_number=line_number,
                    captured_at=datetime.now(timezone.utc),
                    line=line,
                )
            )
            session.execute(
                update(Transaction)
                .where(Transaction.execution_id == execution_id)
                .values(
                    script_log_output=(
                        Transaction.script_log_output + f"{stream}: {line}\n"
                    )
                )
            )

    def finish_execution(
        self, execution_id: int, status: str, exit_code: int | None
    ) -> None:
        with self._transaction() as session:
            execution = session.get(ScriptExecution, execution_id)
            if execution is not None:
                execution.finished_at = datetime.now(timezone.utc)
                execution.exit_code = exit_code
                execution.status = status

    def _upgrade_schema(self) -> None:
        with self.engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT pg_advisory_lock(hashtext('k8s-deployer-audit-migrations'))"
            )
            connection.commit()
            try:
                config = Config(str(ALEMBIC_CONFIG))
                config.set_main_option("script_location", str(MIGRATIONS_DIR))
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.commit()
            finally:
                if connection.in_transaction():
                    connection.rollback()
                connection.exec_driver_sql(
                    "SELECT pg_advisory_unlock(hashtext('k8s-deployer-audit-migrations'))"
                )
                connection.commit()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        try:
            with self._sessions.begin() as session:
                yield session
        except SQLAlchemyError as exc:
            raise AuditPersistenceError("could not write audit record") from exc

    @staticmethod
    def _get_or_create(
        session: Session,
        model: type,
        identity: dict,
        defaults: dict | None = None,
    ):
        instance = session.scalar(select(model).filter_by(**identity))
        if instance is None:
            instance = model(**identity, **(defaults or {}))
            session.add(instance)
            session.flush()
        return instance

    @staticmethod
    def _timestamp(value: str | None) -> datetime:
        if value is None:
            raise AuditPersistenceError("Git commit timestamp is missing")
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _repository_name(repository_url: str) -> str:
        path = urlsplit(repository_url).path or repository_url
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return name[:-4] if name.endswith(".git") else name

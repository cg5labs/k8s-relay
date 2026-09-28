"""Normalized relational models for scheduler audit records."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("url", "branch", name="uq_repository_url_branch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("name", "email", name="uq_identity_name_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    email: Mapped[str] = mapped_column(String(512), nullable=False, default="")


class GitCommit(Base):
    __tablename__ = "git_commits"
    __table_args__ = (
        UniqueConstraint("repository_id", "sha", name="uq_git_commit_repository_sha"),
        Index("ix_git_commits_authored_at", "authored_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("identities.id", ondelete="RESTRICT"), nullable=False
    )
    committer_id: Mapped[int] = mapped_column(
        ForeignKey("identities.id", ondelete="RESTRICT"), nullable=False
    )
    authored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Script(Base):
    __tablename__ = "scripts"
    __table_args__ = (
        UniqueConstraint("repository_id", "path", name="uq_script_repository_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        UniqueConstraint(
            "scheduler_id", "name", name="uq_scheduled_job_scheduler_name"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduler_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schedule: Mapped[str] = mapped_column(String(255), nullable=False)
    script_id: Mapped[int] = mapped_column(
        ForeignKey("scripts.id", ondelete="RESTRICT"), nullable=False
    )
    args: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)


class ScriptExecution(Base):
    __tablename__ = "script_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', 'stopped')",
            name="ck_script_execution_status",
        ),
        Index("ix_script_executions_started_at", "started_at"),
        Index("ix_script_executions_job_started", "job_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    script_id: Mapped[int] = mapped_column(
        ForeignKey("scripts.id", ondelete="RESTRICT"), nullable=False
    )
    commit_id: Mapped[int] = mapped_column(
        ForeignKey("git_commits.id", ondelete="RESTRICT"), nullable=False
    )
    pod_identity: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    process_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")


class ExecutionOutput(Base):
    __tablename__ = "execution_output"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "stream",
            "line_number",
            name="uq_output_execution_stream_line",
        ),
        CheckConstraint("stream IN ('stdout', 'stderr')", name="ck_output_stream"),
        Index("ix_execution_output_execution", "execution_id", "line_number"),
        Index("ix_execution_output_captured_at", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("script_executions.id", ondelete="CASCADE"), nullable=False
    )
    stream: Mapped[str] = mapped_column(String(6), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    line: Mapped[str] = mapped_column(Text, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_transaction_execution"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("script_executions.id", ondelete="CASCADE"), nullable=False
    )
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    git_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    script_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    script_log_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    email: Mapped[str] = mapped_column(String(512), nullable=False)

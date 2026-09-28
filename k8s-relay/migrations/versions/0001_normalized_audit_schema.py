"""Create normalized scheduler audit schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_normalized_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("email", sa.String(length=512), nullable=False),
        sa.UniqueConstraint("name", "email", name="uq_identity_name_email"),
    )
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("url", "branch", name="uq_repository_url_branch"),
    )
    op.create_table(
        "scripts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer(),
            sa.ForeignKey("repositories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.UniqueConstraint("repository_id", "path", name="uq_script_repository_path"),
    )
    op.create_table(
        "git_commits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer(),
            sa.ForeignKey("repositories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sha", sa.String(length=64), nullable=False),
        sa.Column(
            "author_id",
            sa.Integer(),
            sa.ForeignKey("identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "committer_id",
            sa.Integer(),
            sa.ForeignKey("identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("authored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "repository_id", "sha", name="uq_git_commit_repository_sha"
        ),
    )
    op.create_index("ix_git_commits_authored_at", "git_commits", ["authored_at"])
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scheduler_id", sa.String(length=512), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("schedule", sa.String(length=255), nullable=False),
        sa.Column(
            "script_id",
            sa.Integer(),
            sa.ForeignKey("scripts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "scheduler_id", "name", name="uq_scheduled_job_scheduler_name"
        ),
    )
    op.create_table(
        "script_executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("scheduled_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "script_id",
            sa.Integer(),
            sa.ForeignKey("scripts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "commit_id",
            sa.Integer(),
            sa.ForeignKey("git_commits.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("pod_identity", sa.String(length=255), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', 'stopped')",
            name="ck_script_execution_status",
        ),
    )
    op.create_index(
        "ix_script_executions_started_at", "script_executions", ["started_at"]
    )
    op.create_index(
        "ix_script_executions_job_started",
        "script_executions",
        ["job_id", "started_at"],
    )
    op.create_table(
        "execution_output",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.Integer(),
            sa.ForeignKey("script_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stream", sa.String(length=6), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.CheckConstraint("stream IN ('stdout', 'stderr')", name="ck_output_stream"),
        sa.UniqueConstraint(
            "execution_id",
            "stream",
            "line_number",
            name="uq_output_execution_stream_line",
        ),
    )
    op.create_index(
        "ix_execution_output_execution",
        "execution_output",
        ["execution_id", "line_number"],
    )
    op.create_index(
        "ix_execution_output_captured_at", "execution_output", ["captured_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_output_captured_at", table_name="execution_output")
    op.drop_index("ix_execution_output_execution", table_name="execution_output")
    op.drop_table("execution_output")
    op.drop_index("ix_script_executions_job_started", table_name="script_executions")
    op.drop_index("ix_script_executions_started_at", table_name="script_executions")
    op.drop_table("script_executions")
    op.drop_table("scheduled_jobs")
    op.drop_index("ix_git_commits_authored_at", table_name="git_commits")
    op.drop_table("git_commits")
    op.drop_table("scripts")
    op.drop_table("repositories")
    op.drop_table("identities")

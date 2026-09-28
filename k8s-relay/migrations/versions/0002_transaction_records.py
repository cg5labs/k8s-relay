"""Add consolidated transaction records for script executions."""

from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa

revision = "0002_transaction_records"
down_revision = "0001_normalized_audit"
branch_labels = None
depends_on = None


def _repository_name(repository_url: str) -> str:
    path = urlsplit(repository_url).path or repository_url
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def upgrade() -> None:
    transactions = op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.Integer(),
            sa.ForeignKey("script_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repository_name", sa.String(length=255), nullable=False),
        sa.Column("git_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("script_path", sa.String(length=1024), nullable=False),
        sa.Column("script_log_output", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("email", sa.String(length=512), nullable=False),
        sa.UniqueConstraint("execution_id", name="uq_transaction_execution"),
    )

    connection = op.get_bind()
    executions = connection.execute(sa.text("""
            SELECT e.id, r.url, c.sha, s.path, i.name, i.email
            FROM script_executions AS e
            JOIN scripts AS s ON s.id = e.script_id
            JOIN repositories AS r ON r.id = s.repository_id
            JOIN git_commits AS c ON c.id = e.commit_id
            JOIN identities AS i ON i.id = c.author_id
            ORDER BY e.id
            """)).mappings().all()
    output: dict[int, list[str]] = {}
    for row in connection.execute(sa.text("""
            SELECT execution_id, stream, line
            FROM execution_output
            ORDER BY execution_id, captured_at, id
            """)).mappings():
        output.setdefault(row["execution_id"], []).append(
            f'{row["stream"]}: {row["line"]}\n'
        )

    transaction_rows = [
        {
            "execution_id": row["id"],
            "repository_name": _repository_name(row["url"]),
            "git_commit_sha": row["sha"],
            "script_path": row["path"],
            "script_log_output": "".join(output.get(row["id"], [])),
            "name": row["name"],
            "email": row["email"],
        }
        for row in executions
    ]
    if transaction_rows:
        connection.execute(transactions.insert(), transaction_rows)


def downgrade() -> None:
    op.drop_table("transactions")

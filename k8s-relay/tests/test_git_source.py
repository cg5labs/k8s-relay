import subprocess
from pathlib import Path

import pytest

from lease_scheduler.config import Job
from lease_scheduler.git_source import GitScriptRepository, GitSourceError


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Script Author")
    git(path, "config", "user.email", "author@example.com")
    (path / "hello.sh").write_text("echo first\n")
    git(path, "add", "hello.sh")
    git(path, "commit", "-m", "initial")
    return path


def test_clone_pull_and_snapshot_script_per_commit(tmp_path):
    source = make_repository(tmp_path / "source")
    checkout = tmp_path / "checkout"
    repository = GitScriptRepository(
        str(source), "main", checkout, snapshots_dir=tmp_path / "snapshots"
    )
    repository.initialize()
    job = Job("hello", "* * * * *", checkout / "hello.sh")

    initial = repository.prepare(job)
    assert initial.script.read_text() == "echo first\n"
    assert initial.working_directory == initial.script.parent
    assert initial.commit_sha == git(source, "rev-parse", "HEAD")
    assert initial.commit_author_name == "Script Author"
    assert initial.commit_author_email == "author@example.com"
    assert initial.script_relative_path == "hello.sh"

    (source / "hello.sh").write_text("echo second\n")
    git(source, "add", "hello.sh")
    git(source, "commit", "-m", "update script")
    updated = repository.prepare(job)
    assert updated.script.read_text() == "echo second\n"
    assert updated.commit_sha != initial.commit_sha
    assert not initial.script.exists()


def test_pull_failure_raises_for_scheduler_to_skip_run(tmp_path):
    source = make_repository(tmp_path / "source")
    checkout = tmp_path / "checkout"
    repository = GitScriptRepository(
        str(source), "main", checkout, snapshots_dir=tmp_path / "snapshots"
    )
    repository.initialize()
    git(checkout, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    with pytest.raises(GitSourceError, match="git command failed"):
        repository.prepare(Job("hello", "* * * * *", checkout / "hello.sh"))

"""Git-backed source for scheduled scripts."""

from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from .config import Job


class GitSourceError(RuntimeError):
    """Raised when the configured script repository cannot be prepared."""


class GitScriptRepository:
    def __init__(
        self,
        repository: str,
        branch: str,
        checkout_dir: Path,
        username: str | None = None,
        token: str | None = None,
        snapshots_dir: Path = Path("/tmp/k8s-deployer-snapshots"),
    ) -> None:
        self.repository = repository
        self.branch = branch
        self.checkout_dir = checkout_dir
        self.username = username
        self.token = token
        self.snapshots_dir = snapshots_dir

    def initialize(self) -> None:
        self._git(["check-ref-format", "--branch", self.branch], cwd=None)
        try:
            self.checkout_dir.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GitSourceError(
                f"could not prepare checkout directory {self.checkout_dir.parent}: {exc}"
            ) from exc
        if (self.checkout_dir / ".git").is_dir():
            current = self._git(
                ["remote", "get-url", "origin"], cwd=self.checkout_dir
            ).strip()
            if current != self.repository:
                raise GitSourceError(
                    f"existing checkout at {self.checkout_dir} uses a different repository"
                )
            self._git(["checkout", "--force", self.branch], cwd=self.checkout_dir)
        else:
            self._git(
                [
                    "clone",
                    "--branch",
                    self.branch,
                    "--single-branch",
                    "--",
                    self.repository,
                    str(self.checkout_dir),
                ],
                cwd=None,
            )

    def prepare(self, job: Job) -> Job:
        """Pull the configured branch and return an immutable script snapshot."""
        self._git(["pull", "--ff-only", "origin", self.branch], cwd=self.checkout_dir)
        commit_values = (
            self._git(
                [
                    "log",
                    "-1",
                    "--format=%H%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
                ],
                cwd=self.checkout_dir,
            )
            .strip()
            .split("\x00")
        )
        if len(commit_values) != 7:
            raise GitSourceError("could not read the latest commit metadata")

        (
            commit_sha,
            author_name,
            author_email,
            author_timestamp,
            committer_name,
            committer_email,
            committer_timestamp,
        ) = commit_values
        try:
            relative_script = job.script.relative_to(self.checkout_dir)
        except ValueError as exc:
            raise GitSourceError(
                f"job {job.name!r} script is outside the Git checkout"
            ) from exc
        job_snapshot_dir = (
            self.snapshots_dir / hashlib.sha256(job.name.encode()).hexdigest()
        )
        snapshot = job_snapshot_dir / commit_sha
        try:
            if job_snapshot_dir.exists():
                shutil.rmtree(job_snapshot_dir)
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                self.checkout_dir,
                snapshot,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        except OSError as exc:
            raise GitSourceError(f"could not create script snapshot: {exc}") from exc
        script = snapshot / relative_script
        if not script.is_file():
            raise GitSourceError(
                f"job {job.name!r} script {relative_script} is missing from commit {commit_sha}"
            )
        return replace(
            job,
            script=script,
            script_relative_path=str(relative_script),
            commit_sha=commit_sha,
            commit_author_name=author_name,
            commit_author_email=author_email,
            commit_author_timestamp=author_timestamp,
            commit_committer_name=committer_name,
            commit_committer_email=committer_email,
            commit_timestamp=committer_timestamp,
            working_directory=snapshot,
        )

    def _git(self, args: list[str], cwd: Path | None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        askpass_dir: tempfile.TemporaryDirectory | None = None
        if self.username is not None and self.token is not None:
            askpass_dir = tempfile.TemporaryDirectory(prefix="git-askpass-")
            askpass = Path(askpass_dir.name) / "askpass"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *Username*) printf "%s\\n" "$GIT_USERNAME" ;;\n'
                '  *Password*) printf "%s\\n" "$GIT_TOKEN" ;;\n'
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            askpass.chmod(0o700)
            env.update(
                {
                    "GIT_ASKPASS": str(askpass),
                    "GIT_USERNAME": self.username,
                    "GIT_TOKEN": self.token,
                }
            )
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={self.checkout_dir}",
                    *args,
                ],
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitSourceError(f"git command failed: {exc}") from exc
        finally:
            if askpass_dir is not None:
                askpass_dir.cleanup()
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitSourceError(f"git command failed: {detail}")
        return result.stdout

"""Settings from the environment and job definitions from YAML."""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter

SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


class ConfigError(ValueError):
    """Raised when settings or job definitions are invalid."""


@dataclass(frozen=True)
class Job:
    name: str
    schedule: str
    script: Path
    args: tuple[str, ...] = ()
    timeout: float | None = None
    commit_sha: str | None = None
    commit_author_name: str | None = None
    commit_author_email: str | None = None
    commit_author_timestamp: str | None = None
    commit_committer_name: str | None = None
    commit_committer_email: str | None = None
    commit_timestamp: str | None = None
    working_directory: Path | None = None
    script_relative_path: str | None = None


@dataclass(frozen=True)
class JobsConfig:
    timezone: ZoneInfo
    jobs: tuple[Job, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Settings:
    jobs_file: Path
    scripts_dir: Path
    lease_name: str
    namespace: str
    identity: str
    lease_duration: int
    renew_deadline: int
    retry_period: int
    kill_grace_seconds: float
    health_port: int
    log_level: str
    git_repository: str = ""
    git_branch: str = "main"
    git_username: str | None = None
    git_token: str | None = None
    audit_enabled: bool = False
    db_host: str = ""
    db_port: int = 5432
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""


def _int_env(env: dict, name: str, default: int) -> int:
    raw = env.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float_env(env: dict, name: str, default: float) -> float:
    raw = env.get(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _default_namespace() -> str:
    try:
        return SA_NAMESPACE_FILE.read_text().strip() or "default"
    except OSError:
        return "default"


def load_settings(env: dict | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    identity = env.get("POD_NAME") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    git_repository = env.get("GIT_REPOSITORY", "").strip()
    repository_parts = urlsplit(git_repository)
    if repository_parts.scheme in {"http", "https"} and (
        repository_parts.username is not None or repository_parts.password is not None
    ):
        raise ConfigError("GIT_REPOSITORY must not contain embedded credentials")
    git_username = env.get("GIT_USERNAME") or None
    git_token = env.get("GIT_TOKEN") or None
    audit_raw = env.get("AUDIT_ENABLED", "false").lower()
    if audit_raw not in {"true", "false"}:
        raise ConfigError("AUDIT_ENABLED must be 'true' or 'false'")
    audit_enabled = audit_raw == "true"
    if not git_repository:
        raise ConfigError("GIT_REPOSITORY must be set")
    if (git_username is None) != (git_token is None):
        raise ConfigError("GIT_USERNAME and GIT_TOKEN must be set together")
    database_values = {
        "DB_HOST": env.get("DB_HOST", "").strip(),
        "DB_NAME": env.get("DB_NAME", "").strip(),
        "DB_USER": env.get("DB_USER", "").strip(),
        "DB_PASSWORD": env.get("DB_PASSWORD", ""),
    }
    if audit_enabled and any(not value for value in database_values.values()):
        raise ConfigError(
            "DB_HOST, DB_NAME, DB_USER, and DB_PASSWORD are required when audit is enabled"
        )
    settings = Settings(
        jobs_file=Path(env.get("JOBS_FILE", "/etc/scheduler/jobs.yaml")),
        scripts_dir=Path(env.get("SCRIPTS_DIR", "/opt/scripts")),
        git_repository=git_repository,
        git_branch=env.get("GIT_BRANCH", "main"),
        git_username=git_username,
        git_token=git_token,
        audit_enabled=audit_enabled,
        db_host=database_values["DB_HOST"],
        db_port=_int_env(env, "DB_PORT", 5432),
        db_name=database_values["DB_NAME"],
        db_user=database_values["DB_USER"],
        db_password=database_values["DB_PASSWORD"],
        lease_name=env.get("LEASE_NAME", "k8s-deployer"),
        namespace=env.get("POD_NAMESPACE") or _default_namespace(),
        identity=identity,
        lease_duration=_int_env(env, "LEASE_DURATION", 17),
        renew_deadline=_int_env(env, "RENEW_DEADLINE", 15),
        retry_period=_int_env(env, "RETRY_PERIOD", 5),
        kill_grace_seconds=_float_env(env, "KILL_GRACE_SECONDS", 10.0),
        health_port=_int_env(env, "HEALTH_PORT", 8080),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )
    # Mirror the upstream electionconfig checks, which call sys.exit() instead.
    if settings.lease_duration <= settings.renew_deadline:
        raise ConfigError("LEASE_DURATION must be greater than RENEW_DEADLINE")
    if settings.renew_deadline <= 1.2 * settings.retry_period:
        raise ConfigError("RENEW_DEADLINE must be greater than 1.2 * RETRY_PERIOD")
    if settings.retry_period < 1:
        raise ConfigError("RETRY_PERIOD must be at least 1")
    if settings.kill_grace_seconds < 0:
        raise ConfigError("KILL_GRACE_SECONDS must not be negative")
    return settings


def _resolve_script(scripts_dir: Path, script: str, job_name: str) -> Path:
    # Keep configured paths lexically inside the repository checkout.
    base = Path(os.path.abspath(scripts_dir))
    path = Path(os.path.normpath(base / script))
    if base not in path.parents:
        raise ConfigError(f"job {job_name!r}: script {script!r} escapes {base}")
    if not path.is_file():
        raise ConfigError(f"job {job_name!r}: script {path} not found")
    return path


def parse_jobs(data: object, scripts_dir: Path) -> JobsConfig:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("jobs file must be a mapping")

    tz_name = data.get("timezone", "UTC")
    try:
        tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone {tz_name!r}") from exc

    raw_jobs = data.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raise ConfigError("'jobs' must be a list")

    jobs: list[Job] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_jobs):
        if not isinstance(raw, dict):
            raise ConfigError(f"jobs[{index}] must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"jobs[{index}]: 'name' must be a non-empty string")
        if name in seen:
            raise ConfigError(f"duplicate job name {name!r}")
        seen.add(name)

        schedule = raw.get("schedule")
        if not isinstance(schedule, str) or not croniter.is_valid(schedule):
            raise ConfigError(f"job {name!r}: invalid cron schedule {schedule!r}")

        script = raw.get("script")
        if not isinstance(script, str) or not script:
            raise ConfigError(f"job {name!r}: 'script' must be a non-empty string")

        args = raw.get("args") or []
        if not isinstance(args, list):
            raise ConfigError(f"job {name!r}: 'args' must be a list")

        timeout = raw.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ConfigError(f"job {name!r}: 'timeout' must be a number")
            if timeout <= 0:
                raise ConfigError(f"job {name!r}: 'timeout' must be positive")
            timeout = float(timeout)

        jobs.append(
            Job(
                name=name,
                schedule=schedule,
                script=_resolve_script(scripts_dir, script, name),
                script_relative_path=script,
                args=tuple(str(a) for a in args),
                timeout=timeout,
            )
        )
    return JobsConfig(timezone=tz, jobs=tuple(jobs))


def load_jobs(jobs_file: Path, scripts_dir: Path) -> JobsConfig:
    try:
        text = jobs_file.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read jobs file {jobs_file}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {jobs_file}: {exc}") from exc
    return parse_jobs(data, scripts_dir)

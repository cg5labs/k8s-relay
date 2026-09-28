from pathlib import Path

import pytest

from lease_scheduler.config import ConfigError, load_jobs, load_settings, parse_jobs


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    (tmp_path / "hello.sh").write_text("echo hi\n")
    return tmp_path


def test_parse_jobs_valid(scripts):
    cfg = parse_jobs(
        {
            "timezone": "Asia/Singapore",
            "jobs": [
                {
                    "name": "hello",
                    "schedule": "*/5 * * * *",
                    "script": "hello.sh",
                    "args": ["a", 1],
                    "timeout": 30,
                }
            ],
        },
        scripts,
    )
    assert str(cfg.timezone) == "Asia/Singapore"
    job = cfg.jobs[0]
    assert job.script == scripts / "hello.sh"
    assert job.args == ("a", "1")
    assert job.timeout == 30.0


def test_defaults_to_utc_and_no_jobs(scripts):
    cfg = parse_jobs(None, scripts)
    assert str(cfg.timezone) == "UTC"
    assert cfg.jobs == ()


@pytest.mark.parametrize(
    "job, message",
    [
        ({"schedule": "* * * * *", "script": "hello.sh"}, "'name'"),
        ({"name": "x", "schedule": "nope", "script": "hello.sh"}, "invalid cron"),
        ({"name": "x", "schedule": "* * * * *", "script": "missing.sh"}, "not found"),
        ({"name": "x", "schedule": "* * * * *", "script": "../etc/passwd"}, "escapes"),
        (
            {"name": "x", "schedule": "* * * * *", "script": "hello.sh", "timeout": 0},
            "positive",
        ),
        (
            {"name": "x", "schedule": "* * * * *", "script": "hello.sh", "args": "a"},
            "'args'",
        ),
    ],
)
def test_parse_jobs_rejects_invalid(scripts, job, message):
    with pytest.raises(ConfigError, match=message):
        parse_jobs({"jobs": [job]}, scripts)


def test_duplicate_names_rejected(scripts):
    job = {"name": "x", "schedule": "* * * * *", "script": "hello.sh"}
    with pytest.raises(ConfigError, match="duplicate"):
        parse_jobs({"jobs": [job, job]}, scripts)


def test_unknown_timezone_rejected(scripts):
    with pytest.raises(ConfigError, match="timezone"):
        parse_jobs({"timezone": "Mars/Olympus"}, scripts)


def test_load_jobs_reads_yaml(tmp_path, scripts):
    jobs_file = tmp_path / "jobs.yaml"
    jobs_file.write_text(
        "jobs:\n  - name: hello\n    schedule: '@hourly'\n    script: hello.sh\n"
    )
    assert load_jobs(jobs_file, scripts).jobs[0].name == "hello"


def test_load_jobs_bad_yaml(tmp_path, scripts):
    jobs_file = tmp_path / "jobs.yaml"
    jobs_file.write_text("jobs: [\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_jobs(jobs_file, scripts)


def test_load_settings_from_env():
    s = load_settings(
        {
            "POD_NAME": "pod-a",
            "POD_NAMESPACE": "ops",
            "LEASE_NAME": "sched",
            "GIT_REPOSITORY": "https://example.com/scripts.git",
            "GIT_BRANCH": "release",
        }
    )
    assert (s.identity, s.namespace, s.lease_name) == ("pod-a", "ops", "sched")
    assert (s.lease_duration, s.renew_deadline, s.retry_period) == (17, 15, 5)
    assert s.git_repository == "https://example.com/scripts.git"
    assert s.git_branch == "release"
    assert not s.audit_enabled


def test_load_settings_rejects_incomplete_git_credentials():
    with pytest.raises(ConfigError, match="set together"):
        load_settings(
            {
                "GIT_REPOSITORY": "https://example.com/scripts.git",
                "GIT_USERNAME": "user",
            }
        )


def test_load_settings_requires_database_configuration_when_audit_enabled():
    with pytest.raises(ConfigError, match="DB_HOST"):
        load_settings(
            {
                "GIT_REPOSITORY": "https://example.com/scripts.git",
                "AUDIT_ENABLED": "true",
            }
        )


@pytest.mark.parametrize(
    "env, message",
    [
        ({"LEASE_DURATION": "10", "RENEW_DEADLINE": "10"}, "LEASE_DURATION"),
        ({"RENEW_DEADLINE": "6", "RETRY_PERIOD": "5"}, "RENEW_DEADLINE"),
        ({"RETRY_PERIOD": "x"}, "integer"),
    ],
)
def test_load_settings_rejects_bad_timings(env, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(
            {
                "POD_NAMESPACE": "ops",
                "GIT_REPOSITORY": "https://example.com/scripts.git",
                **env,
            }
        )

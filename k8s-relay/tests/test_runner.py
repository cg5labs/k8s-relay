import logging
import sys
import time
from pathlib import Path

import pytest

from lease_scheduler.config import Job
from lease_scheduler.audit import AuditPersistenceError
from lease_scheduler.runner import ScriptRunner, command_for


def write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_command_for(tmp_path):
    py = write(tmp_path / "a.py", "print(1)\n")
    sh = write(tmp_path / "b.sh", "echo 1\n")
    exe = write(tmp_path / "c", "#!/bin/sh\necho 1\n", 0o755)
    assert command_for(py, ("x",)) == [sys.executable, str(py), "x"]
    assert command_for(sh) == ["/bin/sh", str(sh)]
    assert command_for(exe) == [str(exe)]


def test_runs_script_and_logs_output(tmp_path, caplog):
    script = write(tmp_path / "hi.sh", 'echo "hello $1 $JOB_NAME"\necho oops >&2\n')
    runner = ScriptRunner()
    caplog.set_level(logging.INFO, logger="scheduler.runner")
    job = Job(
        "hi",
        "* * * * *",
        script,
        ("world",),
        commit_author_email="author@example.com",
    )
    assert runner.start(job)
    assert wait_until(lambda: not runner.is_running("hi"))
    messages = [(r.getMessage(), getattr(r, "stream", None)) for r in caplog.records]
    assert ("hello world hi", "stdout") in messages
    assert ("oops", "stderr") in messages
    started = [r for r in caplog.records if r.getMessage() == "job started"]
    assert started and started[0].commit_author_email == "author@example.com"
    finished = [r for r in caplog.records if r.getMessage() == "job finished"]
    assert finished and finished[0].exit_code == 0


def test_refuses_overlapping_start(tmp_path):
    script = write(tmp_path / "slow.sh", "sleep 5\n")
    runner = ScriptRunner(kill_grace_seconds=1)
    job = Job("slow", "* * * * *", script)
    assert runner.start(job)
    assert not runner.start(job)
    runner.terminate_all()
    assert not runner.is_running("slow")


def test_terminate_escalates_to_sigkill(tmp_path, caplog):
    script = write(
        tmp_path / "stubborn.sh", "trap '' TERM\nwhile :; do sleep 0.1; done\n"
    )
    runner = ScriptRunner(kill_grace_seconds=0.5)
    runner.start(Job("stubborn", "* * * * *", script))
    time.sleep(0.3)
    caplog.set_level(logging.WARNING, logger="scheduler.runner")
    started = time.monotonic()
    runner.terminate_all()
    assert time.monotonic() - started < 5
    assert not runner.is_running("stubborn")
    assert any("SIGKILL" in r.getMessage() for r in caplog.records)


def test_timeout_stops_job(tmp_path, caplog):
    script = write(tmp_path / "long.sh", "sleep 10\n")
    runner = ScriptRunner(kill_grace_seconds=1)
    caplog.set_level(logging.WARNING, logger="scheduler.runner")
    runner.start(Job("long", "* * * * *", script, timeout=0.3))
    assert wait_until(lambda: not runner.is_running("long"))
    assert any(r.getMessage() == "job timed out" for r in caplog.records)


@pytest.mark.parametrize("code", [3])
def test_nonzero_exit_logged_as_error(tmp_path, caplog, code):
    script = write(tmp_path / "fail.sh", f"exit {code}\n")
    runner = ScriptRunner()
    caplog.set_level(logging.INFO, logger="scheduler.runner")
    runner.start(Job("fail", "* * * * *", script))
    assert wait_until(lambda: not runner.is_running("fail"))
    failed = [r for r in caplog.records if r.getMessage() == "job failed"]
    assert failed and failed[0].exit_code == code


class FakeAuditStore:
    def __init__(self):
        self.outputs = []
        self.finished = []

    def begin_execution(self, job):
        return 27

    def set_process_id(self, execution_id, process_id):
        pass

    def add_output(self, execution_id, stream, line_number, line):
        self.outputs.append((execution_id, stream, line_number, line))

    def finish_execution(self, execution_id, status, exit_code):
        self.finished.append((execution_id, status, exit_code))


def test_persists_script_output_and_outcome(tmp_path):
    script = write(tmp_path / "audited.sh", "echo audit-line\n")
    store = FakeAuditStore()
    runner = ScriptRunner(audit_store=store)
    assert runner.start(Job("audited", "* * * * *", script))
    assert wait_until(lambda: not runner.is_running("audited"))
    assert store.outputs == [(27, "stdout", 1, "audit-line")]
    assert store.finished == [(27, "succeeded", 0)]


def test_skips_job_when_audit_store_is_unavailable(tmp_path, caplog):
    class UnavailableAuditStore(FakeAuditStore):
        def begin_execution(self, job):
            raise AuditPersistenceError("database unavailable")

    script = write(tmp_path / "not-run.sh", "echo must-not-run\n")
    outcomes = []
    runner = ScriptRunner(
        audit_store=UnavailableAuditStore(),
        on_outcome=lambda name, result: outcomes.append((name, result)),
        popen=lambda *args, **kwargs: pytest.fail("script started without audit row"),
    )
    caplog.set_level(logging.ERROR, logger="scheduler.runner")
    assert not runner.start(Job("not-run", "* * * * *", script))
    assert outcomes == [("not-run", "skipped")]
    assert any(
        record.getMessage() == "job skipped, audit persistence unavailable"
        for record in caplog.records
    )


def test_secret_credentials_are_not_inherited_by_script(tmp_path, monkeypatch, caplog):
    script = write(
        tmp_path / "secret-check.sh",
        'printf "%s %s %s\\n" "${GIT_TOKEN-unset}" "${GIT_USERNAME-unset}" "${DB_PASSWORD-unset}"\n',
    )
    monkeypatch.setenv("GIT_TOKEN", "git-secret")
    monkeypatch.setenv("GIT_USERNAME", "git-user")
    monkeypatch.setenv("DB_PASSWORD", "db-secret")
    caplog.set_level(logging.INFO, logger="scheduler.runner")
    runner = ScriptRunner()
    assert runner.start(Job("secret-check", "* * * * *", script))
    assert wait_until(lambda: not runner.is_running("secret-check"))
    output = [record.getMessage() for record in caplog.records]
    assert "unset unset unset" in output

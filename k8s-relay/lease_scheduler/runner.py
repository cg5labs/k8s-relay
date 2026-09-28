"""Run job scripts as subprocesses and stop them on demand."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable

from .audit import AuditPersistenceError, AuditStore
from .config import Job

log = logging.getLogger("scheduler.runner")


def command_for(script: Path, args: tuple[str, ...] = ()) -> list[str]:
    """Build argv: honour a shebang on executables, else pick by extension."""
    path = str(script)
    try:
        with open(script, "rb") as handle:
            has_shebang = handle.read(2) == b"#!"
    except OSError:
        has_shebang = False
    if has_shebang and os.access(script, os.X_OK):
        return [path, *args]
    if script.suffix == ".py":
        return [sys.executable, path, *args]
    return ["/bin/sh", path, *args]


@dataclass
class _Run:
    job: Job
    proc: subprocess.Popen
    started: float
    audit_id: int | None = None
    thread: threading.Thread | None = None
    stopping: bool = False
    line_numbers: dict[str, int] | None = None
    output_lock: threading.Lock | None = None


class ScriptRunner:
    def __init__(
        self,
        kill_grace_seconds: float = 10.0,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        audit_store: AuditStore | None = None,
        on_outcome: Callable[[str, str], None] | None = None,
    ) -> None:
        self.kill_grace_seconds = kill_grace_seconds
        self._popen = popen
        self._clock = clock
        self.audit_store = audit_store
        self.on_outcome = on_outcome
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}

    def is_running(self, name: str) -> bool:
        with self._lock:
            return name in self._runs

    def running(self) -> list[str]:
        with self._lock:
            return sorted(self._runs)

    def start(self, job: Job) -> bool:
        """Start ``job`` unless it is already running. Returns True if started."""
        with self._lock:
            if job.name in self._runs:
                return False
            try:
                audit_id = (
                    self.audit_store.begin_execution(job) if self.audit_store else None
                )
            except AuditPersistenceError as exc:
                log.error(
                    "job skipped, audit persistence unavailable",
                    extra={"job": job.name, "error": str(exc)},
                )
                if self.on_outcome:
                    self.on_outcome(job.name, "skipped")
                return False
            argv = command_for(job.script, job.args)
            try:
                proc = self._popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    cwd=job.working_directory,
                    env={
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key not in {"GIT_USERNAME", "GIT_TOKEN", "DB_PASSWORD"}
                        },
                        "JOB_NAME": job.name,
                    },
                    start_new_session=True,
                    text=True,
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                log.error(
                    "job failed to start",
                    extra={"job": job.name, "error": str(exc), "argv": argv},
                )
                self._complete_audit(audit_id, "failed", None)
                if self.on_outcome:
                    self.on_outcome(job.name, "failed")
                return False
            run = _Run(
                job=job,
                proc=proc,
                started=self._clock(),
                audit_id=audit_id,
                line_numbers={"stdout": 0, "stderr": 0},
                output_lock=threading.Lock(),
            )
            if self.audit_store and audit_id is not None:
                try:
                    self.audit_store.set_process_id(audit_id, proc.pid)
                except AuditPersistenceError as exc:
                    log.warning(
                        "could not persist script process id",
                        extra={"job": job.name, "error": str(exc)},
                    )
            self._runs[job.name] = run
            run.thread = threading.Thread(
                target=self._supervise, args=(run,), name=f"job-{job.name}", daemon=True
            )
        log.info(
            "job started",
            extra={
                "job": job.name,
                "pid": proc.pid,
                "argv": argv,
                "commit_author_email": job.commit_author_email,
            },
        )
        run.thread.start()
        return True

    def _supervise(self, run: _Run) -> None:
        readers = [
            threading.Thread(target=self._pump, args=(run, stream, name), daemon=True)
            for stream, name in (
                (run.proc.stdout, "stdout"),
                (run.proc.stderr, "stderr"),
            )
            if stream is not None
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            run.proc.wait(timeout=run.job.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            log.warning(
                "job timed out", extra={"job": run.job.name, "timeout": run.job.timeout}
            )
            self._stop_process(run, self.kill_grace_seconds)
        for reader in readers:
            reader.join(timeout=5)
        duration = round(self._clock() - run.started, 3)
        code = run.proc.returncode
        fields = {
            "job": run.job.name,
            "pid": run.proc.pid,
            "exit_code": code,
            "duration_s": duration,
            "timed_out": timed_out,
            "stopped": run.stopping,
        }
        if code == 0:
            log.info("job finished", extra=fields)
            outcome = "succeeded"
        else:
            log.error("job failed", extra=fields)
            outcome = "failed"
        if timed_out:
            outcome = "timed_out"
        elif run.stopping:
            outcome = "stopped"
        self._complete_audit(run.audit_id, outcome, code)
        if self.on_outcome:
            self.on_outcome(run.job.name, outcome)
        with self._lock:
            if self._runs.get(run.job.name) is run:
                del self._runs[run.job.name]

    def _pump(self, run: _Run, stream: IO[str], stream_name: str) -> None:
        with stream:
            for line in stream:
                message = line.rstrip("\n")
                if (
                    self.audit_store
                    and run.audit_id is not None
                    and run.line_numbers is not None
                    and run.output_lock is not None
                ):
                    with run.output_lock:
                        run.line_numbers[stream_name] += 1
                        line_number = run.line_numbers[stream_name]
                    try:
                        self.audit_store.add_output(
                            run.audit_id, stream_name, line_number, message
                        )
                    except AuditPersistenceError as exc:
                        log.warning(
                            "could not persist script output",
                            extra={
                                "job": run.job.name,
                                "stream": stream_name,
                                "error": str(exc),
                            },
                        )
                log.info(message, extra={"job": run.job.name, "stream": stream_name})

    def _complete_audit(
        self, audit_id: int | None, status: str, exit_code: int | None
    ) -> None:
        if self.audit_store and audit_id is not None:
            try:
                self.audit_store.finish_execution(audit_id, status, exit_code)
            except AuditPersistenceError as exc:
                log.warning(
                    "could not complete script audit",
                    extra={"execution_id": audit_id, "error": str(exc)},
                )

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _stop_process(self, run: _Run, grace: float) -> None:
        run.stopping = True
        if run.proc.poll() is not None:
            return
        self._signal(run.proc, signal.SIGTERM)
        try:
            run.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            log.warning(
                "job ignored SIGTERM, sending SIGKILL",
                extra={"job": run.job.name, "pid": run.proc.pid},
            )
            self._signal(run.proc, signal.SIGKILL)
            run.proc.wait()

    def terminate_all(self, grace: float | None = None) -> None:
        """SIGTERM every running job, SIGKILL any still alive after ``grace``."""
        grace = self.kill_grace_seconds if grace is None else grace
        with self._lock:
            runs = list(self._runs.values())
        if not runs:
            return
        log.info("terminating running jobs", extra={"jobs": [r.job.name for r in runs]})
        for run in runs:
            run.stopping = True
            if run.proc.poll() is None:
                self._signal(run.proc, signal.SIGTERM)
        deadline = self._clock() + grace
        for run in runs:
            remaining = max(0.0, deadline - self._clock())
            try:
                run.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.warning(
                    "job ignored SIGTERM, sending SIGKILL",
                    extra={"job": run.job.name, "pid": run.proc.pid},
                )
                self._signal(run.proc, signal.SIGKILL)
                run.proc.wait()
        for run in runs:
            if run.thread is not None:
                run.thread.join(timeout=5)

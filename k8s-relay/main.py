"""Entrypoint: leader-elected cron scheduler for a multi-replica Deployment."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from kubernetes import config as kube_config

from lease_scheduler.audit import AuditStore, postgres_url
from lease_scheduler.config import ConfigError, load_jobs, load_settings
from lease_scheduler.election import LeaderElector
from lease_scheduler.git_source import GitScriptRepository, GitSourceError
from lease_scheduler.health import setup_logging, start_health_server
from lease_scheduler.metrics import SchedulerMetrics
from lease_scheduler.runner import ScriptRunner
from lease_scheduler.scheduler import Scheduler

log = logging.getLogger("scheduler")


def load_kube_config() -> None:
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config()


def main() -> int:
    setup_logging()
    try:
        settings = load_settings()
        setup_logging(settings.log_level)
        script_source = GitScriptRepository(
            settings.git_repository,
            settings.git_branch,
            settings.scripts_dir,
            settings.git_username,
            settings.git_token,
        )
        script_source.initialize()
        jobs = load_jobs(settings.jobs_file, settings.scripts_dir)
    except (ConfigError, GitSourceError) as exc:
        log.error("invalid configuration", extra={"error": str(exc)})
        return 2
    log.info(
        "configuration loaded",
        extra={
            "identity": settings.identity,
            "namespace": settings.namespace,
            "lease": settings.lease_name,
            "jobs": [job.name for job in jobs.jobs],
            "timezone": str(jobs.timezone),
            "git_scripts_enabled": True,
            "script_branch": settings.git_branch,
        },
    )

    load_kube_config()
    audit_store = None
    if settings.audit_enabled:
        audit_store = AuditStore(
            postgres_url(
                settings.db_user,
                settings.db_password,
                settings.db_host,
                settings.db_port,
                settings.db_name,
            ),
            settings.git_repository,
            settings.git_branch,
            settings.identity,
            f"{settings.namespace}/{settings.lease_name}",
        )
    else:
        log.warning(
            "audit persistence disabled; audit records will be logged to stdout"
        )
    metrics = SchedulerMetrics()
    runner = ScriptRunner(
        kill_grace_seconds=settings.kill_grace_seconds,
        audit_store=audit_store,
        on_outcome=metrics.record_run,
    )
    elector = LeaderElector(
        settings,
        Scheduler(
            jobs,
            runner,
            prepare_job=script_source.prepare,
            on_outcome=metrics.record_run,
        ),
    )
    election_thread = threading.Thread(
        target=elector.run_forever, name="election", daemon=True
    )

    def status() -> tuple[bool, bool, dict]:
        alive = election_thread.is_alive()
        audit_ready = audit_store.is_ready() if audit_store else True
        leader = elector.is_leader
        ready = alive and not elector.shutdown.is_set() and audit_ready
        metrics.update_health(alive, ready, leader, audit_ready)
        details = {
            "identity": settings.identity,
            "leader": leader,
            "running_jobs": runner.running(),
            "audit_database_ready": audit_ready,
        }
        return alive, ready, details

    server = start_health_server(settings.health_port, status, metrics=metrics)

    stop_requested = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        log.info("shutdown requested", extra={"signal": signal.Signals(signum).name})
        stop_requested.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    election_thread.start()
    while not stop_requested.wait(1.0):
        if not election_thread.is_alive():
            log.error("election thread exited unexpectedly")
            break

    elector.stop()
    server.shutdown()
    log.info("shutdown complete")
    return 0 if stop_requested.is_set() else 1


if __name__ == "__main__":
    sys.exit(main())

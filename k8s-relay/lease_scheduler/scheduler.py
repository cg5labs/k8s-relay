"""Leader-only scheduling loop."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from .config import Job, JobsConfig
from .cron import next_fire
from .git_source import GitSourceError
from .runner import ScriptRunner

log = logging.getLogger("scheduler.loop")

MAX_SLEEP_SECONDS = 1.0


class Scheduler:
    """Fires jobs on their cron schedule while this pod holds the lease.

    Next fire times are computed from the moment ``run()`` starts, so a newly
    promoted leader never catches up on ticks missed during failover.
    """

    def __init__(
        self,
        jobs: JobsConfig,
        runner: ScriptRunner,
        now: Callable[[], datetime] | None = None,
        prepare_job: Callable[[Job], Job] | None = None,
        on_outcome: Callable[[str, str], None] | None = None,
    ) -> None:
        self.jobs = jobs
        self.runner = runner
        self._now = now or (lambda: datetime.now(jobs.timezone))
        self.prepare_job = prepare_job
        self.on_outcome = on_outcome
        self._next: dict[str, datetime] = {}

    def reset(self, now: datetime) -> None:
        self._next = {job.name: next_fire(job.schedule, now) for job in self.jobs.jobs}

    def tick(self, now: datetime, can_run: Callable[[], bool] = lambda: True) -> float:
        """Start every due job; return seconds until the next fire time.

        ``can_run`` is checked right before each start to fence stale leaders.
        """
        for job in self.jobs.jobs:
            due = self._next[job.name]
            if due > now:
                continue
            if not can_run():
                log.warning(
                    "skipping tick, lease not recently renewed",
                    extra={"job": job.name, "tick": due.isoformat()},
                )
            elif self.runner.is_running(job.name):
                log.warning(
                    "skipping tick, previous run still active",
                    extra={"job": job.name, "tick": due.isoformat()},
                )
            else:
                try:
                    prepared_job = self.prepare_job(job) if self.prepare_job else job
                except GitSourceError as exc:
                    log.error(
                        "job skipped, script repository refresh failed",
                        extra={"job": job.name, "error": str(exc)},
                    )
                    if self.on_outcome:
                        self.on_outcome(job.name, "failed")
                else:
                    self.runner.start(prepared_job)
            # Compute from ``now`` so several missed ticks collapse into one.
            self._next[job.name] = next_fire(job.schedule, now)
        if not self._next:
            return MAX_SLEEP_SECONDS
        soonest = min(self._next.values())
        return max(0.0, (soonest - now).total_seconds())

    def run(
        self, stop: threading.Event, can_run: Callable[[], bool] = lambda: True
    ) -> None:
        """Blocking loop for one leadership term; returns once ``stop`` is set.

        Running jobs are terminated before returning.
        """
        try:
            self.reset(self._now())
            log.info(
                "scheduler started",
                extra={"jobs": {n: t.isoformat() for n, t in self._next.items()}},
            )
            while not stop.is_set():
                delay = self.tick(self._now(), can_run)
                stop.wait(min(delay, MAX_SLEEP_SECONDS))
        finally:
            self.runner.terminate_all()
            log.info("scheduler stopped")

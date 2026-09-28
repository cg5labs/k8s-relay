import threading
from pathlib import Path

from lease_scheduler.config import Job, JobsConfig
from lease_scheduler.git_source import GitSourceError
from lease_scheduler.scheduler import Scheduler


class FakeRunner:
    def __init__(self):
        self.started = []
        self.active = set()
        self.terminated = 0

    def is_running(self, name):
        return name in self.active

    def start(self, job):
        self.started.append(job.name)
        self.active.add(job.name)
        return True

    def terminate_all(self, grace=None):
        self.terminated += 1
        self.active.clear()


def make(clock, *jobs):
    tz = clock.now().tzinfo
    cfg = JobsConfig(timezone=tz, jobs=tuple(jobs))
    runner = FakeRunner()
    return Scheduler(cfg, runner, now=clock.now), runner


def job(name, schedule="* * * * *"):
    return Job(name=name, schedule=schedule, script=Path(f"/scripts/{name}.sh"))


def test_fires_when_due_and_reports_delay(clock):
    sched, runner = make(clock, job("a"))
    sched.reset(clock.now())
    assert sched.tick(clock.now()) == 60.0
    assert runner.started == []
    clock.advance(seconds=60)
    sched.tick(clock.now())
    assert runner.started == ["a"]


def test_skips_tick_while_previous_run_active(clock):
    sched, runner = make(clock, job("a"))
    sched.reset(clock.now())
    clock.advance(minutes=1)
    sched.tick(clock.now())
    clock.advance(minutes=1)
    sched.tick(clock.now())
    assert runner.started == ["a"]
    runner.active.clear()
    clock.advance(minutes=1)
    sched.tick(clock.now())
    assert runner.started == ["a", "a"]


def test_missed_ticks_collapse_into_one(clock):
    sched, runner = make(clock, job("a"))
    sched.reset(clock.now())
    clock.advance(minutes=10, seconds=5)
    delay = sched.tick(clock.now())
    assert runner.started == ["a"]
    assert delay == 55.0


def test_no_catch_up_after_reset(clock):
    sched, runner = make(clock, job("a", "0 * * * *"))
    # A new leader takes over at 12:30; the 12:00 tick is not replayed.
    clock.advance(minutes=30)
    sched.reset(clock.now())
    sched.tick(clock.now())
    assert runner.started == []
    clock.advance(minutes=30)
    sched.tick(clock.now())
    assert runner.started == ["a"]


def test_run_returns_on_stop_and_terminates_jobs(clock):
    sched, runner = make(clock, job("a"))
    stop = threading.Event()
    thread = threading.Thread(target=sched.run, args=(stop,))
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert runner.terminated == 1


def test_run_exits_immediately_if_already_stopped(clock):
    sched, runner = make(clock, job("a"))
    stop = threading.Event()
    stop.set()
    sched.run(stop)
    assert runner.terminated == 1


def test_fenced_tick_is_skipped_not_deferred(clock):
    sched, runner = make(clock, job("a"))
    sched.reset(clock.now())
    clock.advance(minutes=1)
    sched.tick(clock.now(), can_run=lambda: False)
    assert runner.started == []
    # The skipped tick is not replayed once the lease is confirmed again.
    clock.advance(seconds=30)
    sched.tick(clock.now(), can_run=lambda: True)
    assert runner.started == []
    clock.advance(seconds=30)
    sched.tick(clock.now(), can_run=lambda: True)
    assert runner.started == ["a"]


def test_git_refresh_failure_skips_due_run(clock, caplog):
    sched, runner = make(clock, job("a"))
    outcomes = []

    def fail_refresh(_job):
        raise GitSourceError("branch unavailable")

    sched.prepare_job = fail_refresh
    sched.on_outcome = lambda name, result: outcomes.append((name, result))
    sched.reset(clock.now())
    clock.advance(minutes=1)
    sched.tick(clock.now())
    assert runner.started == []
    assert outcomes == [("a", "failed")]
    assert any(
        "script repository refresh failed" in record.getMessage()
        for record in caplog.records
    )

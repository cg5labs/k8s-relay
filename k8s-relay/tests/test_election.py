import threading
import time
from pathlib import Path
from types import SimpleNamespace

from kubernetes.leaderelection.resourcelock.leaselock import LeaseLock

from lease_scheduler.config import JobsConfig, Settings
from lease_scheduler.election import LeaderElector


def settings(identity, **overrides):
    values = dict(
        jobs_file=Path("/dev/null"),
        scripts_dir=Path("/tmp"),
        lease_name="sched",
        namespace="ops",
        identity=identity,
        lease_duration=3,
        renew_deadline=2,
        retry_period=1,
        kill_grace_seconds=1,
        health_port=0,
        log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


class RecordingScheduler:
    def __init__(self):
        self.terms = 0
        self.running = threading.Event()

    def run(self, stop, can_run=lambda: True):
        self.terms += 1
        self.can_run = can_run
        self.running.set()
        stop.wait()
        self.running.clear()


def lease_lock_factory(fake_api):
    def factory(name, namespace, identity):
        lock = LeaseLock(name, namespace, identity)
        lock.api_instance = fake_api.client_for(identity)
        return lock

    return factory


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def start(elector):
    thread = threading.Thread(target=elector.run_forever, daemon=True)
    thread.start()
    return thread


def test_only_one_leader_and_failover(fake_api):
    pods = {}
    for name in ("pod-a", "pod-b", "pod-c"):
        sched = RecordingScheduler()
        elector = LeaderElector(settings(name), sched, lease_lock_factory(fake_api))
        pods[name] = (elector, sched)
    threads = [start(e) for e, _ in pods.values()]

    assert wait_until(lambda: fake_api.holder("ops", "sched") is not None, 5)
    leader = fake_api.holder("ops", "sched")
    assert wait_until(lambda: pods[leader][1].running.is_set(), 5)
    time.sleep(1.5)
    assert [n for n, (e, _) in pods.items() if e.is_leader] == [leader]

    # Cut the leader off from the API server: it must stop its scheduler and
    # a follower must take over once the lease expires.
    fake_api.unreachable.add(leader)
    assert wait_until(lambda: not pods[leader][1].running.is_set(), 6)
    assert wait_until(lambda: fake_api.holder("ops", "sched") not in (None, leader), 8)
    new_leader = fake_api.holder("ops", "sched")
    assert wait_until(lambda: pods[new_leader][1].running.is_set(), 5)
    assert not pods[leader][0].is_leader

    # The old leader rejoins as a follower rather than exiting.
    fake_api.unreachable.discard(leader)
    time.sleep(2)
    assert threads[list(pods).index(leader)].is_alive()
    assert not pods[leader][0].is_leader
    assert fake_api.holder("ops", "sched") == new_leader

    for elector, _ in pods.values():
        elector.stop()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_stop_terminates_leading_scheduler(fake_api):
    sched = RecordingScheduler()
    elector = LeaderElector(settings("solo"), sched, lease_lock_factory(fake_api))
    thread = start(elector)
    assert wait_until(sched.running.is_set, 5)
    elector.stop()
    assert not sched.running.is_set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_recandidates_after_election_returns():
    runs = []

    class FakeElection:
        def __init__(self, config):
            self.config = config

        def run(self):
            runs.append(self.config)
            if len(runs) >= 2:
                elector.shutdown.set()

    elector = LeaderElector(
        settings("x"),
        RecordingScheduler(),
        lock_factory=lambda n, ns, i: SimpleNamespace(name=n, namespace=ns, identity=i),
        election_factory=FakeElection,
    )
    start_time = time.monotonic()
    elector.run_forever()
    assert len(runs) == 2
    assert time.monotonic() - start_time < 3


def test_graceful_stop_releases_lease_for_fast_handover(fake_api):
    lease_duration = 10
    pods = {}
    for name in ("pod-a", "pod-b"):
        sched = RecordingScheduler()
        s = settings(name, lease_duration=lease_duration, renew_deadline=5)
        pods[name] = (LeaderElector(s, sched, lease_lock_factory(fake_api)), sched)
    threads = {name: start(e) for name, (e, _) in pods.items()}
    assert wait_until(lambda: fake_api.holder("ops", "sched") is not None, 5)
    leader = fake_api.holder("ops", "sched")
    follower = next(n for n in pods if n != leader)
    assert wait_until(pods[leader][1].running.is_set, 5)

    stopped_at = time.monotonic()
    pods[leader][0].stop()
    threads[leader].join(timeout=5)
    assert not threads[leader].is_alive()
    assert wait_until(pods[follower][1].running.is_set, lease_duration)
    # Without the release the follower would wait out the full lease duration.
    assert time.monotonic() - stopped_at < lease_duration - 2
    pods[follower][0].stop()


def test_lease_freshness_fences_stale_leader(fake_api):
    now = [1000.0]
    sched = RecordingScheduler()
    elector = LeaderElector(
        settings("solo", lease_duration=30, renew_deadline=20, retry_period=10),
        sched,
        lease_lock_factory(fake_api),
        clock=lambda: now[0],
    )
    assert not elector.lease_is_fresh()
    thread = start(elector)
    assert wait_until(sched.running.is_set, 5)
    assert elector.lease_is_fresh() and sched.can_run()
    # Simulate the process being paused past renew_deadline without renewing.
    now[0] += 21
    assert not sched.can_run()
    elector.stop()
    thread.join(timeout=5)

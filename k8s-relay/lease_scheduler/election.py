"""Lease-based leader election that re-enters candidacy after losing the lease.

Follows the upstream ``kubernetes/leaderelection/example.py`` pattern:
``LeaseLock`` + ``electionconfig.Config`` + ``LeaderElection(config).run()``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from kubernetes.client.rest import ApiException
from kubernetes.leaderelection import electionconfig, leaderelection
from kubernetes.leaderelection.resourcelock.leaselock import LeaseLock

from .config import Settings
from .scheduler import Scheduler

log = logging.getLogger("scheduler.election")


@dataclass
class _Term:
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    lock: _GuardedLock | None = None


class _ShutdownRequested(Exception):
    """Raised inside the upstream elector to unwind ``run()`` on shutdown."""


class _GuardedLock:
    """Delegates to a resource lock but aborts the elector once shut down.

    The upstream ``acquire()`` and ``renew_loop()`` never return on their own
    while the lease is healthy; raising lets ``run()`` unwind and prevents
    re-acquiring a lease that was just released.
    """

    def __init__(
        self,
        inner: object,
        shutdown: threading.Event,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.namespace = inner.namespace
        self.identity = inner.identity
        self._shutdown = shutdown
        self._clock = clock
        self.last_write: float | None = None

    def _record(self, ok: bool) -> bool:
        # The elector only writes records naming this identity, so a successful
        # write means this pod held the lease at that moment.
        if ok:
            self.last_write = self._clock()
        return ok

    def _check(self) -> None:
        if self._shutdown.is_set():
            raise _ShutdownRequested

    def get(self, name, namespace):
        self._check()
        return self.inner.get(name, namespace)

    def create(self, name, namespace, election_record):
        self._check()
        return self._record(self.inner.create(name, namespace, election_record))

    def update(self, name, namespace, updated_record):
        self._check()
        return self._record(self.inner.update(name, namespace, updated_record))

    def release(self) -> None:
        """Clear the holder so a follower can take over without waiting."""
        api = getattr(self.inner, "api_instance", None)
        if api is None:
            return
        try:
            lease = api.read_namespaced_lease(self.name, self.namespace)
            if lease.spec is None or lease.spec.holder_identity != self.identity:
                return
            lease.spec.holder_identity = None
            api.replace_namespaced_lease(
                name=self.name, namespace=self.namespace, body=lease
            )
            log.info("released lease", extra={"identity": self.identity})
        except ApiException as exc:
            log.warning("failed to release lease", extra={"error": str(exc)})


class LeaderElector:
    def __init__(
        self,
        settings: Settings,
        scheduler: Scheduler,
        lock_factory: Callable[[str, str, str], object] = LeaseLock,
        election_factory: Callable[[object], object] = leaderelection.LeaderElection,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.scheduler = scheduler
        self._lock_factory = lock_factory
        self._election_factory = election_factory
        self._clock = clock
        self.shutdown = threading.Event()
        self._term = _Term()
        self._leader = threading.Event()

    @property
    def is_leader(self) -> bool:
        return self._leader.is_set()

    def lease_is_fresh(self) -> bool:
        """True if this pod renewed the lease within ``renew_deadline``.

        Fences job starts: a leader that was paused or partitioned may still
        think it leads until its renew loop times out, while another pod has
        already taken over the expired lease.
        """
        lock = self._term.lock
        if not self.is_leader or lock is None or lock.last_write is None:
            return False
        return self._clock() - lock.last_write < self.settings.renew_deadline

    def _stop_timeout(self) -> float:
        return self.settings.kill_grace_seconds + 10

    def _on_started(self, term: _Term) -> None:
        term.started = True
        if term.stop.is_set():
            term.done.set()
            return
        self._leader.set()
        log.info("became leader", extra={"identity": self.settings.identity})
        try:
            self.scheduler.run(term.stop, self.lease_is_fresh)
        finally:
            self._leader.clear()
            term.done.set()

    def _on_stopped(self, term: _Term) -> None:
        log.warning("lost leadership", extra={"identity": self.settings.identity})
        term.stop.set()
        if term.started and not term.done.wait(self._stop_timeout()):
            log.error("scheduler did not stop in time")

    def _config(self, term: _Term) -> electionconfig.Config:
        term.lock = _GuardedLock(
            self._lock_factory(
                self.settings.lease_name,
                self.settings.namespace,
                self.settings.identity,
            ),
            self.shutdown,
            self._clock,
        )
        return electionconfig.Config(
            term.lock,
            lease_duration=self.settings.lease_duration,
            renew_deadline=self.settings.renew_deadline,
            retry_period=self.settings.retry_period,
            onstarted_leading=lambda: self._on_started(term),
            onstopped_leading=lambda: self._on_stopped(term),
        )

    def run_forever(self) -> None:
        """Campaign for the lease until ``stop()``; blocks while following."""
        while not self.shutdown.is_set():
            term = _Term()
            self._term = term
            log.info(
                "joining leader election as candidate",
                extra={
                    "identity": self.settings.identity,
                    "lease": f"{self.settings.namespace}/{self.settings.lease_name}",
                },
            )
            try:
                self._election_factory(self._config(term)).run()
            except _ShutdownRequested:
                break
            except Exception:
                log.exception("leader election failed")
            if self.shutdown.wait(self.settings.retry_period):
                break

    def stop(self) -> None:
        """Stop campaigning, terminate scripts and release the lease if leading."""
        self.shutdown.set()
        term = self._term
        term.stop.set()
        if term.started:
            if not term.done.wait(self._stop_timeout()):
                log.error("scheduler did not stop in time")
            if term.lock is not None:
                term.lock.release()

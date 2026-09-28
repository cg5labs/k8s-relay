import copy
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from kubernetes.client import V1ObjectMeta
from kubernetes.client.rest import ApiException


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))


def _api_error(status: int, reason: str) -> ApiException:
    exc = ApiException(status=status, reason=reason)
    exc.body = json.dumps({"code": status, "reason": reason})
    return exc


class FakeCoordinationApi:
    """In-memory stand-in for CoordinationV1Api with optimistic concurrency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases = {}
        self._version = 0
        self.unreachable = set()

    def _check(self, caller):
        if caller in self.unreachable:
            raise _api_error(503, "Service Unavailable")

    def client_for(self, identity):
        api = self

        class Bound:
            def read_namespaced_lease(self, name, namespace):
                api._check(identity)
                with api._lock:
                    lease = api._leases.get((namespace, name))
                    if lease is None:
                        raise _api_error(404, "Not Found")
                    return copy.deepcopy(lease)

            def create_namespaced_lease(self, namespace, body):
                api._check(identity)
                with api._lock:
                    meta = body.metadata
                    name = meta["name"] if isinstance(meta, dict) else meta.name
                    if (namespace, name) in api._leases:
                        raise _api_error(409, "Conflict")
                    api._version += 1
                    stored = copy.deepcopy(body)
                    stored.metadata = V1ObjectMeta(
                        name=name, resource_version=str(api._version)
                    )
                    api._leases[(namespace, name)] = stored
                    return copy.deepcopy(stored)

            def replace_namespaced_lease(self, name, namespace, body):
                api._check(identity)
                with api._lock:
                    current = api._leases.get((namespace, name))
                    if current is None:
                        raise _api_error(404, "Not Found")
                    if (
                        body.metadata.resource_version
                        != current.metadata.resource_version
                    ):
                        raise _api_error(409, "Conflict")
                    api._version += 1
                    stored = copy.deepcopy(body)
                    stored.metadata = V1ObjectMeta(
                        name=name, resource_version=str(api._version)
                    )
                    api._leases[(namespace, name)] = stored
                    return copy.deepcopy(stored)

        return Bound()

    def holder(self, namespace, name):
        with self._lock:
            lease = self._leases.get((namespace, name))
            return None if lease is None else lease.spec.holder_identity


@pytest.fixture
def fake_api():
    return FakeCoordinationApi()

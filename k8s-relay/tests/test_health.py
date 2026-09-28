import json
import logging
import urllib.error
import urllib.request

import pytest

from lease_scheduler.health import JsonFormatter, start_health_server
from lease_scheduler.metrics import SchedulerMetrics


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


@pytest.fixture
def server():
    state = {"live": True, "ready": False}
    metrics = SchedulerMetrics()
    srv = start_health_server(
        0,
        lambda: (state["live"], state["ready"], {"leader": True}),
        "127.0.0.1",
        metrics,
    )
    yield srv.server_address[1], state
    srv.shutdown()


def test_healthz_and_readyz(server):
    port, state = server
    assert get(port, "/healthz") == (200, {"ok": True, "leader": True})
    assert get(port, "/readyz")[0] == 503
    state["ready"] = True
    assert get(port, "/readyz")[0] == 200
    state["live"] = False
    assert get(port, "/healthz")[0] == 503


def test_json_formatter_includes_extras():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "hello", (), None)
    record.job = "a"
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hello" and out["job"] == "a" and out["level"] == "INFO"


def test_metrics_endpoint_exposes_health_and_job_counters():
    metrics = SchedulerMetrics()
    metrics.record_run("backup", "succeeded")
    srv = start_health_server(
        0,
        lambda: (
            True,
            False,
            {"leader": True, "audit_database_ready": False},
        ),
        "127.0.0.1",
        metrics,
    )
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/metrics"
        ) as response:
            body = response.read().decode()
        assert "k8s_deployer_app_health 1.0" in body
        assert "k8s_deployer_app_ready 0.0" in body
        assert "k8s_deployer_leader 1.0" in body
        assert "k8s_deployer_audit_database_ready 0.0" in body
        assert (
            'k8s_deployer_job_runs_total{job="backup",result="succeeded"} 1.0' in body
        )
    finally:
        srv.shutdown()

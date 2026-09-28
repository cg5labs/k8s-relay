"""Prometheus metrics for scheduler and script execution health."""

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest


class SchedulerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.app_health = Gauge(
            "k8s_deployer_app_health",
            "Whether the scheduler process is alive.",
            registry=self.registry,
        )
        self.app_ready = Gauge(
            "k8s_deployer_app_ready",
            "Whether the scheduler is ready to execute jobs.",
            registry=self.registry,
        )
        self.leader = Gauge(
            "k8s_deployer_leader",
            "Whether this pod currently holds the scheduler lease.",
            registry=self.registry,
        )
        self.audit_database_ready = Gauge(
            "k8s_deployer_audit_database_ready",
            "Whether configured audit persistence is available.",
            registry=self.registry,
        )
        self.job_runs = Counter(
            "k8s_deployer_job_runs",
            "Scheduled script run outcomes, including runs skipped before execution.",
            labelnames=("job", "result"),
            registry=self.registry,
        )

    def update_health(
        self, live: bool, ready: bool, leader: bool, audit_ready: bool
    ) -> None:
        self.app_health.set(int(live))
        self.app_ready.set(int(ready))
        self.leader.set(int(leader))
        self.audit_database_ready.set(int(audit_ready))

    def record_run(self, job: str, result: str) -> None:
        self.job_runs.labels(job=job, result=result).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)

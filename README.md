# k8s-deployer-plus: leader-elected script scheduler

A Python cron scheduler that runs from scripts stored in a separate Git
repository. It runs as a 3-replica Kubernetes Deployment. Replicas compete for a
`coordination.k8s.io/v1` Lease, and only the pod that holds it runs scripts.
The other pods stay idle as followers until one of them is promoted. The Helm
chart enforces a positive odd `replicaCount` (such as 1, 3, or 5) to maintain
the intended quorum for lease leader election.

Leader election follows the upstream
[`leaderelection/example.py`](https://github.com/kubernetes-client/python/blob/master/kubernetes/leaderelection/example.py)
(`LeaseLock` + `electionconfig.Config` + `LeaderElection(config).run()`).
`LeaseLock` is not in a PyPI release yet, so `requirements.txt` pins the
`kubernetes` client to upstream commit `ad199bf`. Installing it needs `git`.

## Application Overview

- **Follower.** Every pod joins the election. Followers only poll the Lease.
- **Leader.** The pod holding the Lease runs the scheduler loop. When a job's
  cron time arrives, the loop starts its script as a subprocess.
  - It skips a run if the previous run of the same job is still going.
  - Before each start it checks that the lease was renewed within
    `RENEW_DEADLINE`. A leader that was paused or cut off from the network
    therefore can't start jobs after another pod has taken over.
- **No catch-up.** A newly promoted leader schedules from the moment it takes
  over. Runs missed during failover are not replayed.
- **Losing the lease.** Running scripts get SIGTERM, then SIGKILL after
  `KILL_GRACE_SECONDS`. The pod then rejoins the election as a follower
  without restarting.
- **Pod shutdown (SIGTERM).** The pod stops its scripts the same way, then
  clears the Lease holder so a follower can take over within about one
  `RETRY_PERIOD` instead of waiting out the whole `LEASE_DURATION`.
- **Crash or kill -9.** The Lease expires and a follower takes over after about
  `LEASE_DURATION`. The identity is the pod name, so a container restarted in
  the same pod can take its lease straight back.

## Configuration

Job definitions live in YAML, mounted from a ConfigMap at `JOBS_FILE`. Script
contents are not stored in Helm values. Set `git.repository` to the clone URL
for the repository containing the scripts, and set `git.branch` to the branch
to use (defaults to `main`). Each pod clones that repository into `SCRIPTS_DIR`
at startup. Before each scheduled execution, the leader pulls the configured
branch with fast-forward-only semantics and runs a snapshot of its tip commit.
A failed pull or a script missing from the pulled revision skips that run; no
older version is run as a fallback. Each execution is attributed to the tip
commit's author and committer, not file-level blame.

The `script` setting is a path **relative to the root of the Git repository**,
not a local container path and not a path prefixed with the repository's name.
For example, if the repository contains `scripts/hello.sh`, configure
`script: scripts/hello.sh`. The path must stay within the checkout. Jobs,
schedules, arguments, and timeouts remain in Helm-managed YAML; only script
files come from Git.

This complete example uses the public `devops-scripts` repository and its
`scripts/hello.sh` file:

```yaml
replicaCount: 3
timezone: UTC

git:
  repository: https://github.com/cg5labs/devops-scripts.git
  branch: main
  # For a private HTTPS repository, set this to the Secret name and create
  # that Secret with the username and token keys shown below.
  credentialSecretName: ""
  usernameSecretKey: username
  tokenSecretKey: token

jobs:
  - name: hello
    schedule: "*/5 * * * *"
    script: scripts/hello.sh
    args: ["world"]
    timeout: 60
```

Save the configuration as `scheduler-values.yaml`, then install or update the
release:

```bash
helm upgrade --install sched chart -n ops --create-namespace \
  -f scheduler-values.yaml
```

For a private HTTPS repository, create a Kubernetes Secret containing keys
`username` and `token`, then set `git.credentialSecretName` to its name. For
example, use protected local files and do not commit them:

```bash
kubectl -n ops create secret generic ops-scripts-git \
  --from-file=username=./git-username \
  --from-file=token=./git-token
```

Then set `git.credentialSecretName: ops-scripts-git`. Do not put credentials
in the repository URL. The scheduler removes Git credentials, along with
`DB_PASSWORD`, from the environment passed to scripts.

The scheduler picks how to run each script as follows:

- an executable file with a `#!` line runs directly;
- `*.py` runs with the container's Python;
- anything else runs with `/bin/sh`.

Scripts run with the pod's non-secret environment plus `JOB_NAME`. Their
stdout and stderr are logged line by line as JSON to container stdout. The
repository is never updated underneath a running script: each run uses its
own snapshot.

### Git script snapshots

Before a run, the leader fast-forward-pulls the configured branch, reads the
resulting commit SHA and author/committer metadata, then copies the checkout
without its `.git` directory to a pod-local path under
`/tmp/k8s-deployer-snapshots/`. The snapshot path includes a job-specific
directory and the commit SHA. The scheduler verifies that the configured
script exists in that copy, then runs it with the snapshot as its working
directory.

This isolates each run from later Git pulls, so a running script keeps using
the exact files from the revision it started with. It also ties the run and its
audit/log metadata to a specific commit. Snapshots are temporary pod storage,
not durable data; a replacement pod clones the repository and creates snapshots
again. If the pull fails or the script is absent from the new revision, that
run is skipped instead of silently running an older copy.

### Audit logging and PostgreSQL

Set `audit.enabled: true` to persist execution audits with SQLAlchemy in
PostgreSQL. Versioned Alembic migrations are applied automatically when the
scheduler checks database readiness. The schema is normalized and linked by
foreign keys rather than storing the entire audit in one large table:

| Table | Information |
| --- | --- |
| `repositories` | Repository URL and configured branch |
| `git_commits` | Commit SHA, author and committer references, authored/committed timestamps |
| `identities` | Normalized Git author and committer names/emails |
| `scripts` | Repository-relative script paths |
| `scheduled_jobs` | Job name, schedule, script, arguments, and timeout |
| `script_executions` | Job, script, commit, pod/process identity, start/finish times, status, and exit code |
| `execution_output` | Ordered stdout/stderr lines and capture timestamps, stored separately per execution |
| `transactions` | One consolidated record per execution with repository name, commit SHA, script path, combined log output, and commit author name/email |

The normalized records remain the source for relational audit details, while
`transactions` provides a single per-run summary that is backfilled from
existing executions when upgrading to schema version 0002. When
audit persistence is disabled, the scheduler warns; script and application
output still goes to container stdout, but there are no database audit rows.
If enabled PostgreSQL is unavailable, the pod becomes unready and new script
runs are skipped until the database is ready. A database failure during a run
is logged; the scheduler continues draining script output to stdout.

To apply a database schema change, add a new Alembic migration version to the
app code, rebuild and push the container image with a new tag so it includes
the migration, then redeploy the Helm chart with that updated image tag. When
the deployed app connects to the database for readiness checks, it automatically
runs the pending migrations. Verify the active revision in PostgreSQL with
`SELECT version_num FROM alembic_version;`.

The chart can create a minimal in-release PostgreSQL instance based on the
Docker Official `postgres:16` image. It is a single-replica StatefulSet with a
headless Service, password Secret, optional configuration ConfigMap, probes,
and a PVC. The PVC is retained when the Helm release is uninstalled: plan
backups and PVC cleanup separately. This minimal database chart is not a
high-availability PostgreSQL operator or a backup solution.

| Env var | Default | Purpose |
| --- | --- | --- |
| `POD_NAME` | hostname + random suffix | Lease holder identity (Downward API) |
| `POD_NAMESPACE` | service account namespace | Lease namespace |
| `LEASE_NAME` | `k8s-deployer` | Lease object name |
| `LEASE_DURATION` / `RENEW_DEADLINE` / `RETRY_PERIOD` | `17` / `15` / `5` | Election timings (seconds) |
| `KILL_GRACE_SECONDS` | `10` | Wait between SIGTERM and SIGKILL for scripts |
| `JOBS_FILE` | `/etc/scheduler/jobs.yaml` | Jobs YAML |
| `SCRIPTS_DIR` | `/opt/scripts` | Script directory |
| `GIT_REPOSITORY` | required | Scripts repository URL |
| `GIT_BRANCH` | `main` | Repository branch to clone and pull |
| `GIT_USERNAME` / `GIT_TOKEN` | unset | Optional HTTPS credentials |
| `AUDIT_ENABLED` | `false` | Enable PostgreSQL audit persistence |
| `DB_HOST` / `DB_PORT` | empty / `5432` | PostgreSQL host and port; host required when audit is enabled |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | empty | PostgreSQL database name and credentials; required when audit is enabled |
| `HEALTH_PORT` | `8080` | `/healthz` liveness, `/readyz` readiness, `/metrics` Prometheus |
| `LOG_LEVEL` | `INFO` | Log level |

`/healthz` is the liveness endpoint and remains healthy while the scheduler
process is alive. `/readyz` reports readiness and returns HTTP 503 when enabled
audit persistence cannot connect; both return JSON including pod identity,
leader state, running jobs, and audit database readiness.

`/metrics` returns Prometheus text exposition format. It exports
`k8s_deployer_app_health`, `k8s_deployer_app_ready`,
`k8s_deployer_leader`, `k8s_deployer_audit_database_ready`, and the
`k8s_deployer_job_runs_total` counter labelled by `job` and `result`
(`succeeded`, `failed`, `timed_out`, `stopped`, or `skipped`). When
`metrics.enabled` is true, the chart adds Prometheus scrape annotations to the
scheduler pod. Startup, liveness, and readiness probes use `/healthz` and
`/readyz` on the same health port.

## Deployments with Helm

```bash
docker build -t k8s-deployer:0.1.0 .
helm install sched chart -n ops --create-namespace \
  --set image.repository=k8s-deployer --set image.tag=0.1.0 \
  --set git.repository=https://github.com/example/ops-scripts.git
kubectl -n ops get lease sched-k8s-deployer -o jsonpath='{.spec.holderIdentity}'
```

Configure `git.repository`, `git.branch`, and `jobs` in your values file as
shown in the complete script example above. The chart creates:

- a ServiceAccount;
- a Role that can only `create` Leases and `get`/`update` its own Lease;
- a ConfigMap for job definitions;
- the Deployment. It uses the Downward API for `POD_NAME`/`POD_NAMESPACE`,
  prefers spreading pods across nodes, and runs as non-root with a read-only
  root filesystem. Prometheus scrape annotations and startup, liveness, and
  readiness probes use the health port.

Audit persistence is opt-in with `audit.enabled: true`. To deploy PostgreSQL
inside the release, also set `postgresql.enabled: true`; the chart creates a
single-replica StatefulSet based on the Docker Official Image `postgres:16`,
a headless Service, Secret, optional configuration ConfigMap, and PVC. Set
`postgresql.auth.password` to provide a Helm-managed password, or preferably
set `postgresql.auth.existingSecret` and
`postgresql.auth.existingSecretPasswordKey` to use an existing Secret. The
Secret must be available to the PostgreSQL pod and contain the database
password. The chart sets `POSTGRES_DB`, `POSTGRES_USER`, and
`POSTGRES_PASSWORD_FILE` for the official image. The PVC is retained on Helm
uninstall; manage its lifecycle and backups separately. Optional
`postgresql.configuration` entries are mounted as `postgresql.conf`; the
chart defaults `listen_addresses` to `*` for in-cluster connections.

For an external service, leave `postgresql.enabled` false and configure
`audit.database.host`, `port`, `name`, `username`, and
`passwordSecretName`/`passwordSecretKey`. Database passwords stay in a
Kubernetes Secret. In either mode, the scheduler uses these connection
settings when `audit.enabled` is true.

Example values for Git-hosted scripts and bundled PostgreSQL:

```yaml
git:
  repository: https://github.com/cg5labs/devops-scripts.git
  branch: main
jobs:
  - name: hello
    schedule: "*/5 * * * *"
    script: scripts/hello.sh
    args: ["world"]
    timeout: 60
audit:
  enabled: true
postgresql:
  enabled: true
  auth:
    existingSecret: scheduler-postgresql-auth
    existingSecretPasswordKey: password
  persistence:
    enabled: true
    size: 8Gi
```

For an external PostgreSQL service, set `postgresql.enabled: false`, provide
`audit.database.host`, `port`, `name`, and `username`, and set
`audit.database.passwordSecretName` plus its `passwordSecretKey`. In Helm
values, `audit.database.passwordSecretName` is required for external databases.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
python -m black --check .
```

The tests use an in-memory fake of the Lease API, run through the real
upstream elector, and cover single-leader, failover, step-down, lease release
and fencing. The scheduler tests use a fake clock. From the repository root,
`pre-commit install` enables this project's pytest and Black hooks.

You can run it locally against a cluster from your kubeconfig. It falls back to
`load_kube_config()` when it isn't running in a cluster. Set `GIT_REPOSITORY`
and optionally `GIT_BRANCH`; configure `AUDIT_ENABLED` and the `DB_*` variables
when testing persistence:

```bash
GIT_REPOSITORY=https://github.com/example/ops-scripts.git \
JOBS_FILE=./jobs.yaml SCRIPTS_DIR=./scripts POD_NAMESPACE=default python main.py
```

![CI](https://github.com/Dhayalramesh/release-guard/actions/workflows/ci.yml/badge.svg)

# release-guard

A small release tool that deploys a service through **staging -> production** with the process controls an operations team expects: change records, health checks, and automatic rollback. Every push is tested end to end in GitHub Actions.

## What it does

- **Change record required for every release:** version, risk level, approver, and rollback plan are validated (Pydantic) before anything deploys.
- **Promotion rule:** a version can only go to production after it has passed staging.
- **Health checks with retry and exponential backoff** after every deploy.
- **Automatic rollback:** if a release is unhealthy, the last good version is restored.
- **Deploy history:** every deploy, failure, and restore is stored in SQLite, and there is a manual `rollback` command.

## How a deploy works

1. Validate the change record. A bad or incomplete record is rejected.
2. For production, check that the version already succeeded in staging.
3. Stop the current service and start the new version.
4. Poll `/health` with retry and backoff.
5. Healthy: record `success`. Unhealthy: record `failed`, restore the previous version, and record `restored`.

## Proof of run

Every push runs an end-to-end scenario in GitHub Actions: a good release is promoted from staging to production, an unstaged version is blocked, and a broken release is detected and rolled back automatically.

<!-- Drag your cropped screenshot of the "Deploy history (proof of the run)" step here -->

## CI pipeline

| Job | What it checks |
|---|---|
| `test` | 9 pytest tests (change-record validation, promotion rule, rollback, manual rollback) |
| `docker-smoke` | Builds the Docker image, confirms a healthy container passes and a broken one is rejected (Bash health-check script) |
| `release-demo` | Runs the real CLI: good release, promotion, blocked release, broken release with automatic rollback, then prints the deploy history |

## Quick start

```bash
pip install -r requirements.txt
pytest -v

python releaseguard.py deploy --env staging --version v1.0.0 --risk low \
    --approver "Asha" --rollback-plan "redeploy previous tag"
python releaseguard.py deploy --env production --version v1.0.0 --risk low \
    --approver "Asha" --rollback-plan "redeploy previous tag"

# simulate a bad release: it is detected and rolled back automatically
python releaseguard.py deploy --env staging --version v1.1.0 --risk medium \
    --approver "Asha" --rollback-plan "redeploy previous tag" --simulate-broken

python releaseguard.py history
python releaseguard.py stop
```

## Commands

| Command | Purpose |
|---|---|
| `deploy` | Deploy a version to `staging` or `production` |
| `rollback` | Manually roll back to the previous good version |
| `status` | Show the recorded live version and current health |
| `history` | Show the deploy history |
| `stop` | Stop all running services |

## Project layout

```
app/main.py                demo FastAPI service (/health, /version)
releaseguard.py            deploy logic and CLI
tests/                     pytest suite
scripts/healthcheck.sh     Bash health-check used in CI
Dockerfile                 container image for the demo service
pytest.ini                 puts the repo root on the import path
.github/workflows/ci.yml   test + docker smoke test + release demo on every push
```

## Limitations and next steps

- Staging and production run as two local processes on different ports, so no cloud account is needed. `--simulate-broken` exists only to demonstrate rollback.
- There is no manual approval step yet. The approver is a required field in the change record.
- Possible next steps: deploy real containers instead of local processes, add a manual approval gate with GitHub Environments, and add notifications on rollback.

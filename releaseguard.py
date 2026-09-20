"""Release Guard: deploy with a change record, health checks and automatic rollback."""
import argparse
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from pydantic import BaseModel, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent
SEMVER = re.compile(r"^v\d+\.\d+\.\d+$")
DEFAULT_PORTS = {"staging": 8101, "production": 8102}


class DeployError(Exception):
    """Raised when a deployment is blocked by a process rule."""


class ChangeRecord(BaseModel):
    """The change-management record required for every release."""

    version: str
    risk: str = Field(pattern=r"^(low|medium|high)$")
    approver: str = Field(min_length=2)
    rollback_plan: str = Field(min_length=5)
    description: str = ""

    @field_validator("version")
    @classmethod
    def _semver(cls, v):
        if not SEMVER.match(v):
            raise ValueError("version must look like v1.2.3")
        return v


class Deployer:
    def __init__(self, state_dir=None, ports=None, app_module="app.main:app",
                 retries=6, backoff=0.5, max_delay=2.0):
        self.state = Path(state_dir) if state_dir else ROOT / ".state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.ports = ports or dict(DEFAULT_PORTS)
        self.app_module = app_module
        self.retries = retries
        self.backoff = backoff
        self.max_delay = max_delay
        self.db = self.state / "history.db"
        self._procs = {}
        self._q(
            "CREATE TABLE IF NOT EXISTS deployments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, env TEXT, version TEXT, "
            "risk TEXT, approver TEXT, status TEXT, note TEXT)"
        )

    # ---------- history (SQLite) ----------
    def _q(self, sql, args=(), fetch=False):
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.fetchall() if fetch else None
        finally:
            conn.close()

    def _log(self, env, version, risk, approver, status, note=""):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._q(
            "INSERT INTO deployments (ts, env, version, risk, approver, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, env, version, risk, approver, status, note),
        )

    def history(self, env=None):
        sql = "SELECT id, ts, env, version, status, note FROM deployments"
        args = ()
        if env:
            sql += " WHERE env = ?"
            args = (env,)
        return self._q(sql + " ORDER BY id", args, fetch=True)

    def live_version(self, env):
        rows = self._q(
            "SELECT version FROM deployments WHERE env = ? "
            "AND status IN ('success', 'restored') ORDER BY id DESC LIMIT 1",
            (env,), fetch=True,
        )
        return rows[0][0] if rows else None

    def _staged_ok(self, version):
        rows = self._q(
            "SELECT 1 FROM deployments WHERE env = 'staging' AND version = ? "
            "AND status = 'success' LIMIT 1",
            (version,), fetch=True,
        )
        return bool(rows)

    # ---------- process control ----------
    def _pidfile(self, env):
        return self.state / f"{env}.pid"

    def _stop(self, env):
        proc = self._procs.pop(env, None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            pf = self._pidfile(env)
            if pf.exists():
                try:
                    pid = int(pf.read_text())
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(30):
                        time.sleep(0.1)
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            break
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        self._pidfile(env).unlink(missing_ok=True)

    def _start(self, env, version, broken=False):
        self._stop(env)
        env_vars = dict(os.environ, APP_VERSION=version, APP_BROKEN="1" if broken else "0")
        log = open(self.state / f"{env}.log", "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", self.app_module,
             "--port", str(self.ports[env]), "--log-level", "warning"],
            cwd=str(ROOT), env=env_vars, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )
        self._procs[env] = proc
        self._pidfile(env).write_text(str(proc.pid))

    def stop_all(self):
        for env in self.ports:
            self._stop(env)

    # ---------- health check: retry with exponential backoff ----------
    def _healthy(self, env, version):
        url = f"http://127.0.0.1:{self.ports[env]}/health"
        delay = self.backoff
        for _ in range(self.retries):
            try:
                r = requests.get(url, timeout=2)
                if r.status_code == 200 and r.json().get("version") == version:
                    return True
            except (requests.RequestException, ValueError):
                pass
            time.sleep(delay)
            delay = min(delay * 2, self.max_delay)
        return False

    # ---------- actions ----------
    def deploy(self, env, record, simulate_broken=False):
        if env not in self.ports:
            raise DeployError(f"unknown environment '{env}'")
        if env == "production" and not self._staged_ok(record.version):
            raise DeployError(
                f"{record.version} has not passed staging; deploy it to staging first"
            )
        previous = self.live_version(env)
        self._start(env, record.version, simulate_broken)
        if self._healthy(env, record.version):
            self._log(env, record.version, record.risk, record.approver,
                      "success", "health check passed")
            return {"status": "success", "live": record.version}

        self._log(env, record.version, record.risk, record.approver,
                  "failed", "health check failed")
        if previous:
            self._start(env, previous)
            ok = self._healthy(env, previous)
            self._log(env, previous, record.risk, "auto-rollback",
                      "restored" if ok else "restore_failed",
                      f"rolled back after {record.version} failed")
            return {"status": "rolled_back", "live": previous if ok else None}
        self._stop(env)
        return {"status": "failed", "live": None}

    def rollback(self, env):
        live = self.live_version(env)
        rows = self._q(
            "SELECT version FROM deployments WHERE env = ? AND status = 'success' "
            "AND version != ? ORDER BY id DESC LIMIT 1",
            (env, live or ""), fetch=True,
        )
        if not rows:
            raise DeployError("no previous version to roll back to")
        target = rows[0][0]
        self._start(env, target)
        ok = self._healthy(env, target)
        self._log(env, target, "n/a", "manual-rollback",
                  "restored" if ok else "restore_failed", f"manual rollback from {live}")
        return target if ok else None

    def status(self, env):
        live = self.live_version(env)
        try:
            r = requests.get(f"http://127.0.0.1:{self.ports[env]}/health", timeout=2)
            http = r.status_code
        except requests.RequestException:
            http = None
        return {"env": env, "recorded_live": live, "health_http": http}


# ---------- CLI ----------
def main(argv=None):
    p = argparse.ArgumentParser(prog="releaseguard", description="Safe deploys with auto-rollback")
    p.add_argument("--state-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="deploy a version to an environment")
    d.add_argument("--env", required=True, choices=list(DEFAULT_PORTS))
    d.add_argument("--version", required=True)
    d.add_argument("--risk", required=True)
    d.add_argument("--approver", required=True)
    d.add_argument("--rollback-plan", required=True)
    d.add_argument("--description", default="")
    d.add_argument("--simulate-broken", action="store_true",
                   help="testing only: start the release in a failing state")

    r = sub.add_parser("rollback", help="manually roll back to the previous good version")
    r.add_argument("--env", required=True, choices=list(DEFAULT_PORTS))

    s = sub.add_parser("status", help="show what is live")
    s.add_argument("--env", required=True, choices=list(DEFAULT_PORTS))

    h = sub.add_parser("history", help="show deploy history")
    h.add_argument("--env", choices=list(DEFAULT_PORTS))

    sub.add_parser("stop", help="stop all running services")

    a = p.parse_args(argv)
    dep = Deployer(state_dir=a.state_dir)
    try:
        if a.cmd == "deploy":
            rec = ChangeRecord(version=a.version, risk=a.risk, approver=a.approver,
                               rollback_plan=a.rollback_plan, description=a.description)
            out = dep.deploy(a.env, rec, simulate_broken=a.simulate_broken)
            print(f"[{a.env}] result={out['status']} live={out['live']}")
            return 0 if out["status"] == "success" else 1
        if a.cmd == "rollback":
            print(f"[{a.env}] rolled back to {dep.rollback(a.env)}")
        elif a.cmd == "status":
            print(dep.status(a.env))
        elif a.cmd == "history":
            for row in dep.history(a.env):
                print(" | ".join(str(x) for x in row))
        elif a.cmd == "stop":
            dep.stop_all()
            print("stopped")
        return 0
    except ValidationError as e:
        print("Invalid change record:")
        for err in e.errors():
            print(f"  - {err['loc'][0]}: {err['msg']}")
        return 2
    except DeployError as e:
        print(f"Blocked: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

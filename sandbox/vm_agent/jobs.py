"""Job records on the VM's disk.

What: start, observe, cancel, and tail the processes behind exec jobs.
Why: the record on disk is authoritative. `exec_start` writes it before
spawning and returns the existing job if the id is already known, so a retried
start never runs a command twice. `exec_wait` reads it, so a wait can reattach
after the orchestrator restarts. A wrapper shell writes the exit code to the
record when the command ends, so completion is visible even if nobody was
watching. The command runs in its own session so cancel can kill the whole
group.
Production: identical. `run_as_user` is the unprivileged agent user and sudo is
the privilege boundary; the same code runs the tests as the current user.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sandbox.contract.types import ExecJob, ExecSpec
from sandbox.timeutil import now_iso

# "$0" is the job directory. The command's exit code is written atomically so a
# reader never sees an empty exit file.
_WRAPPER = '"$@"; rc=$?; printf "%s" "$rc" > "$0/exit.tmp"; mv "$0/exit.tmp" "$0/exit"; exit "$rc"'


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    running: bool
    exit_code: int | None
    started_at: float
    ended_at: float | None
    reason: str
    lost: bool


def _safe(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", job_id)


def _pid_alive(pid: int) -> bool:
    try:
        # Reap it if it is our own finished child, so a zombie does not look alive.
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


class JobStore:
    def __init__(self, root: Path, vm_id: str, run_as_user: str | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.vm_id = vm_id
        self.run_as_user = run_as_user
        self._procs: dict[str, subprocess.Popen] = {}

    # ---- paths ---------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        return self.root / _safe(job_id)

    def log_path(self, job_id: str, stream: str) -> Path:
        return self.job_dir(job_id) / f"{stream}.log"

    def exists(self, job_id: str) -> bool:
        return (self.job_dir(job_id) / "spec.json").exists()

    def load_spec(self, job_id: str) -> ExecSpec:
        return ExecSpec(**json.loads((self.job_dir(job_id) / "spec.json").read_text()))

    def _meta(self, job_id: str) -> dict | None:
        path = self.job_dir(job_id) / "meta.json"
        return json.loads(path.read_text()) if path.exists() else None

    def pgid(self, job_id: str) -> int:
        meta = self._meta(job_id)
        if meta is None:
            raise KeyError(job_id)
        return int(meta["pgid"])

    # ---- lifecycle -----------------------------------------------------------

    def start(self, spec: ExecSpec, env: dict[str, str]) -> ExecJob:
        d = self.job_dir(spec.job_id)
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            meta = self._meta(spec.job_id)
            if meta is None:
                raise
            return ExecJob(job_id=spec.job_id, vm_id=self.vm_id, started_at=meta["started_at_iso"])

        (d / "spec.json").write_text(json.dumps(asdict(spec)))
        argv = self._wrap(spec, env, d)
        try:
            with open(d / "stdout.log", "ab") as out, open(d / "stderr.log", "ab") as err:
                proc = subprocess.Popen(
                    argv,
                    cwd=spec.cwd,
                    env=self._popen_env(env),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        started = time.time()
        meta = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "started_at": started,
            "started_at_iso": now_iso(),
        }
        (d / "meta.json").write_text(json.dumps(meta))
        self._procs[spec.job_id] = proc
        return ExecJob(job_id=spec.job_id, vm_id=self.vm_id, started_at=meta["started_at_iso"])

    def _wrap(self, spec: ExecSpec, env: dict[str, str], d: Path) -> list[str]:
        inner = list(spec.argv)
        if self.run_as_user:
            inner = [
                "sudo",
                "-n",
                "-u",
                self.run_as_user,
                "--",
                "/usr/bin/env",
                "-i",
                *[f"{k}={v}" for k, v in env.items()],
                *inner,
            ]
        return ["sh", "-c", _WRAPPER, str(d), *inner]

    def _popen_env(self, env: dict[str, str]) -> dict[str, str]:
        # The wrapper shell and sudo need a PATH; the job's own environment is
        # rebuilt from `env -i` when sudo is in play, or is `env` itself otherwise.
        base = {"PATH": env.get("PATH") or os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
        return base if self.run_as_user else {**base, **env}

    def status(self, job_id: str) -> JobStatus:
        meta = self._meta(job_id)
        if meta is None:
            return JobStatus(job_id, False, None, 0.0, None, "", True)
        proc = self._procs.get(job_id)
        if proc is not None:
            proc.poll()
        d = self.job_dir(job_id)
        reason = (d / "reason").read_text().strip() if (d / "reason").exists() else ""
        exit_file = d / "exit"
        if exit_file.exists():
            text = exit_file.read_text().strip()
            code = int(text) if text.lstrip("-").isdigit() else None
            return JobStatus(
                job_id, False, code, meta["started_at"], exit_file.stat().st_mtime, reason, False
            )
        if _pid_alive(int(meta["pid"])):
            return JobStatus(job_id, True, None, meta["started_at"], None, reason, False)
        return JobStatus(job_id, False, None, meta["started_at"], time.time(), reason, True)

    def running_job_ids(self) -> list[str]:
        ids = []
        for child in self.root.iterdir():
            if (child / "meta.json").exists() and self.status(child.name).running:
                ids.append(child.name)
        return ids

    def cancel(self, job_id: str, grace_seconds: float, reason: str = "cancelled") -> None:
        if not self.status(job_id).running:
            return
        d = self.job_dir(job_id)
        (d / "reason").write_text(reason)
        pgid = self.pgid(job_id)
        _killpg(pgid, signal.SIGTERM)
        deadline = time.time() + grace_seconds
        while time.time() < deadline and self.status(job_id).running:
            time.sleep(0.05)
        if self.status(job_id).running:
            _killpg(pgid, signal.SIGKILL)
            deadline = time.time() + 5
            while time.time() < deadline and self.status(job_id).running:
                time.sleep(0.05)
        if not (d / "exit").exists():
            # The wrapper died with the group and never recorded an exit code.
            (d / "exit").write_text("-1")

    # ---- output --------------------------------------------------------------

    def read_tail(self, job_id: str, stream: str, max_bytes: int = 8192) -> str:
        path = self.log_path(job_id, stream)
        if not path.exists():
            return ""
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", errors="replace")

    def prune(self, older_than_seconds: float) -> int:
        removed = 0
        cutoff = time.time() - older_than_seconds
        for child in list(self.root.iterdir()):
            if not (child / "meta.json").exists():
                continue
            st = self.status(child.name)
            if st.running:
                continue
            if (st.ended_at or 0) <= cutoff:
                shutil.rmtree(child, ignore_errors=True)
                self._procs.pop(child.name, None)
                removed += 1
        return removed

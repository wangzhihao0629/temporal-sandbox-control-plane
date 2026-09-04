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
the privilege boundary, invoked with `--preserve-env` so the job's environment
crosses in the process environment rather than on the command line, where
`/proc/<pid>/cmdline` would expose every resolved secret to any local reader;
the same code runs the tests as the current user. That boundary cuts both ways:
the worker cannot signal what it launched through sudo, so every kill goes back
through sudo as well, and `cancel` refuses to record an exit code while
anything in the group is still alive.
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

from sandbox.contract.errors import Incompatible
from sandbox.contract.types import ExecJob, ExecSpec
from sandbox.timeutil import now_iso

# "$0" is the job directory. The command's exit code is written atomically so a
# reader never sees an empty exit file.
_WRAPPER = '"$@"; rc=$?; printf "%s" "$rc" > "$0/exit.tmp"; mv "$0/exit.tmp" "$0/exit"; exit "$rc"'

_STREAMS = ("stdout", "stderr")

# Resolved from the image, not from PATH: see _wrap and _popen_env.
_SH = "/bin/sh"
_SUDO = "/usr/bin/sudo"
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# How long a SIGKILLed group gets to disappear before cancel calls it a leak.
_KILL_GRACE_SECONDS = 5.0


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
        if stream not in _STREAMS:
            raise ValueError(f"unknown stream {stream!r}; expected one of {_STREAMS}")
        return self.job_dir(job_id) / f"{stream}.log"

    def exists(self, job_id: str) -> bool:
        return (self.job_dir(job_id) / "spec.json").exists()

    def load_spec(self, job_id: str) -> ExecSpec:
        return ExecSpec(**json.loads((self.job_dir(job_id) / "spec.json").read_text()))

    def _meta(self, job_id: str) -> dict | None:
        path = self.job_dir(job_id) / "meta.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _await_meta(self, job_id: str, timeout: float = 2.0) -> dict | None:
        # A concurrent starter may be between mkdir and meta.json. Give it a
        # window before concluding the directory is a crash leftover.
        deadline = time.time() + timeout
        while True:
            meta = self._meta(job_id)
            if meta is not None:
                return meta
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def pgid(self, job_id: str) -> int:
        meta = self._meta(job_id)
        if meta is None:
            raise KeyError(job_id)
        return int(meta["pgid"])

    # ---- lifecycle -----------------------------------------------------------

    def start(self, spec: ExecSpec, env: dict[str, str]) -> ExecJob:
        d = self.job_dir(spec.job_id)
        try:
            d.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            meta = self._await_meta(spec.job_id)
            if meta is not None:
                recorded = meta.get("job_id", spec.job_id)
                if recorded != spec.job_id:
                    raise Incompatible(
                        f"job id {spec.job_id!r} collides with {recorded!r} on disk"
                    ) from None
                return ExecJob(
                    job_id=spec.job_id, vm_id=self.vm_id, started_at=meta["started_at_iso"]
                )
            # No meta after the window: a start died between mkdir and meta.json,
            # so this is a leftover directory and not a job anyone can wait on.
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=False, mode=0o700)

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
            "job_id": spec.job_id,
            "pid": proc.pid,
            "pgid": proc.pid,
            "started_at": started,
            "started_at_iso": now_iso(),
        }
        # Atomically: _await_meta treats a directory without meta.json as a
        # crash leftover and deletes it, so a half-written file must never exist.
        (d / "meta.json.tmp").write_text(json.dumps(meta))
        os.replace(d / "meta.json.tmp", d / "meta.json")
        self._procs[spec.job_id] = proc
        return ExecJob(job_id=spec.job_id, vm_id=self.vm_id, started_at=meta["started_at_iso"])

    def _wrap(self, spec: ExecSpec, env: dict[str, str], d: Path) -> list[str]:
        inner = list(spec.argv)
        if self.run_as_user:
            # The values stay out of argv: sudo carries them over from our own
            # environment, so nothing sensitive lands in /proc/<pid>/cmdline.
            # Absolute paths for the shell and for sudo: the caller supplies part
            # of this environment, and a caller-chosen PATH must never decide
            # which binary crosses the privilege boundary.
            inner = [
                _SUDO,
                "-n",
                "-u",
                self.run_as_user,
                f"--preserve-env={','.join(sorted(env))}",
                "--",
                *inner,
            ]
        return [_SH, "-c", _WRAPPER, str(d), *inner]

    def _popen_env(self, env: dict[str, str]) -> dict[str, str]:
        # The wrapper shell and sudo need a PATH of their own, and it is the
        # agent's, never the caller's: whatever a request asks for, the binaries
        # that run before the privilege drop are resolved against the image.
        # sudo's secure_path resets PATH for the target command anyway.
        return {**env, "PATH": os.environ.get("PATH") or _DEFAULT_PATH}

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
        return JobStatus(job_id, False, None, meta["started_at"], self._mark_lost(d), reason, True)

    def _mark_lost(self, d: Path) -> float:
        # A lost job has no exit file to date it, so the first reader to notice
        # writes the marker and every later reader agrees on when it ended.
        marker = d / "lost"
        if not marker.exists():
            tmp = d / "lost.tmp"
            tmp.write_text(now_iso())
            os.replace(tmp, marker)
        return marker.stat().st_mtime

    def running_job_ids(self) -> list[str]:
        ids = []
        for child in self.root.iterdir():
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            job_id = json.loads(meta_path.read_text()).get("job_id", child.name)
            if self.status(job_id).running:
                ids.append(job_id)
        return ids

    def _signal_group(self, pgid: int, sig: signal.Signals) -> None:
        """Signal the whole job group, on both sides of the privilege boundary.

        In `run_as_user` mode the group is `sh` (ours) -> `sudo` (root) -> the
        command (the job user). `os.killpg` from the worker reaches only the
        `sh`: it may not signal root, and it may not signal another user's
        processes. So the same signal goes back through sudo, where the job
        user is allowed to signal its own. The group id is the `sh` pid, which
        every descendant inherits.
        """
        _killpg(pgid, sig)
        if self.run_as_user:
            subprocess.run(
                [
                    _SUDO,
                    "-n",
                    "-u",
                    self.run_as_user,
                    "--",
                    "kill",
                    f"-{sig.name.removeprefix('SIG')}",
                    "--",
                    f"-{pgid}",
                ],
                check=False,
            )

    def _survivors(self, pgid: int) -> bool:
        """True while anything in the job's process group is still alive."""
        if self.run_as_user:
            return (
                subprocess.run(
                    [_SUDO, "-n", "-u", self.run_as_user, "--", "pgrep", "-g", str(pgid)],
                    check=False,
                    capture_output=True,
                ).returncode
                == 0
            )
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True

    def _await_stopped(self, job_id: str, pgid: int, timeout: float) -> bool:
        # The wrapper's own liveness is not enough: on the far side of sudo the
        # command outlives the shell that spawned it, which is exactly how a
        # cancel used to report success while the job kept running.
        #
        # The delay backs off because the survivor check costs a sudo and a
        # pgrep once the wrapper is gone, and a job that takes its whole grace
        # period to die would otherwise mean hundreds of them.
        deadline = time.time() + timeout
        delay = 0.05
        while True:
            if not self.status(job_id).running and not self._survivors(pgid):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)

    def cancel(self, job_id: str, grace_seconds: float, reason: str = "cancelled") -> None:
        if not self.status(job_id).running:
            return
        d = self.job_dir(job_id)
        (d / "reason").write_text(reason)
        pgid = self.pgid(job_id)
        self._signal_group(pgid, signal.SIGTERM)
        if not self._await_stopped(job_id, pgid, grace_seconds):
            self._signal_group(pgid, signal.SIGKILL)
            if not self._await_stopped(job_id, pgid, _KILL_GRACE_SECONDS):
                # Recording an exit code here would tell the orchestrator the job
                # is over while the command is still writing to the workspace.
                raise RuntimeError(f"job {job_id}: process group {pgid} still has survivors")
        if not (d / "exit").exists():
            # The wrapper died with the group and never recorded an exit code.
            (d / "exit.tmp").write_text("-1")
            os.replace(d / "exit.tmp", d / "exit")

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
            meta_path = child / "meta.json"
            if not meta_path.exists():
                # A crash leftover: nothing can wait on it, so age it out by the
                # directory's own mtime.
                if child.stat().st_mtime <= cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                continue
            job_id = json.loads(meta_path.read_text()).get("job_id", child.name)
            st = self.status(job_id)
            if st.running:
                continue
            if (st.ended_at or 0) <= cutoff:
                shutil.rmtree(child, ignore_errors=True)
                self._procs.pop(job_id, None)
                removed += 1
        return removed

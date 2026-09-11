"""Scenario workflow that exercises the client end to end."""

from dataclasses import dataclass

from temporalio import workflow

from sandbox.client import Sandbox, Timeouts
from sandbox.contract.errors import ExecFailed, LeaseLost, SandboxUnavailable
from sandbox.contract.names import WORKSPACE_ROOT
from sandbox.contract.types import ExecSpec, SandboxSpec


@dataclass
class ExerciseParams:
    scenario: str
    pool: str = "demo"
    sleep_seconds: int = 8
    workspace_root: str = WORKSPACE_ROOT
    hold_seconds: int = 1800


@dataclass
class ExerciseResult:
    outcome: str
    stdout: str = ""
    vm_id: str = ""
    detail: str = ""


@workflow.defn
class ExerciseWorkflow:
    @workflow.run
    async def run(self, p: ExerciseParams) -> ExerciseResult:
        sandbox = Sandbox(Timeouts.test(), workspace_root=p.workspace_root)
        spec = SandboxSpec(
            pool=p.pool, request_id=str(workflow.uuid4()), hold_seconds=p.hold_seconds
        )
        try:
            async with sandbox.lease(spec) as vm:
                cwd = vm.workspace()
                if p.scenario == "echo":
                    result = await vm.exec(
                        ExecSpec(
                            job_id=vm.new_job_id(),
                            argv=["echo", "hi"],
                            cwd=cwd,
                            timeout_seconds=30,
                        )
                    )
                    return ExerciseResult("ok", result.stdout_tail, vm.lease.vm_id)
                if p.scenario == "long":
                    result = await vm.exec(
                        ExecSpec(
                            job_id=vm.new_job_id(),
                            argv=["sh", "-c", f"sleep {p.sleep_seconds}; echo done"],
                            cwd=cwd,
                            timeout_seconds=60,
                        )
                    )
                    return ExerciseResult("ok", result.stdout_tail, vm.lease.vm_id)
                if p.scenario == "failing":
                    await vm.exec(
                        ExecSpec(
                            job_id=vm.new_job_id(),
                            argv=["sh", "-c", "echo bad >&2; exit 2"],
                            cwd=cwd,
                            timeout_seconds=30,
                        )
                    )
                    return ExerciseResult("unexpected")
                if p.scenario == "lost":
                    await vm.exec(
                        ExecSpec(
                            job_id=vm.new_job_id(),
                            argv=["true"],
                            cwd=cwd,
                            timeout_seconds=30,
                        )
                    )
                    return ExerciseResult("unexpected")
                if p.scenario == "raise_inside":
                    raise RuntimeError("caller bug")
                raise RuntimeError(f"unknown scenario {p.scenario}")
        except LeaseLost as e:
            return ExerciseResult("lease_lost", detail=str(e))
        except ExecFailed as e:
            return ExerciseResult(
                "exec_failed", detail=f"exit={e.result.exit_code} {e.result.stderr_tail.strip()}"
            )
        except SandboxUnavailable as e:
            return ExerciseResult("unavailable", detail=str(e))

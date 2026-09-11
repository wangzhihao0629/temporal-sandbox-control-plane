"""The agent that edits code: scripted by default, `claude -p` on request.

What: `run_fake_turn` plays a scenario's steps against the workspace, printing
one line per tool event and sleeping so a turn lasts about `seconds`;
`run_claude_turn` hands the prompt and the last test report to the Claude Code
CLI instead.
Why: the log tail is what the dashboard and the `exec_wait` heartbeats show,
so the fake agent streams the same kind of output a real one does. Fake cost is
a fixed amount per step so the summary can show a spend the way a production
agent's cost events do. A missing edit target is the turn breaking, not a finding.
Production: the real agent harness; the orchestrator does not care which.
"""

import difflib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from sandbox.runner.checks import child_env
from sandbox.runner.scenarios import Scenario

COST_USD = {"read": 0.002, "think": 0.010, "edit": 0.020, "run": 0.005}


@dataclass
class TurnOutcome:
    ok: bool
    summary: str = ""
    files_changed: int = 0
    fake_cost_usd: float = 0.0
    error: str = ""


def apply_edit(path: Path, old: str, new: str) -> tuple[int, int] | None:
    """Replace `old` with `new` once; None when `new` is already in place.

    "Already applied" is checked first: a repeated turn whose `new` text
    contains its `old` text (adding a function after an existing one) would
    otherwise apply twice and duplicate the function.
    """
    before = path.read_text() if path.exists() else ""
    if new in before:
        return None
    if old not in before:
        raise LookupError(f"edit target not found in {path.name}")
    after = before.replace(old, new, 1)
    path.write_text(after)
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    body = [line for line in diff if not line.startswith(("+++", "---", "@@"))]
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    return added, removed


def describe_feedback(feedback: dict | None) -> list[str]:
    if not feedback or not feedback.get("failed"):
        return []
    lines = [f"[feedback] {feedback['failed']} failing test(s) from the last turn:"]
    for failure in feedback.get("failures", [])[:5]:
        first = (failure.get("message") or "").splitlines()[:1]
        lines.append(f"[feedback]   {failure.get('test', '?')}: {first[0] if first else ''}")
    return lines


def run_fake_turn(
    workspace: Path,
    scenario: Scenario,
    turn: int,
    seconds: float,
    feedback: dict | None,
    python: str = sys.executable,
    out=sys.stdout,
) -> TurnOutcome:
    script = scenario.turn_for(turn)
    pause = seconds / len(script.steps) if script.steps else 0.0
    changed: set[str] = set()
    cost = 0.0
    for line in describe_feedback(feedback):
        print(line, file=out, flush=True)
    for step in script.steps:
        cost += COST_USD.get(step.kind, 0.0)
        if step.kind == "read":
            path = workspace / step.target
            count = len(path.read_text().splitlines()) if path.exists() else 0
            print(f"[tool] Read {step.target} ({count} lines)", file=out, flush=True)
        elif step.kind == "think":
            print(f"[think] {step.text}", file=out, flush=True)
        elif step.kind == "edit":
            new = step.new.replace("{turn}", str(turn))
            try:
                stat = apply_edit(workspace / step.target, step.old, new)
            except LookupError as e:
                return TurnOutcome(ok=False, error=str(e), fake_cost_usd=round(cost, 4))
            if stat is None:
                print(f"[tool] Edit {step.target} (already applied)", file=out, flush=True)
            else:
                changed.add(step.target)
                print(f"[tool] Edit {step.target} (+{stat[0]} -{stat[1]})", file=out, flush=True)
        elif step.kind == "run":
            proc = subprocess.run(
                [python, "-m", *step.argv],
                cwd=workspace,
                env=child_env(),
                capture_output=True,
                text=True,
            )
            tail = (proc.stdout.strip().splitlines() or [""])[-1]
            print(
                f"[tool] Run python -m {' '.join(step.argv)} (exit {proc.returncode}): {tail}",
                file=out,
                flush=True,
            )
        else:
            return TurnOutcome(ok=False, error=f"unknown step kind {step.kind!r}")
        if pause:
            time.sleep(pause)
    return TurnOutcome(
        ok=True,
        summary=script.summary,
        files_changed=len(changed),
        fake_cost_usd=round(cost, 4),
    )


def run_claude_turn(
    workspace: Path, prompt: str, feedback: dict | None, seconds: float, out=sys.stdout
) -> TurnOutcome:
    if shutil.which("claude") is None:
        return TurnOutcome(
            ok=False,
            error="claude CLI is not installed in this image; build the claude layer or use "
            "--agent fake",
        )
    text = prompt
    if feedback and feedback.get("failed"):
        text += "\n\nThe last test run failed:\n" + "\n".join(describe_feedback(feedback))
    proc = subprocess.run(
        ["claude", "-p", text, "--permission-mode", "acceptEdits"],
        cwd=workspace,
        env=child_env(),
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.strip().splitlines()[-20:]:
        print(f"[claude] {line}", file=out, flush=True)
    if proc.returncode != 0:
        return TurnOutcome(ok=False, error=f"claude exited {proc.returncode}: {proc.stderr[-500:]}")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True
    )
    changed = len([line for line in status.stdout.splitlines() if line.strip()])
    summary = (proc.stdout.strip().splitlines() or ["claude turn"])[0][:72]
    return TurnOutcome(ok=True, summary=summary, files_changed=changed, fake_cost_usd=0.0)

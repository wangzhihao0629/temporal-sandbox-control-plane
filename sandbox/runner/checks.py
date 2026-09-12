"""Lint and test as data.

What: run ruff and pytest in a workspace and turn their machine-readable output
into bounded envelopes.
Why: a failing test is the normal case in a coding session, not an error, so
these never raise on findings. Exit codes other than "ran, found things" mean
the tool itself broke, and that is reported as ok=False. The child environment
drops PYTEST_* and PYTHONPATH so the workspace's tools see a clean interpreter
even when this runs under pytest or under the artifact launcher, and disables
bytecode caching so an edit between two runs in the same workspace can't be
shadowed by a stale __pycache__ entry.
Production: identical; the real harness reads the same envelopes.
"""

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from sandbox.runner.envelopes import (
    MAX_FAILURES,
    MAX_FINDINGS,
    LintEnvelope,
    LintFinding,
    TestEnvelope,
    TestFailure,
    truncate,
)


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.pop("PYTHONPATH", None)
    # Back-to-back turns edit and re-run the same workspace fast enough that a
    # stale __pycache__ entry can outlive the source edit that invalidates it.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _tail(text: str, lines: int = 20) -> str:
    return truncate("\n".join(text.strip().splitlines()[-lines:]))


def parse_junit(text: str) -> TestEnvelope:
    root = ET.fromstring(text)
    total = passed = failed = 0
    failures: list[TestFailure] = []
    for case in root.iter("testcase"):
        total += 1
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            failed += 1
            failures.append(
                TestFailure(
                    test=f"{case.get('classname', '')}::{case.get('name', '')}",
                    message=truncate(problem.get("message") or (problem.text or "").strip()),
                )
            )
        elif case.find("skipped") is None:
            passed += 1
    return TestEnvelope(
        ok=True, passed=passed, failed=failed, total=total, failures=failures[:MAX_FAILURES]
    )


def run_pytest(workspace: Path, python: str = sys.executable) -> TestEnvelope:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", f"--junitxml={xml}"],
            cwd=workspace,
            env=child_env(),
            capture_output=True,
            text=True,
        )
        # 0: all passed, 1: some failed. Anything else is pytest itself failing.
        if proc.returncode not in (0, 1) or not xml.exists():
            return TestEnvelope(ok=False, error=_tail(proc.stdout + "\n" + proc.stderr))
        return parse_junit(xml.read_text())


def run_ruff(workspace: Path, python: str = sys.executable) -> LintEnvelope:
    proc = subprocess.run(
        [python, "-m", "ruff", "check", "--output-format", "json", "."],
        cwd=workspace,
        env=child_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        return LintEnvelope(ok=False, error=_tail(proc.stdout + "\n" + proc.stderr))
    findings = [
        LintFinding(
            file=os.path.relpath(item["filename"], workspace),
            line=int(item["location"]["row"]),
            code=item["code"],
            message=truncate(item["message"]),
        )
        for item in json.loads(proc.stdout or "[]")
    ]
    return LintEnvelope(ok=True, count=len(findings), findings=findings[:MAX_FINDINGS])

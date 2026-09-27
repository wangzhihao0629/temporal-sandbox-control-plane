"""Start a CodingSessionDemoWorkflow from the command line and print the result.

What: connect to Temporal, start the session with the runner artifact named in
.env, print the Temporal UI link, wait, print the result and the summary.
Why: `make session` and `make demo` are how a reader runs the demo; the link is
what they watch while it runs.
Production: a CLI or UI action that starts a workflow.

Usage: uv run python -m sandbox.cli.session [--scenario NAME]
       [--prompt TEXT] [--max-turns N] [--turn-seconds N] [--agent fake|claude]
       [--session-id ID]
"""

import argparse
import asyncio
import os
import sys
import uuid

from sandbox import envfile
from sandbox.cli import common
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE
from sandbox.objectstore import ObjectStore
from sandbox.orchestrator.workflows import CodingSessionDemoWorkflow, CodingSessionParams
from sandbox.runner.scenarios import SCENARIOS


def _args(argv):
    p = argparse.ArgumentParser(prog="session")
    p.add_argument("--scenario", default=os.environ.get("SCENARIO", ""), choices=("", *SCENARIOS))
    p.add_argument("--prompt", default="Add a multiply function to calc with a test")
    p.add_argument("--max-turns", type=int, default=3)
    p.add_argument("--turn-seconds", type=int, default=30)
    p.add_argument("--turn-timeout", type=int, default=3600, help="seconds before a turn is killed")
    p.add_argument("--step-timeout", type=int, default=600, help="the same for the other steps")
    p.add_argument("--agent", default="fake", choices=("fake", "claude"))
    p.add_argument("--session-id", default=f"demo-{uuid.uuid4().hex[:8]}")
    return p.parse_args(argv)


async def main(argv=None) -> int:
    envfile.load()
    args = _args(sys.argv[1:] if argv is None else argv)
    runner_uri, runner_sha = common.runner_artifact()
    client = await common.connect()
    workflow_id = f"session-{args.session_id}"
    params = CodingSessionParams(
        session_id=args.session_id,
        runner_uri=runner_uri,
        runner_sha256=runner_sha,
        prompt=args.prompt,
        scenario=args.scenario,
        max_turns=args.max_turns,
        turn_seconds=args.turn_seconds,
        turn_timeout_seconds=args.turn_timeout,
        step_timeout_seconds=args.step_timeout,
        agent=args.agent,
        **common.placement(),
    )
    print(f"session  {args.session_id}  scenario {args.scenario or '(from prompt)'}")
    print(f"watch:   {common.watch_url(workflow_id)}")
    result = await client.execute_workflow(
        CodingSessionDemoWorkflow.run, params, id=workflow_id, task_queue=ORCHESTRATOR_TASK_QUEUE
    )
    if result.tests_passed:
        verdict = "tests pass"
    else:
        verdict = f"{result.tests_failed} test(s) still failing"
    print(f"done:    {result.turns} turn(s), {verdict}, {result.lint_findings} lint finding(s)")
    print(f"vms:     {', '.join(result.vm_ids)}  ({result.attempts} lease attempt(s))")
    print(f"cost:    ${result.fake_cost_usd:.4f} (fake)")
    print(f"patch:   {result.patch_uri}")
    print(f"summary: {result.summary_uri}")
    summary = ObjectStore.from_env().get_json(result.summary_uri)
    print(f"         finished_at {summary['finished_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Start a GoBuildDemoWorkflow from the command line and print the result.

What: connect to Temporal, start the Go build demo with the runner artifact
named in .env, print the Temporal UI link, wait, and print both runs' output.
Why: `make gobuild` is how a reader runs the demo; the two output lines are
the thing to compare.
Production: a CLI or UI action that starts a workflow.

Usage: uv run python -m sandbox.orchestrator.run_gobuild [--arg ARG ...]
       [--repo-url URL] [--ref REF] [--session-id ID]
"""

import argparse
import asyncio
import os
import sys
import uuid

from temporalio.client import Client

from sandbox import envfile
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE, WORKSPACE_ROOT
from sandbox.orchestrator.gobuild_workflow import (
    DEFAULT_REF,
    DEFAULT_REPO_URL,
    GoBuildDemoWorkflow,
    GoBuildParams,
)


def _args(argv):
    p = argparse.ArgumentParser(prog="run_gobuild")
    p.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    p.add_argument("--ref", default=DEFAULT_REF)
    p.add_argument("--arg", action="append", default=[], help="passed to the built program")
    p.add_argument("--session-id", default=f"go-{uuid.uuid4().hex[:8]}")
    return p.parse_args(argv)


async def main(argv=None) -> int:
    envfile.load()
    args = _args(sys.argv[1:] if argv is None else argv)
    runner_uri = os.environ.get("RUNNER_URI", "")
    runner_sha = os.environ.get("RUNNER_SHA256", "")
    if not runner_uri or not runner_sha:
        print("RUNNER_URI / RUNNER_SHA256 missing from .env: run `make artifact` first",
              file=sys.stderr)
        return 2
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"), namespace=namespace
    )
    ui = os.environ.get("TEMPORAL_UI", "http://localhost:8233")
    workflow_id = f"gobuild-{args.session_id}"
    params = GoBuildParams(
        session_id=args.session_id,
        runner_uri=runner_uri,
        runner_sha256=runner_sha,
        repo_url=args.repo_url,
        ref=args.ref,
        run_args=args.arg,
        pool=os.environ.get("SANDBOX_POOL", "demo"),
        profile=os.environ.get("SANDBOX_PROFILE", "local"),
        workspace_root=os.environ.get("SANDBOX_WORKSPACE_ROOT", WORKSPACE_ROOT),
    )
    print(f"gobuild  {args.session_id}  {args.repo_url}@{args.ref[:12]}")
    print(f"watch:   {ui}/namespaces/{namespace}/workflows/{workflow_id}")
    r = await client.execute_workflow(
        GoBuildDemoWorkflow.run, params, id=workflow_id, task_queue=ORCHESTRATOR_TASK_QUEUE
    )
    print(f"built:   {r.build_vm}  head {r.head[:12]}  {r.go_version}  {r.binary_bytes} bytes")
    print(f"ran:     {r.output}")
    print(f"snap:    {r.snapshot_uri}  ({r.snapshot_files} files, {r.snapshot_bytes} bytes)")
    print(f"restore: {r.restore_vm}")
    if r.lost_vms:
        print(f"lost:    {', '.join(r.lost_vms)}  (build attempts {r.build_attempts}, "
              f"restore attempts {r.restore_attempts})")
    print(f"ran:     {r.restored_output}")
    same = r.output == r.restored_output
    print(f"match:   {'yes' if same else 'NO'}")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

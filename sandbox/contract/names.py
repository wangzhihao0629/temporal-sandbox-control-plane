"""Names that cross the boundary: activities, task queues, paths.

What: string constants and the two helpers that build them.
Why: a typo in an activity name is a silent hang, not an error. Keeping every
name here means the VM agent registers and the client calls the same strings.
Production: identical.
"""

import re

from sandbox.contract.version import CONTRACT_MAJOR

MANAGER_TASK_QUEUE = "sandbox-manager-queue"
ORCHESTRATOR_TASK_QUEUE = "orchestrator-queue"
VM_TASK_QUEUE_PREFIX = "sandbox-vm-"
WORKSPACE_ROOT = "/private/tmp/sandbox"
ARTIFACTS_ROOT = "/var/lib/sandbox/artifacts"


def vm_task_queue(vm_id: str) -> str:
    return f"{VM_TASK_QUEUE_PREFIX}{vm_id}"


def activity_name(op: str, major: int = CONTRACT_MAJOR) -> str:
    return f"sandbox.v{major}.{op}"


def sanitize_id(value: str) -> str:
    """Make a workflow id safe to use as a directory name."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


ACQUIRE = activity_name("acquire")
RELEASE = activity_name("release")
EXEC_START = activity_name("exec_start")
EXEC_WAIT = activity_name("exec_wait")
EXEC_CANCEL = activity_name("exec_cancel")
PUT_FILE = activity_name("put_file")
GET_FILE = activity_name("get_file")
ENSURE_ARTIFACT = activity_name("ensure_artifact")
DESCRIBE = activity_name("describe")

VM_OPERATIONS = (EXEC_START, EXEC_WAIT, EXEC_CANCEL, PUT_FILE, GET_FILE, ENSURE_ARTIFACT, DESCRIBE)

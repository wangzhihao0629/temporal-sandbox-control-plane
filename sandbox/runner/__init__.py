"""The turn runner: the code the orchestrator ships to a VM.

What: a CLI with five subcommands (clone, turn, lint, test, export), each
writing a JSON envelope to the object store, plus the fake agent it drives.
Why: the VM agent knows how to run commands; it knows nothing about coding
sessions. Everything session-shaped lives here, versioned by the artifact the
orchestrator asks for, so the orchestrator decides which runner runs.
Production: a per-commit virtualenv holding the real agent harness; the
subcommand shape and the envelopes are the same.
"""

RUNNER_VERSION = "0.1.0"

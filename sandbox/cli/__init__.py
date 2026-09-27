"""Command-line entry points: what the Makefile's demo and operator targets run.

What: one module per target (`make smoke` runs `sandbox.cli.smoke`, and so on).
Each parses its arguments, starts a workflow or calls into the manager, and
prints the result.
Why: kept out of the packages they drive, so the system's own logic reads
without scripts in the way, and so no server imports a CLI.
Production: operators use the Temporal UI, the dashboard, or their own tooling.
"""

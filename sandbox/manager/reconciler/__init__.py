"""The reconciler: the loop that keeps each pool's fleet the right size and healthy.

What: `core.py` is the pure logic, one pass of five steps (health, leases,
capacity, requests, sample) against the registry and the provider;
`workflow.py` runs a pass as `ReconcileWorkflow`; `activities.py` are the
activities it calls; `schedule.py` keeps a Temporal Schedule running it every
interval; `types.py` holds the values passed between them.
Why: every decision is in `core.py`, so it can be tested without Temporal and
read in one place; the rest is how Temporal runs it.
Production: the same, on the deployed manager worker.
"""

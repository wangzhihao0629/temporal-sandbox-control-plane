"""The registry: source of truth for VM state, leases, jobs, events, requests.

What: four DynamoDB tables and one client class over them.
Why: the sandbox manager needs strongly consistent conditional writes to claim
a VM exactly once, and the VM agent needs to write its own row. Temporal is the
transport, not the state store.
Production: DynamoDB with IAM scoping each VM agent to its own partition key.
Locally: DynamoDB Local, same code, different endpoint.
"""

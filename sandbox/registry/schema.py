"""Table definitions.

What: names, keys, indexes, and an idempotent create.
Why: the schema is part of the contract between the manager, the VM agent, and
the dashboard, so it lives in one file they all import.
Production: created by Terraform; this function is for local and tests.
"""

VMS_TABLE = "sandbox_vms"
JOBS_TABLE = "sandbox_jobs"
EVENTS_TABLE = "sandbox_events"
REQUESTS_TABLE = "sandbox_requests"

POOL_ITEM_PREFIX = "pool#"

STATES = (
    "booting",
    "idle",
    "leased",
    "recycling",
    "draining",
    "terminating",
    "dead",
    "terminated",
)

_TABLES = {
    VMS_TABLE: dict(
        KeySchema=[{"AttributeName": "vm_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "vm_id", "AttributeType": "S"},
            {"AttributeName": "pool", "AttributeType": "S"},
            {"AttributeName": "state", "AttributeType": "S"},
            {"AttributeName": "lease_request_id", "AttributeType": "S"},
            {"AttributeName": "lease_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "pool_state_index",
                "KeySchema": [
                    {"AttributeName": "pool", "KeyType": "HASH"},
                    {"AttributeName": "state", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "lease_request_index",
                "KeySchema": [{"AttributeName": "lease_request_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "lease_index",
                "KeySchema": [{"AttributeName": "lease_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    ),
    JOBS_TABLE: dict(
        KeySchema=[
            {"AttributeName": "vm_id", "KeyType": "HASH"},
            {"AttributeName": "job_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "vm_id", "AttributeType": "S"},
            {"AttributeName": "job_id", "AttributeType": "S"},
        ],
    ),
    EVENTS_TABLE: dict(
        KeySchema=[
            {"AttributeName": "day", "KeyType": "HASH"},
            {"AttributeName": "ts_ulid", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "day", "AttributeType": "S"},
            {"AttributeName": "ts_ulid", "AttributeType": "S"},
        ],
    ),
    REQUESTS_TABLE: dict(
        KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
    ),
}


def create_tables(resource) -> list[str]:
    """Create any missing table. Returns the names created."""
    existing = {t.name for t in resource.tables.all()}
    created: list[str] = []
    for name, spec in _TABLES.items():
        if name in existing:
            continue
        table = resource.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", **spec)
        table.wait_until_exists()
        created.append(name)
    return created

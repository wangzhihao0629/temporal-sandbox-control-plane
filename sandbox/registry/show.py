"""Print VM rows and recent events. The dashboard before there is a dashboard.

Usage: uv run python -m sandbox.registry.show
"""

from sandbox import envfile
from sandbox.registry.client import Registry
from sandbox.timeutil import now, parse_iso


def main() -> None:
    envfile.load()
    registry = Registry.from_env()
    rows = registry.list_vms()
    print(f"{'VM':<16}{'STATE':<12}{'HB AGE':<8}{'PROT':<6}{'LEASE':<34}OWNER")
    for row in rows:
        age = int((now() - parse_iso(row["last_heartbeat_at"])).total_seconds())
        print(
            f"{row['vm_id']:<16}{row['state']:<12}{age:<8}{str(row.get('protected', False)):<6}"
            f"{row.get('lease_id', '-'):<34}{row.get('owner_workflow_id', '-')}"
        )
    if not rows:
        print("(no VMs registered)")
    print("\nrecent events:")
    for event in registry.recent_events(15):
        print(
            f"  {event['ts_ulid'][:19]}  {event['actor']:<10} {event['type']:<10} "
            f"{event.get('vm_id', ''):<16} {event['message']}"
        )


if __name__ == "__main__":
    main()

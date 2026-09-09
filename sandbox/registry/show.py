"""Print VM rows and recent events.

What: one table of every registered VM and a tail of the event stream.
Why: the registry is the system's real state, and being able to read it in a
terminal is what makes a lease, a wipe, or a stuck row explainable without the
Temporal UI. It is the dashboard before there is a dashboard.
Production: the status API serves the same join; this stays as the fallback for
when the API is what is broken.

Usage: uv run python -m sandbox.registry.show
"""

from sandbox import envfile
from sandbox.registry.client import Registry
from sandbox.timeutil import now, parse_iso


def main() -> None:
    envfile.load()
    registry = Registry.from_env()
    for policy in registry.list_pools():
        print(
            f"pool {policy['pool']}: floor {policy['min_idle']}, max {policy['max']}, "
            f"image {policy['image']}"
        )
    sample = registry.latest_fleet_sample()
    if sample:
        d = sample["details"]
        print(
            f"fleet @ {sample['ts_ulid'][:19]}: total {d.get('total')} idle {d.get('idle')} "
            f"leased {d.get('leased')} booting {d.get('booting')} draining {d.get('draining')} "
            f"dead {d.get('dead')} pending {d.get('pending')}"
        )
    print()
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

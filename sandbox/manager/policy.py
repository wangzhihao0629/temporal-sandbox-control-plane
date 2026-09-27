"""Pool policy.

What: the per-pool knobs the reconciler reads: floor of idle VMs, ceiling of
total VMs, and the launch spec.
Why: capacity is a decision the operator makes, stored where the reconciler
and the dashboard can both read it, not a constant in code.
Production: the same item, edited by the dashboard's policy endpoint.
"""

from dataclasses import asdict, dataclass

from sandbox.manager.providers.base import LaunchSpec


@dataclass(frozen=True)
class PoolPolicy:
    pool: str
    min_idle: int
    max: int
    image: str
    cpus: int = 2
    memory: str = "2048M"

    @classmethod
    def from_row(cls, row: dict) -> "PoolPolicy":
        return cls(
            pool=row["pool"],
            min_idle=int(row["min_idle"]),
            max=int(row["max"]),
            image=row["image"],
            cpus=int(row.get("cpus", 2)),
            memory=row.get("memory", "2048M"),
        )

    def to_fields(self) -> dict:
        fields = asdict(self)
        fields.pop("pool")
        return fields

    def launch_spec(self) -> LaunchSpec:
        return LaunchSpec(image=self.image, cpus=self.cpus, memory=self.memory)


def merge_policy(current: dict, min_idle, max, image) -> dict:
    """The stored policy with any given knob replaced; refuses a floor above the ceiling.

    Shared by `make policy` and the dashboard's policy endpoint, so both enforce
    the same rule.
    """
    merged = {
        "min_idle": int(min_idle) if min_idle is not None else int(current["min_idle"]),
        "max": int(max) if max is not None else int(current["max"]),
        "image": image if image is not None else current["image"],
        "cpus": int(current.get("cpus", 2)),
        "memory": current.get("memory", "2048M"),
    }
    if merged["min_idle"] > merged["max"]:
        raise ValueError(f"min_idle {merged['min_idle']} exceeds max {merged['max']}")
    return merged

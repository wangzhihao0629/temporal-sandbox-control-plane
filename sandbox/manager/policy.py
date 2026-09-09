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

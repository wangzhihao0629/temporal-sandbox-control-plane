"""UTC time helpers.

What: one way to produce and parse timestamps.
Why: every timestamp on the wire and in the registry is an ISO 8601 UTC string
ending in Z, so comparisons sort lexicographically and no reader guesses a zone.
Production: identical.
"""

from datetime import UTC, datetime, timedelta


def now() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return to_iso(now())


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def epoch_in(seconds: float) -> int:
    return int((now() + timedelta(seconds=seconds)).timestamp())

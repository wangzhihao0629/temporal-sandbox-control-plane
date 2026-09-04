"""Timestamps: UTC ISO strings in, aware datetimes and TTL epochs out."""

from datetime import UTC, timedelta

from sandbox import timeutil


def test_now_iso_is_utc_with_z_suffix():
    value = timeutil.now_iso()
    assert value.endswith("Z")
    parsed = timeutil.parse_iso(value)
    assert parsed.tzinfo == UTC


def test_parse_iso_round_trips_and_epoch_in_is_in_the_future():
    dt = timeutil.now()
    assert timeutil.parse_iso(timeutil.to_iso(dt)) - dt < timedelta(seconds=1)
    assert timeutil.epoch_in(60) > int(dt.timestamp())

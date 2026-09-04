"""Timeout profiles: the exact durations local, prod, and test hand to Temporal."""

from datetime import timedelta

import pytest

from sandbox.client import Timeouts

PROFILES = {
    "local": {
        "schedule_to_start": timedelta(seconds=30),
        "heartbeat": timedelta(seconds=30),
        "acquire_retry_delay": timedelta(seconds=5),
        "acquire_wait": timedelta(minutes=10),
        "wait_slack": timedelta(minutes=5),
    },
    "prod": {
        "schedule_to_start": timedelta(minutes=5),
        "heartbeat": timedelta(minutes=2),
        "acquire_retry_delay": timedelta(seconds=30),
        "acquire_wait": timedelta(minutes=10),
        "wait_slack": timedelta(minutes=5),
    },
    "test": {
        "schedule_to_start": timedelta(seconds=3),
        "heartbeat": timedelta(seconds=5),
        "acquire_retry_delay": timedelta(milliseconds=500),
        "acquire_wait": timedelta(seconds=5),
        "wait_slack": timedelta(seconds=10),
    },
}


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_for_profile_returns_the_documented_durations(name):
    profile = Timeouts.for_profile(name)
    assert {field: getattr(profile, field) for field in PROFILES[name]} == PROFILES[name]


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_for_profile_matches_the_named_constructor(name):
    assert Timeouts.for_profile(name) == getattr(Timeouts, name)()


def test_an_unknown_profile_raises():
    with pytest.raises(KeyError):
        Timeouts.for_profile("staging")

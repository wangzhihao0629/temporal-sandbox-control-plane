"""policy_cli: merging flags into the stored pool policy."""

from sandbox.manager.policy_cli import merge_policy


def test_merge_keeps_unspecified_fields_and_casts_numbers():
    current = {"pool": "demo", "min_idle": 2, "max": 5, "image": "a", "cpus": 2, "memory": "2048M"}
    merged = merge_policy(current, min_idle=3, max=None, image=None)
    assert merged == {"min_idle": 3, "max": 5, "image": "a", "cpus": 2, "memory": "2048M"}


def test_merge_rejects_a_floor_above_the_ceiling():
    current = {"pool": "demo", "min_idle": 2, "max": 5, "image": "a", "cpus": 2, "memory": "2048M"}
    try:
        merge_policy(current, min_idle=6, max=None, image=None)
    except ValueError as e:
        assert "min_idle" in str(e)
    else:
        raise AssertionError("expected ValueError")

"""Contract version.

What: the single place the wire contract's version lives.
Why: activity names carry the major so a VM agent can serve two majors during a
rollout. Minors are additive, new fields with defaults, and need no rename.
Production: identical. The registry stores each VM's supported majors and
acquire filters on the client's.
"""

CONTRACT_MAJOR = 1
CONTRACT_MINOR = 1
CONTRACT_VERSION = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"
SUPPORTED_MAJORS: tuple[int, ...] = (CONTRACT_MAJOR,)

"""Envelopes: what a runner step reports back.

What: one frozen dataclass per subcommand, a `kind` and `ok` on every one,
caps on lists and messages, and `broken()` for a step that raised.
Why: the orchestrator reads these through an activity, so they are the runner's
contract with the workflow. Caps keep any envelope far below Temporal's payload
limit no matter how many tests fail. The module imports nothing beyond the
standard library because the workflow imports it too.
Production: identical.
"""

from dataclasses import asdict, dataclass, field

MAX_FAILURES = 20
MAX_FINDINGS = 50
MAX_MESSAGE = 500
KINDS = ("clone", "turn", "lint", "test", "export")


def truncate(text: str, limit: int = MAX_MESSAGE) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Envelope:
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CloneEnvelope(_Envelope):
    ok: bool
    source: str = ""
    head: str = ""
    turn: int = 0
    error: str = ""
    kind: str = "clone"


@dataclass(frozen=True)
class TurnEnvelope(_Envelope):
    ok: bool
    turn: int = 0
    commit: str = ""
    files_changed: int = 0
    summary: str = ""
    feedback_used: bool = False
    fake_cost_usd: float = 0.0
    scenario: str = ""
    error: str = ""
    kind: str = "turn"


@dataclass(frozen=True)
class LintFinding:
    file: str
    line: int
    code: str
    message: str


@dataclass(frozen=True)
class LintEnvelope(_Envelope):
    ok: bool
    count: int = 0
    findings: list[LintFinding] = field(default_factory=list)
    error: str = ""
    kind: str = "lint"


@dataclass(frozen=True)
class TestFailure:
    test: str
    message: str


@dataclass(frozen=True)
class TestEnvelope(_Envelope):
    ok: bool
    passed: int = 0
    failed: int = 0
    total: int = 0
    failures: list[TestFailure] = field(default_factory=list)
    error: str = ""
    kind: str = "test"


@dataclass(frozen=True)
class ExportEnvelope(_Envelope):
    ok: bool
    commits: int = 0
    bytes: int = 0
    error: str = ""
    kind: str = "export"


_BY_KIND = {
    "clone": CloneEnvelope,
    "turn": TurnEnvelope,
    "lint": LintEnvelope,
    "test": TestEnvelope,
    "export": ExportEnvelope,
}


def broken(kind: str, error: str):
    """The envelope a step writes when it raised instead of finishing."""
    return _BY_KIND[kind](ok=False, error=truncate(error))

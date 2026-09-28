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
KINDS = ("clone", "turn", "lint", "test", "export", "fetch", "edit", "build", "run")


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
    scenario: str = ""
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


@dataclass(frozen=True)
class FetchEnvelope(_Envelope):
    ok: bool
    url: str = ""
    ref: str = ""
    head: str = ""
    error: str = ""
    kind: str = "fetch"


@dataclass(frozen=True)
class EditEnvelope(_Envelope):
    ok: bool
    edit: str = ""
    file: str = ""
    added: int = 0
    removed: int = 0
    already_applied: bool = False
    error: str = ""
    kind: str = "edit"


@dataclass(frozen=True)
class BuildEnvelope(_Envelope):
    ok: bool
    binary: str = ""
    bytes: int = 0
    go_version: str = ""
    seconds: float = 0.0
    error: str = ""
    kind: str = "build"


@dataclass(frozen=True)
class RunEnvelope(_Envelope):
    ok: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    kind: str = "run"


_BY_KIND = {
    "clone": CloneEnvelope,
    "turn": TurnEnvelope,
    "lint": LintEnvelope,
    "test": TestEnvelope,
    "export": ExportEnvelope,
    "fetch": FetchEnvelope,
    "edit": EditEnvelope,
    "build": BuildEnvelope,
    "run": RunEnvelope,
}


def describe(envelope) -> list[str]:
    """The envelope as indented lines for the job's own log.

    The envelope goes to the object store for the workflow to read; these lines
    go to stdout, so whoever opens the job's log — on the dashboard, or in the
    object store — sees what the step found without fetching the JSON.
    """
    fields = envelope.to_dict()
    if not fields["ok"]:
        # A step that broke has only its error to report; the rest are defaults.
        fields = {"error": fields["error"]}
    lines = []
    for key, value in fields.items():
        if key in ("ok", "kind") or value in ("", [], None):
            continue
        if isinstance(value, list):
            lines.append(f"  {key}:")
            for item in value:
                fields = item if isinstance(item, dict) else {"value": item}
                first = True
                for field, text in fields.items():
                    for i, line in enumerate(str(text).splitlines() or [""]):
                        prefix = "    - " if first else "      "
                        label = f"{field}: " if i == 0 else " " * (len(field) + 2)
                        lines.append(f"{prefix}{label}{line}")
                        first = False
        else:
            text = str(value).splitlines() or [""]
            lines.append(f"  {key}: {text[0]}")
            lines += [f"    {line}" for line in text[1:]]
    return lines


def broken(kind: str, error: str):
    """The envelope a step writes when it raised instead of finishing."""
    return _BY_KIND[kind](ok=False, error=truncate(error))

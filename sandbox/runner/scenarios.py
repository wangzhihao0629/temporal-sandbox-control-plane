"""Scripted turns for the fake coding agent.

What: four scenarios, each a tuple of turns, each turn a tuple of tool steps
(read, think, edit, run) with the exact edits to make and what lint and the
tests will say afterwards.
Why: a deterministic agent makes the demo repeatable and lets a unit test
assert what every turn does to the seed. Edits are old-text -> new-text
replacements, the shape of a coding agent's Edit tool, so the log shows real
diff stats. Past the end of a script the last turn repeats, which is how
`never-fixes` exhausts `max_turns`.
Production: replaced by the real agent; the runner around it does not change.
"""

from dataclasses import dataclass

DEFAULT_SCENARIO = "multiply-with-bug"


@dataclass(frozen=True)
class Step:
    kind: str  # read | think | edit | run
    target: str = ""
    text: str = ""
    old: str = ""
    new: str = ""  # may contain {turn}, replaced with the turn number
    argv: tuple[str, ...] = ()  # run: module and args for `python -m`


@dataclass(frozen=True)
class Turn:
    summary: str
    steps: tuple[Step, ...]
    tests_pass: bool
    lint_findings: int


@dataclass(frozen=True)
class Scenario:
    name: str
    keywords: tuple[str, ...]
    teaches: str
    turns: tuple[Turn, ...]

    def turn_for(self, n: int) -> Turn:
        """Turn n, 1-based; past the script's end the last turn repeats."""
        return self.turns[min(n, len(self.turns)) - 1]


DOCSTRING = '"""A calculator small enough to read in a minute."""\n'
SUBTRACT = "def subtract(a: float, b: float) -> float:\n    return a - b\n"
IMPORT = "from calc import add, subtract\n"
TEST_SUBTRACT = "def test_subtract():\n    assert subtract(5, 3) == 2\n"
MULTIPLY_BUGGY = "def multiply(a: float, b: float) -> float:\n    return a + b\n"
MULTIPLY_FIXED = "def multiply(a: float, b: float) -> float:\n    return a * b\n"
RUN_TESTS = Step("run", argv=("pytest", "-q"))

ADD_MULTIPLY = Turn(
    summary="add multiply and a test for it",
    steps=(
        Step("read", target="calc/__init__.py"),
        Step("think", text="The prompt wants multiply. Add it next to subtract and test it."),
        Step(
            "edit",
            target="calc/__init__.py",
            old=SUBTRACT,
            new=SUBTRACT + "\n\n" + MULTIPLY_BUGGY,
        ),
        Step(
            "edit",
            target="tests/test_calc.py",
            old=IMPORT,
            new="from calc import add, multiply, subtract\n",
        ),
        Step(
            "edit",
            target="tests/test_calc.py",
            old=TEST_SUBTRACT,
            new=TEST_SUBTRACT + "\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n",
        ),
        RUN_TESTS,
    ),
    tests_pass=False,
    lint_findings=0,
)

FIX_MULTIPLY = Turn(
    summary="fix the operator in multiply",
    steps=(
        Step("think", text="test_multiply expected 6 and got 5: multiply is adding. Use *."),
        Step("edit", target="calc/__init__.py", old=MULTIPLY_BUGGY, new=MULTIPLY_FIXED),
        RUN_TESTS,
    ),
    tests_pass=True,
    lint_findings=0,
)

DIVIDE = Turn(
    summary="add divide with a zero guard and tests",
    steps=(
        Step("read", target="calc/__init__.py"),
        Step("think", text="divide needs a guard: dividing by zero raises a clear error."),
        Step(
            "edit",
            target="calc/__init__.py",
            old=SUBTRACT,
            new=SUBTRACT
            + "\n\ndef divide(a: float, b: float) -> float:\n"
            + "    if b == 0:\n"
            + '        raise ZeroDivisionError("cannot divide by zero")\n'
            + "    return a / b\n",
        ),
        Step(
            "edit",
            target="tests/test_calc.py",
            old=IMPORT,
            new="import pytest\n\nfrom calc import add, divide, subtract\n",
        ),
        Step(
            "edit",
            target="tests/test_calc.py",
            old=TEST_SUBTRACT,
            new=TEST_SUBTRACT
            + "\n\ndef test_divide():\n    assert divide(6, 3) == 2\n"
            + "\n\ndef test_divide_by_zero():\n"
            + "    with pytest.raises(ZeroDivisionError):\n        divide(1, 0)\n",
        ),
        RUN_TESTS,
    ),
    tests_pass=True,
    lint_findings=0,
)

POWER_WITH_UNUSED_IMPORT = Turn(
    summary="add power (and an import nobody uses)",
    steps=(
        Step("read", target="calc/__init__.py"),
        Step("think", text="power is a one-liner. Import os in case I need it later."),
        Step("edit", target="calc/__init__.py", old=DOCSTRING, new=DOCSTRING + "\nimport os\n"),
        Step(
            "edit",
            target="calc/__init__.py",
            old=SUBTRACT,
            new=SUBTRACT + "\n\ndef power(a: float, b: float) -> float:\n    return a ** b\n",
        ),
        Step(
            "edit",
            target="tests/test_calc.py",
            old=IMPORT,
            new="from calc import add, power, subtract\n",
        ),
        Step(
            "edit",
            target="tests/test_calc.py",
            old=TEST_SUBTRACT,
            new=TEST_SUBTRACT + "\n\ndef test_power():\n    assert power(2, 3) == 8\n",
        ),
        RUN_TESTS,
    ),
    tests_pass=True,
    lint_findings=1,
)

LEAVE_A_NOTE = Turn(
    summary="leave a note and try again",
    steps=(
        Step("think", text="The test still fails. Maybe the test is wrong? Leaving a note."),
        Step(
            "edit",
            target="calc/__init__.py",
            old=DOCSTRING,
            new=DOCSTRING + "# turn {turn}: still looking at multiply\n",
        ),
        RUN_TESTS,
    ),
    tests_pass=False,
    lint_findings=0,
)

SCENARIOS: dict[str, Scenario] = {
    "multiply-with-bug": Scenario(
        "multiply-with-bug",
        ("multiply",),
        "the fix loop: a failing test feeds the next turn",
        (ADD_MULTIPLY, FIX_MULTIPLY),
    ),
    "divide-clean": Scenario(
        "divide-clean", ("divide",), "the happy path: one turn, green tests", (DIVIDE,)
    ),
    "lint-only": Scenario(
        "lint-only",
        ("power", "lint"),
        "lint findings are data, not failures",
        (POWER_WITH_UNUSED_IMPORT,),
    ),
    "never-fixes": Scenario(
        "never-fixes",
        ("never",),
        "max_turns exhaustion is a result, not an error",
        (ADD_MULTIPLY, LEAVE_A_NOTE),
    ),
}

# Most specific keyword first: "never fix multiply" is never-fixes, not multiply-with-bug.
_KEYWORD_ORDER = ("never-fixes", "lint-only", "divide-clean", "multiply-with-bug")


def pick_scenario(name: str, prompt: str) -> Scenario:
    if name:
        if name not in SCENARIOS:
            raise KeyError(f"unknown scenario {name!r}; choose from {sorted(SCENARIOS)}")
        return SCENARIOS[name]
    lowered = prompt.lower()
    for key in _KEYWORD_ORDER:
        if any(word in lowered for word in SCENARIOS[key].keywords):
            return SCENARIOS[key]
    return SCENARIOS[DEFAULT_SCENARIO]

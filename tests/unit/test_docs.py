"""The course stays true to the repository: paths, make targets, links, and the chapter skeleton."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CHAPTERS = [
    "00-why-split.md",
    "01-architecture.md",
    "02-the-contract.md",
    "03-lease-lifecycle.md",
    "04-vm-agent.md",
    "05-provider.md",
    "06-orchestrator-and-fake-agent.md",
    "07-dashboard.md",
    "08-running-the-demo.md",
    "09-from-demo-to-production.md",
]
REQUIRED_SECTIONS = ("## Read the code", "## Where this maps in production", "## Try it")
PATH_RE = re.compile(
    r"`((?:sandbox|scripts|images|tests|docs)/[\w./-]+?|Makefile|Procfile|pyproject\.toml|"
    r"README\.md)(?::\d+)?`"
)
MAKE_RE = re.compile(r"`make ([a-z][a-z-]*)")
LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
FENCE_RE = re.compile(r"```.*?```", re.S)


def _existing_chapters() -> list[Path]:
    return [DOCS / name for name in CHAPTERS if (DOCS / name).exists()]


def _make_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text()
    return set(re.findall(r"^([a-z][a-z-]*):", text, re.M))


def _wrapped_code_spans(text: str) -> list[str]:
    """Inline code spans (fenced ``` blocks excluded) whose content contains a
    raw newline: the span was hard-wrapped across lines by an editor, so
    PATH_RE/MAKE_RE cannot match it and it renders with a stray space.

    A naive `` `[^`\\n]+\\n[^`]*` `` search (matching a backtick, then a
    newline, then a run up to the next backtick) also fires on two distinct,
    correctly-closed spans separated by nothing but plain prose and a line
    break — the ordinary shape of hard-wrapped, code-reference-heavy prose.
    Splitting on backtick, after stripping fenced blocks, pairs each span
    with its own closing backtick and checks only that content for a newline.
    """
    parts = FENCE_RE.sub("", text).split("`")
    return [part for part in parts[1::2] if "\n" in part]


def test_every_chapter_exists():
    missing = [name for name in CHAPTERS if not (DOCS / name).exists()]
    assert not missing, missing


@pytest.mark.parametrize("chapter", _existing_chapters(), ids=lambda p: p.name)
def test_chapter_skeleton(chapter):
    text = chapter.read_text()
    lines = text.splitlines()
    assert lines[0].startswith("# "), "H1 first"
    body = "\n".join(lines[1:]).lstrip()
    assert body.startswith("After this chapter you can"), "opening promise"
    positions = [text.find(section) for section in REQUIRED_SECTIONS]
    assert all(p >= 0 for p in positions), f"missing sections in {chapter.name}"
    assert positions == sorted(positions), "sections in order"
    index = CHAPTERS.index(chapter.name)
    if index < len(CHAPTERS) - 1:
        assert "Next: [" in text and CHAPTERS[index + 1] in text, "next link"
    else:
        assert "Back to the [index](../README.md)" in text
    words = len(re.findall(r"\w+", text))
    cap = 2000 if chapter.name.startswith("08") else 1500
    assert 700 <= words <= cap, f"{chapter.name} has {words} words"


@pytest.mark.parametrize("doc", [*_existing_chapters(), ROOT / "README.md"], ids=lambda p: p.name)
def test_paths_targets_and_links_resolve(doc):
    text = doc.read_text()
    for match in PATH_RE.finditer(text):
        path = match.group(1)
        assert (ROOT / path).exists(), f"{doc.name} mentions missing path {path}"
    targets = _make_targets()
    for match in MAKE_RE.finditer(text):
        target = match.group(1)
        assert target in targets, f"{doc.name} mentions unknown make target {target}"
    for match in LINK_RE.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (doc.parent / target).exists(), f"{doc.name} links to missing {target}"
    assert not _wrapped_code_spans(text), f"{doc.name} has a code span wrapped across lines"


def test_readme_indexes_every_chapter():
    text = (ROOT / "README.md").read_text()
    for name in CHAPTERS:
        assert f"docs/{name}" in text, f"README does not list {name}"
    for command in (
        "make bootstrap",
        "make up",
        "make image",
        "make artifact",
        "make workers",
        "make demo",
    ):
        assert command in text, f"README does not show `{command}`"

"""Session state: the workspace, its bundle, and the turn ledger.

What: `clone` seeds a workspace from the session's bundle or from the seed
repo; `run_turn` runs the agent, commits, and saves a bundle plus
`session.json`; `export` writes the patch of every turn since the seed.
Why: a turn must be resumable on a different VM. Everything a later lease
needs is in the object store after each turn, so a VM that dies mid-turn
costs one turn, not the session. The bundle is written before the state, and
`run_turn` looks for a commit the state does not know about, so a crash
between the two writes recovers instead of redoing the turn.
Production: identical shape; the bundle goes to S3 under the session prefix.
"""

import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sandbox.objectstore import ObjectStore
from sandbox.runner import agent
from sandbox.runner.envelopes import CloneEnvelope, ExportEnvelope, TurnEnvelope
from sandbox.runner.gitutil import git
from sandbox.runner.scenarios import pick_scenario
from sandbox.timeutil import now_iso


@dataclass
class SessionState:
    session_id: str
    repo: str
    base_commit: str
    scenario: str = ""
    turn: int = 0
    history: list[dict] = field(default_factory=list)
    updated_at: str = ""


def state_uri(session_uri: str) -> str:
    return f"{session_uri.rstrip('/')}/session.json"


def bundle_uri(session_uri: str) -> str:
    return f"{session_uri.rstrip('/')}/repo.bundle"


def load_state(store: ObjectStore, session_uri: str) -> SessionState | None:
    try:
        return SessionState(**store.get_json(state_uri(session_uri)))
    except FileNotFoundError:
        return None


def save_state(store: ObjectStore, session_uri: str, state: SessionState) -> None:
    state.updated_at = now_iso()
    store.put_json(state_uri(session_uri), asdict(state))


def _empty(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for child in workspace.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def clone(
    store: ObjectStore, repo: str, workspace: Path, session_uri: str, seed_root: Path
) -> CloneEnvelope:
    workspace, seed_root = Path(workspace).resolve(), Path(seed_root).resolve()
    _empty(workspace)
    state = load_state(store, session_uri)
    if store.exists(bundle_uri(session_uri)):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "repo.bundle"
            store.download_file(bundle_uri(session_uri), bundle)
            git("clone", "-q", "-b", "main", str(bundle), str(workspace), cwd=workspace.parent)
        source = "bundle"
    else:
        seed = Path(seed_root) / f"{repo}.git"
        if not seed.exists():
            raise FileNotFoundError(f"no seed repository at {seed}")
        git("clone", "-q", "-b", "main", str(seed), str(workspace), cwd=workspace.parent)
        source = "seed"
    head = git("rev-parse", "HEAD", cwd=workspace)
    if state is None:
        session_id = session_uri.rstrip("/").rsplit("/", 1)[-1]
        state = SessionState(session_id=session_id, repo=repo, base_commit=head)
        save_state(store, session_uri, state)
    return CloneEnvelope(
        ok=True, source=source, head=head, turn=state.turn, scenario=state.scenario
    )


def save_bundle(store: ObjectStore, workspace: Path, session_uri: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "repo.bundle"
        git("bundle", "create", str(bundle), "--all", cwd=workspace)
        store.upload_file(bundle, bundle_uri(session_uri))


def run_turn(
    store: ObjectStore,
    workspace: Path,
    session_uri: str,
    prompt: str,
    scenario_name: str,
    feedback_uri: str,
    seconds: float,
    agent_name: str,
    out=sys.stdout,
) -> TurnEnvelope:
    state = load_state(store, session_uri)
    if state is None:
        raise RuntimeError("no session state; run clone first")
    n = state.turn + 1
    # The trailing colon terminates the prefix, so "^turn 1:" cannot match a
    # commit message starting "turn 10:" and turn numbers never collide.
    recovered = git("log", "--format=%H", "-n", "1", f"--grep=^turn {n}:", cwd=workspace)
    if recovered:
        state.turn = n
        state.history.append({"turn": n, "commit": recovered, "summary": "recovered from bundle"})
        save_state(store, session_uri, state)
        return TurnEnvelope(
            ok=True,
            turn=n,
            commit=recovered,
            summary="recovered from bundle",
            scenario=state.scenario,
        )
    feedback = None
    if feedback_uri and feedback_uri != "none":
        feedback = store.get_json(feedback_uri)
    if agent_name == "claude":
        label = "claude"
        outcome = agent.run_claude_turn(workspace, prompt, feedback, seconds, out=out)
    else:
        scenario = pick_scenario(scenario_name or state.scenario, prompt)
        label = scenario.name
        outcome = agent.run_fake_turn(workspace, scenario, n, seconds, feedback, out=out)
    if not outcome.ok:
        return TurnEnvelope(
            ok=False,
            turn=n,
            error=outcome.error,
            scenario=label,
            fake_cost_usd=outcome.fake_cost_usd,
        )
    git("add", "-A", cwd=workspace)
    git("commit", "-q", "--allow-empty", "-m", f"turn {n}: {outcome.summary}", cwd=workspace)
    commit = git("rev-parse", "HEAD", cwd=workspace)
    save_bundle(store, workspace, session_uri)
    state.turn = n
    state.scenario = label
    state.history.append(
        {
            "turn": n,
            "commit": commit,
            "summary": outcome.summary,
            "files_changed": outcome.files_changed,
            "feedback_used": feedback is not None,
            "fake_cost_usd": outcome.fake_cost_usd,
        }
    )
    save_state(store, session_uri, state)
    return TurnEnvelope(
        ok=True,
        turn=n,
        commit=commit,
        files_changed=outcome.files_changed,
        summary=outcome.summary,
        feedback_used=feedback is not None,
        fake_cost_usd=outcome.fake_cost_usd,
        scenario=label,
    )


def export(store: ObjectStore, workspace: Path, session_uri: str) -> ExportEnvelope:
    state = load_state(store, session_uri)
    if state is None:
        raise RuntimeError("no session state; run clone first")
    span = f"{state.base_commit}..HEAD"
    patch = git("format-patch", "--stdout", span, cwd=workspace)
    path = workspace / "session.patch"
    path.write_text(patch + "\n" if patch else "")
    commits = int(git("rev-list", "--count", span, cwd=workspace))
    return ExportEnvelope(ok=True, commits=commits, bytes=path.stat().st_size)

"""`/guideline`: the steps from naming a coordinator to a `/start` that nothing blocks.

A new user names one coordinator model in `LN-AGENTS.md`; the pipeline does the rest or says what
is in the way. This walks the same checks the start gate and dispatch apply, in the order a
person meets them, and reports where each stands, so it cannot say "ready" while `/start` or the
first stages would still refuse:

* the coordinator: `load_setup`, the coordinator's own registry checks and readiness (`choose`),
  and whether the registry binds it to the human authority;
* the repository: the static checks of `run_preflight_tolerating_gaps` (everything `validate`
  reports except rule R20, which the next step handles) and `registry_changes.gaps`, the agents
  that have no model, which the start gate refuses;
* the run itself: a commit to pin (grounding), the local servers dispatch starts on demand, and
  for every stage a run can reach without a human decision, `evaluate_stage` and the adapter
  refusals (`dispatch.adapter_blockers`) that dispatch applies before any call.

It is read-only, writes nothing and calls no model, so it works before any coordinator exists. It
reads the repository as it is now, not a run's frozen copy: it is about starting, not about a run.
One host probe is taken per reply and every check reads that one report.

What it cannot see: whether the provider tools are signed in, or have quota left (the pipeline
does not inspect a login, `readiness.py`; a call that fails for either pauses the run), and what
the run's own inputs decide: the task class at START and the data class G gives the project's
files, which can keep a stage off the cloud. It vouches for the repository and the host.

Known limit: a local server counts as needed when any candidate backend of a reachable operation
runs on it, while `LocalServers.ensure` stops at the first ready backend in preference order, so
an operation with a ready cloud model ahead of a broken local one would show a step to do that
dispatch would not need. No operation in the registry mixes the two today.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_pipeline import registry_changes
from agentic_pipeline.authority import authority_models
from agentic_pipeline.coordinator import (
    CAPABILITY,
    SETUP_FILE,
    SUPPORTED_PROVIDERS,
    Coordinator,
    Setup,
    SetupError,
    for_authority,
    load_setup,
)
from agentic_pipeline.dispatch import BackendAdapter, adapter_blockers
from agentic_pipeline.graph import Graph, load_yaml
from agentic_pipeline.grounding import GroundingError, _git
from agentic_pipeline.readiness import (
    Check,
    Report,
    Status,
    evaluate_stage,
    run_preflight_tolerating_gaps,
)
from agentic_pipeline.registry import backend_location, op_backends, operations

DONE = "done"
TODO = "todo"  # something for you to do
AUTO = "auto"  # the pipeline does it when it is needed
UNCHECKED = "unchecked"  # an earlier step has to be fixed before this one can be read

MAX_LINES = 6
SERVER_ASSUMED = "starts on demand (dispatch starts it when a stage needs it)"
NOTES = (
    "This cannot see whether the provider tools are signed in or have quota left. A call that "
    "fails for either pauses the run and says so.",
    "What the run's own inputs decide is checked when the run reaches them: the task class at "
    "START, and the data class G gives the project's files, which can keep a stage off the cloud.",
    "A run waits at H1, R1 and R2 for your signed decision; that is by design, not a blocker.",
)


def _adapters(registry: Mapping[str, Any]) -> Mapping[str, BackendAdapter]:
    from agentic_pipeline.run_cli import LazyAdapters  # imports the real adapters lazily

    return LazyAdapters(registry)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "--verify", "HEAD^{commit}").strip()


@dataclass(slots=True)
class Deps:
    """Everything with side effects, injectable for tests."""

    preflight: Callable[[Path], Report] = run_preflight_tolerating_gaps
    coordinator_for: Callable[[Path, Path], Coordinator] = for_authority
    adapters: Callable[[Mapping[str, Any]], Mapping[str, BackendAdapter]] = _adapters
    head: Callable[[Path], str] = _head  # the commit a run would pin; raises GroundingError
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[Path], bool] = Path.exists


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    title: str
    status: str
    blocks: bool  # a step that is not done or automatic keeps `/start` from working
    detail: str = ""
    do: tuple[str, ...] = ()
    snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "blocks": self.blocks,
            "detail": self.detail,
            "do": list(self.do),
            "snippet": self.snippet,
        }


def _lines(items: list[str]) -> str:
    shown = items[:MAX_LINES]
    more = f" (and {len(items) - MAX_LINES} more)" if len(items) > MAX_LINES else ""
    return "; ".join(shown) + more


def startable_stages(graph: Graph, registry: Mapping[str, Any]) -> list[str]:
    """The model stages a run can reach from START without a human decision, in graph order.

    The fast path leads on to the build stages before anyone decides, so they count. A disabled
    stage can never run and is left out.
    """
    adjacent: dict[str, list[str]] = {}
    for edge in graph.edges:
        adjacent.setdefault(edge.src, []).append(edge.to)
    seen: list[str] = []
    todo = ["START"]
    while todo:
        state = todo.pop(0)
        if state in seen:
            continue
        seen.append(state)
        if state not in graph.human_states:
            todo.extend(adjacent.get(state, []))
    stages = registry.get("stage_requirements") or {}
    return [s for s in seen if s in stages and stages[s]["mode"] != "disabled"]


def _launch_argv(provider: Mapping[str, Any]) -> list[str]:
    """The argv dispatch runs for a server, a leading `~` expanded the way `_start` does."""
    return [
        str(Path(a).expanduser()) if a.startswith("~") else a for a in provider["launch"]["argv"]
    ]


def launch_problems(provider: Mapping[str, Any], deps: Deps) -> list[str]:
    """Why dispatch could not start this provider's server: a program or a file it names is
    missing. (It cannot tell whether the server then comes up.)"""
    # A path is told by how it is written: on Windows an expanded `~/x` is `C:\x`, which starts
    # with neither "/" nor "~".
    written = provider["launch"]["argv"]
    argv = _launch_argv(provider)
    program = argv[0]
    found = deps.exists(Path(program)) if "/" in written[0] else deps.which(program) is not None
    problems = [] if found else [f"{program} is not installed"]
    problems += [
        f"{arg} does not exist"
        for arg, as_written in zip(argv[1:], written[1:], strict=True)
        if as_written.startswith(("/", "~")) and not deps.exists(Path(arg))
    ]
    return problems


def _alias(provider: Mapping[str, Any]) -> str | None:
    argv = _launch_argv(provider)
    return argv[argv.index("--alias") + 1] if "--alias" in argv[:-1] else None


def assume_servers(report: Report, starting: set[str]) -> Report:
    """`report` as it will read once dispatch has started the servers of `starting`.

    A server that is down reads UNVERIFIED (no daemon, no listener, no model served), which
    would block every stage forever; dispatch starts it first. After a start the server answers,
    listens on loopback and serves only the model its `--alias` names, so only those checks of
    that provider turn VERIFIED. A check that FAILED (a listener on a public interface, say)
    and any other model of the provider stay as they were probed.
    """
    registry = report.registry
    providers = {name: list(checks) for name, checks in report.providers.items()}
    models = {name: list(checks) for name, checks in report.models.items()}
    for name in starting:
        providers[name] = [
            Check(c.name, Status.VERIFIED, SERVER_ASSUMED)
            if c.name in ("daemon", "listener_binding") and c.status is Status.UNVERIFIED
            else c
            for c in providers.get(name, [])
        ]
        alias = _alias(registry["providers"][name])
        for model, entry in registry["models"].items():
            if entry["provider"] == name and entry["model_id"] == alias:
                models[model] = [Check("model_inventory", Status.VERIFIED, SERVER_ASSUMED)]
    return Report(
        report.static_errors,
        report.global_checks,
        providers,
        models,
        report.isolation,
        registry,
    )


def build(root: Path, runs_dir: Path, deps: Deps | None = None) -> dict[str, Any]:
    """The steps, in order, and whether a `/start` would be blocked by none of them."""
    deps = deps or Deps()
    report = deps.preflight(root)
    broken = bool(report.static_errors)
    registry = report.registry or _read_registry(root)
    gaps = registry_changes.gaps(root)
    steps: list[Step] = []

    setup, setup_error = _setup(root)
    coordinator: Coordinator | None
    try:
        coordinator = deps.coordinator_for(root, runs_dir)
        coordinator.preflight = lambda: report  # one probe for the whole reply
    except Exception as exc:  # a repository too broken to read its policy
        coordinator = None
        setup_error = setup_error or str(exc)
    named = _coordinator_step(setup, setup_error, registry, coordinator, report, bool(gaps))
    steps.append(named)
    steps.append(_ready_step(named, coordinator, broken))
    steps.append(_authority_step(named, setup, registry, bool(gaps)))
    steps.append(_repository_step(report))
    steps.append(_mapping_step(gaps, named))
    steps.append(_commit_step(root, deps))
    steps += _run_steps(root, report, deps)

    ready = not any(s.blocks and s.status in (TODO, UNCHECKED) for s in steps)
    return {
        "ready": ready,
        "steps": [s.to_dict() for s in steps],
        "notes": list(NOTES),
        "errors": [],
    }


def _read_registry(root: Path) -> Mapping[str, Any]:
    try:
        registry = load_yaml(root / "policy" / "model_registry.yaml")
    except Exception:  # unreadable: the repository step reports why
        return {}
    return registry if isinstance(registry, dict) else {}


def _setup(root: Path) -> tuple[Setup | None, str | None]:
    try:
        return load_setup(root), None
    except SetupError as exc:
        return None, str(exc)


def _bound(registry: Mapping[str, Any]) -> list[str]:
    """The models the registry binds to the human authority; none if it cannot be read."""
    try:
        return authority_models(registry)
    except (KeyError, TypeError, AttributeError):
        return []


def _ready(report: Report, name: str) -> bool | None:
    """Whether the probe finds `name` ready; None when nothing was probed."""
    return None if not report.registry else not report.backend_blockers(name)


def _candidates(registry: Mapping[str, Any], report: Report) -> list[tuple[str, bool]]:
    """(description, ready) of each model that can coordinate: it declares `coordination` and
    its provider takes a task override."""
    bound = _bound(registry)
    found = []
    for name, model in (registry.get("models") or {}).items():
        if CAPABILITY in model["capabilities"] and model["provider"] in SUPPORTED_PROVIDERS:
            ready = _ready(report, name)
            state = "not probed" if ready is None else "ready" if ready else "not ready"
            where = backend_location(registry, name)
            bound_note = "; bound to human_authority" if name in bound else ""
            found.append(
                (f"{name} ({model['provider']}, {where}; {state}{bound_note})", ready is True)
            )
    return found


def _claims(registry: Mapping[str, Any], report: Report, gaps: bool) -> tuple[str, ...]:
    """How to make a model coordinate that does not declare `coordination` yet: a human claims
    it with `/add_model` and `/confirm`. Offered when no model that can coordinate is ready."""
    found = [
        (name, model, _ready(report, name))
        for name, model in (registry.get("models") or {}).items()
        if CAPABILITY not in model["capabilities"] and model["provider"] in SUPPORTED_PROVIDERS
    ]
    found.sort(key=lambda item: item[2] is not True)  # a ready model first
    lines = [
        f"`/add_model {model['provider']} {model['model_id']} {CAPABILITY}`, then `/confirm <id>`: "
        f"claims that {name} can coordinate (declared, not measured)."
        for name, model, _ in found[:3]
    ]
    if lines and gaps:
        lines.append(
            "`/add_model` is refused while an agent has no model: add `coordination` to that "
            "model's `capabilities` in `policy/model_registry.yaml` by hand, in a reviewed commit."
        )
    return tuple(lines)


def _coordinator_step(
    setup: Setup | None,
    error: str | None,
    registry: Mapping[str, Any],
    coordinator: Coordinator | None,
    report: Report,
    gaps: bool,
) -> Step:
    title = f"Name a coordinator in {SETUP_FILE}"
    options = _candidates(registry, report)
    pick = (
        f"Models that can coordinate: {_lines([text for text, _ in options])}. "
        "You choose; nothing picks one for you."
        if options
        else "The registry has no model that declares `coordination` on a supported provider."
    )
    if gaps:
        pick += " An agent has no model, and nothing can be proposed for it without a coordinator."
    claims = () if any(ready for _, ready in options) else _claims(registry, report, gaps)
    snippet = "---\ncoordinator: <model>\n---\n"
    how = (f"Create {SETUP_FILE} in the project root, with the model you chose:", *claims)
    if error is not None:
        return Step("coordinator", title, TODO, gaps, f"{error}. {pick}", how, snippet)
    if setup is None:
        return Step(
            "coordinator", title, TODO, gaps, f"There is no {SETUP_FILE}. {pick}", how, snippet
        )
    problems = (
        coordinator.registry_problems(setup.coordinator)
        if coordinator is not None
        else [f"{setup.coordinator}: the registry could not be read"]
    )
    if problems:
        return Step(
            "coordinator",
            title,
            TODO,
            gaps,
            f"{SETUP_FILE} names a coordinator that cannot be used: {_lines(problems)}. {pick}",
            (f"Change `coordinator:` in {SETUP_FILE} to a model that can coordinate.", *claims),
        )
    extra = f", fallback {setup.fallback}" if setup.fallback else ""
    return Step("coordinator", title, DONE, gaps, f"{SETUP_FILE} names {setup.coordinator}{extra}.")


def _ready_step(named: Step, coordinator: Coordinator | None, broken: bool) -> Step:
    title = "The coordinator is ready to be used"
    if named.status != DONE or coordinator is None or broken:
        why = "name a coordinator first" if named.status != DONE else "fix the repository first"
        return Step("coordinator_ready", title, UNCHECKED, False, f"Not checked: {why}.")
    backend, why = coordinator.choose()
    if backend is None:
        return Step(
            "coordinator_ready",
            title,
            TODO,
            False,
            f"Not ready, so views and summaries fall back to the raw record: {why}",
            ("Fix the cause above, then run /guideline again.",),
        )
    return Step("coordinator_ready", title, DONE, False, f"{backend} will coordinate.")


def _authority_step(
    named: Step, setup: Setup | None, registry: Mapping[str, Any], gaps: bool
) -> Step:
    title = "The coordinator may choose models for you (optional)"
    if named.status != DONE or setup is None:
        return Step("authority", title, UNCHECKED, False, "Not checked: name a coordinator first.")
    bound = _bound(registry)
    names = [m for m in (setup.coordinator, setup.fallback) if m]
    allowed = [m for m in names if m in bound]
    if allowed:
        return Step(
            "authority",
            title,
            DONE,
            False,
            f"{allowed[0]} is bound to human_authority, so it chooses a model for an agent that "
            "has none (you confirm).",
        )
    detail = (
        f"{setup.coordinator} is not bound to human_authority. When an agent has no model, code "
        "gives it the coordinator and no model chooses."
    )
    if gaps:
        return Step(
            "authority",
            title,
            TODO,
            False,
            detail + " Binding is refused until every agent has a model.",
        )
    slot = "human_authority.model_assistance"
    return Step(
        "authority",
        title,
        TODO,
        False,
        detail,
        (f"`/agents {slot} {','.join([*bound, setup.coordinator])}`, then `/confirm <id>`",),
    )


def _repository_step(report: Report) -> Step:
    title = "The repository passes its own checks"
    if report.static_errors:
        return Step(
            "repository",
            title,
            TODO,
            True,
            _lines(list(report.static_errors)),
            (
                "Fix them in a reviewed commit: the policy files are human-owned (CODEOWNERS).",
                "`python -m agentic_pipeline validate` prints the same list.",
            ),
        )
    return Step("repository", title, DONE, True, "The graph, policies and registry validate.")


def _mapping_step(gaps: list[registry_changes.Gap], named: Step) -> Step:
    title = "Every agent has a model"
    if not gaps:
        return Step("mapping", title, DONE, True, "Every agents/*.md has a model.")
    which = _lines([f"{g.agent} ({g.spec})" for g in gaps])
    noun = "agent has" if len(gaps) == 1 else "agents have"
    if named.status != DONE:
        return Step(
            "mapping",
            title,
            TODO,
            True,
            f"{len(gaps)} {noun} no model: {which}.",
            (f"Name a coordinator first ({SETUP_FILE}): nothing is proposed without one.",),
        )
    return Step(
        "mapping",
        title,
        TODO,
        True,
        f"{len(gaps)} {noun} no model: {which}. No run starts until each has one.",
        (
            "`/start` proposes a model for each agent and starts nothing.",
            "`/confirm <id>` writes the proposal.",
            "`/start` again.",
        ),
    )


def _commit_step(root: Path, deps: Deps) -> Step:
    title = "The project has a commit to pin"
    try:
        sha = deps.head(root)
    except GroundingError as exc:
        return Step(
            "commit",
            title,
            TODO,
            True,
            f"A run pins the commit it starts from, and there is none: {exc}",
            ("`git init`, then commit the project.",),
        )
    return Step("commit", title, DONE, True, f"A run would pin {sha[:12]}.")


def _run_steps(root: Path, report: Report, deps: Deps) -> list[Step]:
    servers_title = "Local model servers can be started"
    stages_title = "Every stage a run can reach has a ready model"
    if report.static_errors:
        why = "Not checked: fix the repository first."
        return [
            Step("servers", servers_title, UNCHECKED, True, why),
            Step("stages", stages_title, UNCHECKED, True, why),
        ]
    registry = report.registry
    graph = Graph.load(root / "pipeline" / "graph.yaml")
    stages = startable_stages(graph, registry)

    needed: dict[str, str] = {}  # provider -> the stage that first needs it
    for stage in stages:
        for _, op in operations({stage: registry["stage_requirements"][stage]}):
            for backend in op_backends(registry, stage, op):
                model = registry["models"].get(backend)
                name = model["provider"] if model else backend
                if "launch" in registry["providers"].get(name, {}):
                    needed.setdefault(name, stage)
    broken: list[str] = []
    starting: set[str] = set()
    running: list[str] = []
    for name in needed:
        provider = registry["providers"][name]
        daemon = [c for c in report.providers.get(name, []) if c.name == "daemon"]
        if daemon and daemon[0].status is Status.VERIFIED:
            running.append(name)
        elif problems := launch_problems(provider, deps):
            broken.append(f"{name}: {_lines(problems)}")
        else:
            starting.add(name)
    if broken:
        server = Step(
            "servers",
            servers_title,
            TODO,
            True,
            _lines(broken),
            ("Install what is missing, or correct the `launch` of that provider (policy).",),
        )
    elif starting:
        server = Step(
            "servers",
            servers_title,
            AUTO,
            True,
            f"{', '.join(sorted(starting))}: not running now. Dispatch starts each when a stage "
            "needs it and stops it afterwards; its program and files are there.",
        )
    else:
        which = f"{', '.join(sorted(running))} already running" if running else "none is needed"
        server = Step("servers", servers_title, DONE, True, f"Local servers: {which}.")

    adapters = deps.adapters(registry)
    seen = assume_servers(report, starting)
    unbuilt = {  # their adapters ask the server about its model, so dispatch builds them after
        name
        for name, model in registry["models"].items()
        if model["provider"] in starting  # starting the server
    }
    blocked: list[str] = []
    for stage in stages:
        verdict = evaluate_stage(stage, seen)
        if not verdict.eligible:
            blocked.append(f"{stage}: " + "; ".join(verdict.reasons))
        else:
            blocked += adapter_blockers(registry, stage, verdict.backends, adapters, unbuilt)
    if blocked:
        stage_step = Step(
            "stages",
            stages_title,
            TODO,
            True,
            _lines(blocked),
            (
                "Fix the cause named for each stage, then run /guideline again.",
                "`/models` shows each model's readiness.",
            ),
        )
    else:
        stage_step = Step("stages", stages_title, DONE, True, f"Ready: {', '.join(stages)}.")
    return [server, stage_step]

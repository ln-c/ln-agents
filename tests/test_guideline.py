"""`/guideline`: the steps from naming a coordinator to a start nothing blocks.

The host is faked (a Report built from the registry) and so are the adapters; the repository is a
copy under tmp_path. Nothing probes this machine and no model is called.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_dispatch import ISOLATION

from agentic_pipeline import registry_changes
from agentic_pipeline.coordinator import for_authority
from agentic_pipeline.dispatch import adapter_blockers
from agentic_pipeline.graph import Graph, load_yaml
from agentic_pipeline.grounding import GroundingError
from agentic_pipeline.guideline import (
    AUTO,
    DONE,
    NOTES,
    TODO,
    UNCHECKED,
    Deps,
    assume_servers,
    build,
    startable_stages,
)
from agentic_pipeline.readiness import Check, Report, Status, evaluate_stage

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gemini_3_8_flash_high"  # the one model that declares `coordination`
SETUP = f"---\ncoordinator: {MODEL}\n---\n# How to write for me\nBe brief.\n"
LAUNCHED = ("llamacpp_local", "llamacpp_decision")
STAGES = ["A1", "A2", "A2J", "A3", "A3R", "C1", "D1", "E1"]


def project(tmp_path: Path, setup: str | None = SETUP) -> Path:
    root = tmp_path / "repo"
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, root / name)
    if setup is not None:
        (root / "LN-AGENTS.md").write_text(setup, encoding="utf-8")
    return root


def registry_of(root: Path) -> dict[str, Any]:
    return load_yaml(root / "policy" / "model_registry.yaml")


def make_report(
    registry: Mapping[str, Any],
    *,
    down: tuple[str, ...] = (),
    servers: str = "down",
    listener: Status | None = None,
    not_served: tuple[str, ...] = (),
) -> Report:
    """What the host probe reads: subscription tools on PATH (except `down`), and each local
    server either answering (`up`) or not running (`down`), as `_probe` reports them."""
    report = Report(
        static_errors=[],
        global_checks=[Check("credential_env_absent", Status.VERIFIED, "")],
        registry=registry,
    )
    for name, provider in registry["providers"].items():
        if provider.get("readiness") is None:
            continue
        if provider["access_mode"] == "subscription_tool":
            status = Status.FAILED if name in down else Status.VERIFIED
            report.providers[name] = [Check("executable", status, "")]
            continue
        up = servers == "up"
        report.providers[name] = [
            Check("daemon", Status.VERIFIED if up else Status.UNVERIFIED, "not running"),
            Check(
                "listener_binding",
                listener or (Status.VERIFIED if up else Status.UNVERIFIED),
                "",
            ),
        ]
        for model, entry in registry["models"].items():
            if entry["provider"] == name:
                served = up and model not in not_served
                report.models[model] = [
                    Check("model_inventory", Status.VERIFIED if served else Status.UNVERIFIED, "")
                ]
    report.isolation = {n: Check(n, Status.VERIFIED, "") for n in ISOLATION}
    return report


class Adapters(Mapping[str, Any]):
    """The adapters mapping dispatch uses: `missing` backends cannot be built."""

    def __init__(
        self, missing: Mapping[str, str] | None = None, enforced: frozenset[str] | None = None
    ) -> None:
        self.missing = dict(missing or {})
        self.enforced = frozenset(ISOLATION) if enforced is None else enforced

    def __getitem__(self, key: str) -> Any:
        if key in self.missing:
            raise KeyError(key)
        return SimpleNamespace(enforced_isolation=self.enforced)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def unavailable(self, name: str) -> str | None:
        return self.missing.get(name)


class Rig:
    """`build` over a copied repository with every host fact injected."""

    def __init__(self, tmp_path: Path, setup: str | None = SETUP, **probe: Any) -> None:
        self.root = project(tmp_path, setup)
        self.runs = tmp_path / "runs"
        self.probe = probe
        self.probes = 0
        self.missing: dict[str, str] = {}
        self.static: list[str] = []
        self.commit: str | GroundingError = "a" * 40
        self.exists: Callable[[Path], bool] = lambda path: True
        self.which: Callable[[str], str | None] = lambda program: f"/usr/bin/{program}"

    def _preflight(self, root: Path) -> Report:
        self.probes += 1
        if self.static:
            return Report(static_errors=self.static)
        return make_report(registry_of(root), **self.probe)

    def _head(self, root: Path) -> str:
        if isinstance(self.commit, GroundingError):
            raise self.commit
        return self.commit

    def _boom(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a model was called")

    def deps(self) -> Deps:
        return Deps(
            preflight=self._preflight,
            coordinator_for=lambda root, runs: for_authority(root, runs, self._boom),
            adapters=lambda registry: Adapters(self.missing),
            head=self._head,
            which=lambda program: self.which(program),
            exists=lambda path: self.exists(path),
        )

    def run(self) -> dict[str, Any]:
        return build(self.root, self.runs, self.deps())

    def step(self, name: str) -> dict[str, Any]:
        return next(s for s in self.run()["steps"] if s["id"] == name)


def by_id(out: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in out["steps"]}


def add_agent_with_no_entry(root: Path) -> None:
    (root / "agents" / "pipeline-extra-agent.md").write_text("# Extra\n", encoding="utf-8")


# ── a repository where everything is in place ───────────────────────────────────


def test_a_ready_repository_reports_every_step_done_or_automatic(tmp_path: Path) -> None:
    out = Rig(tmp_path).run()
    assert out["ready"] is True and out["errors"] == []
    steps = by_id(out)
    assert [s["id"] for s in out["steps"]] == [
        "coordinator",
        "coordinator_ready",
        "authority",
        "repository",
        "mapping",
        "commit",
        "servers",
        "stages",
    ]
    assert {k: s["status"] for k, s in steps.items()} == {
        "coordinator": DONE,
        "coordinator_ready": DONE,
        "authority": DONE,
        "repository": DONE,
        "mapping": DONE,
        "commit": DONE,
        "servers": AUTO,  # both local servers are down, and dispatch starts them
        "stages": DONE,
    }
    assert out["notes"] == list(NOTES)
    assert "Ready: A1, A2, A2J, A3, A3R, C1, D1, E1." in steps["stages"]["detail"]


def test_the_stages_are_the_model_stages_a_run_reaches_before_any_human_decision() -> None:
    graph = Graph.load(ROOT / "pipeline" / "graph.yaml")
    registry = registry_of(ROOT)
    assert startable_stages(graph, registry) == STAGES
    # the fast path goes on to the build stages without H1; E4 and BENCH can never run
    disabled = {s for s, r in registry["stage_requirements"].items() if r["mode"] == "disabled"}
    assert {"E4", "BENCH"} <= disabled and not disabled & set(STAGES)


# ── the coordinator ─────────────────────────────────────────────────────────────


def test_a_new_user_with_no_setup_is_told_what_to_create_and_not_blocked(tmp_path: Path) -> None:
    rig = Rig(tmp_path, setup=None)
    out = rig.run()
    step = by_id(out)["coordinator"]
    assert step["status"] == TODO and step["blocks"] is False
    assert "There is no LN-AGENTS.md" in step["detail"]
    assert MODEL in step["detail"] and "google_ai_pro, cloud; ready" in step["detail"]
    assert step["snippet"] == "---\ncoordinator: <model>\n---\n", "a placeholder: you choose"
    assert by_id(out)["coordinator_ready"]["status"] == UNCHECKED
    assert out["ready"] is True, "with every agent mapped nothing needs a coordinator to start"
    assert not (rig.root / "LN-AGENTS.md").exists(), "code never writes it"


def test_with_an_agent_that_has_no_model_the_coordinator_blocks_until_it_is_named(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, setup=None)
    add_agent_with_no_entry(rig.root)
    out = rig.run()
    steps = by_id(out)
    assert steps["coordinator"]["blocks"] is True and out["ready"] is False
    assert "nothing can be proposed for it without a coordinator" in steps["coordinator"]["detail"]
    assert steps["mapping"]["status"] == TODO and steps["mapping"]["blocks"] is True
    assert "pipeline-extra-agent.md" in steps["mapping"]["detail"]
    assert steps["mapping"]["do"] == [
        "Name a coordinator first (LN-AGENTS.md): nothing is proposed without one."
    ]


def test_a_named_coordinator_lets_start_propose_a_mapping_for_the_agent_with_no_model(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    add_agent_with_no_entry(rig.root)
    out = rig.run()
    steps = by_id(out)
    assert steps["coordinator"]["status"] == DONE and out["ready"] is False
    assert steps["mapping"]["do"] == [
        "`/start` proposes a model for each agent and starts nothing.",
        "`/confirm <id>` writes the proposal.",
        "`/start` again.",
    ]
    assert "1 agent has no model" in steps["mapping"]["detail"]
    assert steps["authority"]["status"] == DONE, "the registry binds the coordinator already"


def test_a_broken_setup_file_is_reported_with_its_reason(tmp_path: Path) -> None:
    step = Rig(tmp_path, setup="no front matter here\n").step("coordinator")
    assert step["status"] == TODO and "no front matter" in step["detail"]


def test_a_coordinator_that_cannot_coordinate_is_refused_with_the_registry_reason(
    tmp_path: Path,
) -> None:
    step = Rig(tmp_path, setup="---\ncoordinator: qwen3_5_9b\n---\n").step("coordinator")
    assert step["status"] == TODO
    assert "the registry does not declare `coordination` for it" in step["detail"]
    assert step["do"] == ["Change `coordinator:` in LN-AGENTS.md to a model that can coordinate."]


def test_an_unready_coordinator_is_reported_but_does_not_block_a_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path, down=("google_ai_pro",))
    out = rig.run()
    step = by_id(out)["coordinator_ready"]
    assert step["status"] == TODO and step["blocks"] is False
    assert "executable FAILED" in step["detail"]
    assert out["ready"] is False, "but Design B runs on the same provider, so the stage step blocks"
    assert by_id(out)["stages"]["status"] == TODO


def test_a_coordinator_the_registry_does_not_bind_to_the_authority_is_offered_the_binding(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    path = rig.root / "policy" / "model_registry.yaml"
    text = path.read_text(encoding="utf-8")
    old = "model_assistance: [claude_cli_default, gemini_3_8_flash_high, qwen3_5_9b]"
    assert old in text
    path.write_text(text.replace(old, "model_assistance: [claude_cli_default, qwen3_5_9b]"))
    step = rig.step("authority")
    assert step["status"] == TODO and step["blocks"] is False
    assert "no model chooses" in step["detail"]
    assert step["do"] == [
        "`/agents human_authority.model_assistance claude_cli_default,qwen3_5_9b,"
        f"{MODEL}`, then `/confirm <id>`"
    ]


def test_binding_the_authority_is_deferred_while_an_agent_has_no_model(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    add_agent_with_no_entry(rig.root)
    path = rig.root / "policy" / "model_registry.yaml"
    text = path.read_text(encoding="utf-8")
    old = "model_assistance: [claude_cli_default, gemini_3_8_flash_high, qwen3_5_9b]"
    path.write_text(text.replace(old, "model_assistance: [claude_cli_default, qwen3_5_9b]"))
    step = rig.step("authority")
    assert step["status"] == TODO and step["do"] == []
    assert "Binding is refused until every agent has a model" in step["detail"]


def test_when_no_coordinator_is_ready_it_shows_how_to_claim_the_capability_for_another(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, setup=None, down=("google_ai_pro",))
    step = rig.step("coordinator")
    claims = [d for d in step["do"] if "/add_model" in d]
    assert claims and "`/confirm <id>`" in claims[0] and "coordination" in claims[0]
    assert claims[0].startswith("`/add_model claude_pro "), "a model that is ready comes first"
    assert "gemini_3_8_flash_high (google_ai_pro, cloud; not ready" in step["detail"]
    assert all("refused while an agent has no model" not in d for d in step["do"])


def test_the_claim_is_not_offered_when_a_coordinator_is_ready(tmp_path: Path) -> None:
    step = Rig(tmp_path, setup=None).step("coordinator")
    assert [d for d in step["do"] if "/add_model" in d] == []


def test_with_an_agent_that_has_no_model_the_claim_is_a_hand_edit(tmp_path: Path) -> None:
    rig = Rig(tmp_path, setup=None, down=("google_ai_pro",))
    add_agent_with_no_entry(rig.root)
    step = rig.step("coordinator")
    assert "`/add_model` is refused while an agent has no model" in step["do"][-1]
    assert "policy/model_registry.yaml" in step["do"][-1]


def test_a_named_model_that_lacks_the_capability_is_offered_the_claim(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path, setup="---\ncoordinator: claude_cli_default\n---\n", down=("google_ai_pro",)
    )
    step = rig.step("coordinator")
    assert step["status"] == TODO
    assert any(d.startswith("`/add_model claude_pro cli-default coordination`") for d in step["do"])


def test_the_notes_say_what_the_runs_own_inputs_decide(tmp_path: Path) -> None:
    notes = Rig(tmp_path).run()["notes"]
    assert any("task class at START" in n and "data class G gives" in n for n in notes)


def test_the_claim_it_suggests_is_one_the_registry_accepts(tmp_path: Path) -> None:
    rig = Rig(tmp_path, setup=None, down=("google_ai_pro",))
    line = next(d for d in rig.step("coordinator")["do"] if "/add_model" in d)
    _, provider, model_id, capability = line.split("`")[1].split()
    draft = registry_changes.draft_add_model(
        rig.root, tmp_path / "drafts", provider, model_id, [capability]
    )
    assert draft.kind == "claim_capabilities" and "coordination" in draft.diff


# ── the repository, the commit, the servers and the stages ──────────────────────


def test_a_repository_that_fails_its_checks_leaves_the_probing_steps_unchecked(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.static = ["registry R21 agents.build: 'agents/pipeline-build-agent.md' has no brief"]
    out = rig.run()
    steps = by_id(out)
    assert steps["repository"]["status"] == TODO and "R21" in steps["repository"]["detail"]
    for unchecked in ("coordinator_ready", "servers", "stages"):
        assert steps[unchecked]["status"] == UNCHECKED, unchecked
    assert out["ready"] is False


def test_a_project_with_no_commit_cannot_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.commit = GroundingError("git rev-parse failed: unknown revision")
    out = rig.run()
    step = by_id(out)["commit"]
    assert step["status"] == TODO and step["do"] == ["`git init`, then commit the project."]
    assert out["ready"] is False


def test_a_down_local_server_whose_program_and_files_exist_does_not_block(tmp_path: Path) -> None:
    out = Rig(tmp_path, servers="down").run()
    assert by_id(out)["servers"]["status"] == AUTO
    assert by_id(out)["stages"]["status"] == DONE and out["ready"] is True


def test_a_running_local_server_is_used_as_it_is(tmp_path: Path) -> None:
    step = Rig(tmp_path, servers="up").step("servers")
    assert step["status"] == DONE and "already running" in step["detail"]


def test_a_down_local_server_whose_model_file_is_missing_blocks(tmp_path: Path) -> None:
    rig = Rig(tmp_path, servers="down")
    rig.exists = lambda path: not str(path).endswith(".gguf")
    out = rig.run()
    steps = by_id(out)
    assert (
        steps["servers"]["status"] == TODO and ".gguf does not exist" in steps["servers"]["detail"]
    )
    assert steps["stages"]["status"] == TODO, "nothing assumed the server up"
    assert out["ready"] is False


def test_a_down_local_server_whose_program_is_missing_blocks(tmp_path: Path) -> None:
    rig = Rig(tmp_path, servers="down")
    rig.which = lambda program: None
    step = rig.step("servers")
    assert step["status"] == TODO and "llama-server is not installed" in step["detail"]


def test_a_listener_on_a_public_interface_is_not_assumed_away(tmp_path: Path) -> None:
    rig = Rig(tmp_path, servers="down", listener=Status.FAILED)
    steps = by_id(rig.run())
    assert steps["servers"]["status"] == AUTO, "dispatch would still try to start it"
    assert steps["stages"]["status"] == TODO and "A2J" in steps["stages"]["detail"]


def test_a_model_the_running_server_does_not_serve_blocks_its_stages(tmp_path: Path) -> None:
    out = Rig(tmp_path, servers="up", not_served=("qwen3_5_9b",)).run()
    step = by_id(out)["stages"]
    assert step["status"] == TODO and "C1" in step["detail"] and "E1" in step["detail"]
    assert out["ready"] is False


def test_only_the_model_a_server_is_launched_with_is_assumed_served(tmp_path: Path) -> None:
    root = project(tmp_path)
    registry = registry_of(root)
    registry["models"]["qwen_other"] = {
        "provider": "llamacpp_local",
        "model_id": "qwen3.5-other",
        "family": "qwen3_5",
        "capabilities": [],
    }
    report = make_report(registry, servers="down")
    seen = assume_servers(report, {"llamacpp_local"})
    assert [c.status for c in seen.models["qwen3_5_9b"]] == [Status.VERIFIED]
    assert [c.status for c in seen.models["qwen_other"]] == [Status.UNVERIFIED]
    assert all(c.status is Status.VERIFIED for c in seen.providers["llamacpp_local"])
    assert any(c.status is not Status.VERIFIED for c in seen.providers["llamacpp_decision"])


def test_a_missing_adapter_blocks_a_cloud_stage(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.missing = {"claude_opus_5_5_high_cli": "FileNotFoundError: claude"}
    out = rig.run()
    step = by_id(out)["stages"]
    assert step["status"] == TODO and "no adapter for" in step["detail"]
    assert "FileNotFoundError: claude" in step["detail"] and out["ready"] is False


def test_the_adapter_of_a_local_server_that_has_not_started_is_not_asked_for(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, servers="down")
    rig.missing = {"qwen3_5_9b": "ConnectionRefusedError", "clef_flash_local": "refused"}
    assert rig.step("stages")["status"] == DONE, "dispatch builds them after it starts the server"
    rig = Rig(tmp_path / "up", servers="up")
    rig.missing = {"qwen3_5_9b": "ConnectionRefusedError"}
    assert rig.step("stages")["status"] == TODO, "but a running one must be built"


def test_a_disabled_stage_never_blocks(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    registry = registry_of(rig.root)
    assert not evaluate_stage("E4", make_report(registry)).eligible, "E4 is blocked for good"
    assert rig.run()["ready"] is True


def test_one_blocker_makes_the_guideline_and_the_stage_verdict_agree(tmp_path: Path) -> None:
    rig = Rig(tmp_path, down=("claude_pro",))
    registry = registry_of(rig.root)
    report = make_report(registry, down=("claude_pro",))
    assert not evaluate_stage("A1", report).eligible
    out = rig.run()
    assert out["ready"] is False and "A1:" in by_id(out)["stages"]["detail"]
    healthy = Rig(tmp_path / "ok")
    up = make_report(registry, servers="up")
    assert all(evaluate_stage(s, up).eligible for s in STAGES)
    assert healthy.run()["ready"] is True


# ── what it does not do ─────────────────────────────────────────────────────────


def test_it_takes_one_probe_and_writes_nothing_and_calls_no_model(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    rig.run()  # the coordinator's factory raises if a model is called
    assert rig.probes == 1, "the coordinator and the stages read the same probe"
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert not rig.runs.exists(), "no .authority folder, no views"


def test_the_default_deps_probe_with_the_gap_tolerant_preflight() -> None:
    from agentic_pipeline.readiness import run_preflight_tolerating_gaps

    assert Deps().preflight is run_preflight_tolerating_gaps


# ── the refusals dispatch applies, shared with it ───────────────────────────────


def test_adapter_blockers_name_a_missing_adapter_and_an_unenforced_isolation_check() -> None:
    registry = registry_of(ROOT)
    backends = {"proposal_a": "claude_opus_5_5_high_cli", "proposal_b": "gemini_4_argon_medium"}
    assert adapter_blockers(registry, "A1", backends, Adapters()) == []
    missing = Adapters({"gemini_4_argon_medium": "agy is not installed"})
    assert adapter_blockers(registry, "A1", backends, missing) == [
        "A1: no adapter for ['gemini_4_argon_medium: agy is not installed']"
    ]
    weak = Adapters(enforced=frozenset())
    found = adapter_blockers(registry, "A1", backends, weak)
    assert found and all("adapter does not enforce" in line for line in found)


def test_adapter_blockers_skip_the_backends_a_server_has_yet_to_start() -> None:
    registry = registry_of(ROOT)
    backends = {"module_decomposition": "qwen3_5_9b"}
    gone = Adapters({"qwen3_5_9b": "refused"})
    assert adapter_blockers(registry, "C1", backends, gone) != []
    assert adapter_blockers(registry, "C1", backends, gone, skip={"qwen3_5_9b"}) == []


_ = pytest  # the fixtures come from tmp_path

"""The grounding interview: a model asks, code keeps the draft (fake model only)."""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from test_dispatch import DATA_CLASSES, ISOLATION, report
from test_grounding import Rig as GroundingRig
from test_grounding import git

from agentic_pipeline import grounding_interview as gi
from agentic_pipeline.coordinator import Coordinator
from agentic_pipeline.dispatch import AdapterResult, DispatchGrant, Identity, PreparedCall
from agentic_pipeline.graph import loads_yaml
from agentic_pipeline.grounding import load_entry_spec
from agentic_pipeline.provenance import verify_provenance_chain
from agentic_pipeline.runner import Outcome

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gemini_3_8_flash_high"  # declares `coordination`
ESCALATION = "claude_cli_default"  # does not, until a test says so


class FakeModel:
    """An adapter that records what it was given and replies from a queue."""

    enforced_isolation = frozenset(ISOLATION)

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.task = ""

    def identity(self) -> Identity:
        return Identity("fake:interview", "1.0", "subscription_tool via a fake")

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        from agentic_pipeline.adapters import embed_inputs, embed_spec

        prompt = f"{self.task}\n\n{embed_spec(grant)}{embed_inputs(grant.inputs)}"
        return PreparedCall(**asdict(self.identity()), prompt=prompt.encode())

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        self.rig.seen.append((grant, call.prompt.decode()))
        reply = self.rig.replies.pop(0)
        if isinstance(reply, AdapterResult):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        (grant.write_path / f"{grant.operation_id}.md").write_text(text, encoding="utf-8")
        return AdapterResult("ok")


def q(
    area: str = "objective",
    text: str = "What should change?",
    options: tuple[str, ...] = ("Add a feature", "Fix a bug", "Something else"),
    recommended: int = 1,
    why: str = "Everything else depends on it.",
) -> dict[str, Any]:
    return {
        "area": area,
        "text": text,
        "why": why,
        "options": list(options),
        "recommended": recommended,
    }


def turn(
    question: dict[str, Any] | None = None,
    updates: tuple[dict[str, Any], ...] = (),
    done: bool = False,
) -> dict[str, Any]:
    return {"updates": list(updates), "question": question, "done": done}


def setv(path: str, value: Any) -> dict[str, Any]:
    return {"path": path, "op": "set", "value": value}


def add(path: str, value: Any) -> dict[str, Any]:
    return {"path": path, "op": "add", "value": value}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, root / name)
    return root


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return tmp_path / "runs"


class Rig:
    def __init__(
        self,
        repo: Path,
        runs: Path,
        replies: list[Any],
        *,
        escalation_coordinates: bool = False,
        max_questions: int = gi.DEFAULT_MAX_QUESTIONS,
    ) -> None:
        self.replies, self.seen = list(replies), []
        self.runs = runs
        registry = loads_yaml((repo / "policy" / "model_registry.yaml").read_bytes())
        registry = copy.deepcopy(registry)
        # The tests pin the binding they test; they do not follow the registry file's.
        registry["agents"]["grounding"].update(primary=MODEL, escalation=ESCALATION)
        if escalation_coordinates:
            registry["models"][ESCALATION]["capabilities"].append("coordination")
        self.coordinator = Coordinator(
            root=repo,
            run_dir=runs / ".ground",
            run_id="grounding",
            registry=registry,
            data_classes=DATA_CLASSES,
            adapter_factory=lambda reg, backend, **kw: self._adapter(kw),
            preflight=lambda: report(),
            setup=None,
        )
        classes = loads_yaml((repo / "policy" / "task_classes.yaml").read_bytes())["classes"]
        self.interview = gi.Interview(self.coordinator, classes, max_questions)

    def _adapter(self, kw: dict[str, Any]) -> FakeModel:
        adapter = FakeModel(self)
        adapter.task = kw["task_builder"](None)
        return adapter

    def begin(self, idea: str = "Add a health endpoint", data_class: str = "internal") -> gi.Step:
        return self.interview.begin(idea, data_class)

    def draft(self, step: gi.Step) -> dict[str, Any]:
        return json.loads((self.runs / ".ground" / step.draft_id / "draft.json").read_text())

    def backends(self) -> list[str]:
        return [grant.backend for grant, _ in self.seen]


def complete(rig: Rig) -> gi.Step:
    """An interview that discusses the objective, the data and the acceptance criteria."""
    lies = (setv("handoff.status", "pending_validation"),)
    rig.replies[:] = [
        turn(q()),
        turn(q("data"), (setv("task.objective", "Add a health endpoint"),) + lies),
        turn(q("acceptance")),
        turn(
            done=True,
            updates=(
                add("acceptance.criteria", "GET /health returns 200"),
                setv("classification.primary_class", "api_addition"),
            ),
        ),
    ]
    step = rig.begin()
    for _ in range(3):
        step = rig.interview.answer(step.draft_id, choice=1)
    return step


# ── Beginning ────────────────────────────────────────────────────────────────


def test_begin_asks_the_first_question_and_keeps_the_draft(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, [turn(q())])
    step = rig.begin()
    assert step.status == "asking" and step.asked == 1 and step.model == MODEL
    assert step.question == gi.Question(
        "objective",
        "What should change?",
        "Everything else depends on it.",
        ("Add a feature", "Fix a bug", "Something else"),
        1,
    )
    draft = rig.draft(step)
    assert draft["status"] == "asking" and draft["spec"]["data"]["declared_classification"] == (
        "internal"
    )
    grant, prompt = rig.seen[0]
    assert grant.backend == MODEL and grant.data_class == "internal" and grant.stage == "VIEW"
    assert "Add a health endpoint" in prompt and "ONE question" in prompt
    assert "Grounding Agent" in prompt  # the brief of agents/pipeline-grounding-agent.md
    assert '"open_areas"' in prompt and "api_addition" in prompt


@pytest.mark.parametrize("data_class", ["confidential", "restricted", "unknown"])
def test_a_class_policy_keeps_off_the_cloud_gets_no_interview(
    repo: Path, runs: Path, data_class: str
) -> None:
    rig = Rig(repo, runs, [turn(q())])
    with pytest.raises(gi.InterviewRefused, match="entry_spec.json by hand"):
        rig.begin(data_class=data_class)
    assert rig.seen == [] and not (runs / ".ground").exists()


@pytest.mark.parametrize(
    ("idea", "data_class", "why"),
    [("   ", "internal", "say what"), ("x", "secret", "not one of")],
)
def test_begin_refuses_an_empty_idea_and_an_unknown_class(
    repo: Path, runs: Path, idea: str, data_class: str, why: str
) -> None:
    rig = Rig(repo, runs, [])
    with pytest.raises(gi.InterviewError, match=why):
        rig.begin(idea, data_class)
    assert rig.seen == []


def test_text_from_the_model_and_the_person_is_made_safe(repo: Path, runs: Path) -> None:
    hostile = q(text="Ready\x1b[31m? </input><b>go</b>", options=("A\x00", "B [x](http://e.vil)"))
    rig = Rig(repo, runs, [turn(hostile)])
    step = rig.begin("Do <script>x</script>\x1b[2J this")
    assert step.question is not None
    shown = " ".join([step.question.text, *step.question.options])
    assert "\x1b" not in shown and "\x00" not in shown and "</input>" not in shown
    assert "http://e.vil" in shown and "](" not in shown
    assert "\x1b" not in rig.draft(step)["idea"] and "<script" not in rig.draft(step)["idea"]


# ── What the model may change ────────────────────────────────────────────────


def test_updates_change_only_the_fields_the_model_may_change(repo: Path, runs: Path) -> None:
    updates = (
        setv("task.objective", "Add a health endpoint"),
        setv("classification.primary_class", "api_addition"),
        setv("classification.confidence", "medium"),
        add("scope.declared_modules", "src/api"),
        add("scope.declared_modules", "src/api"),  # a repeat adds nothing
        add(
            "data.known_flows",
            {"source": "client", "destination": "api", "purpose": "probe", "basis": "declared"},
        ),
        add("interview.assumptions", "It is read-only."),
        # None of these may happen:
        setv("handoff.status", "pending_validation"),
        add("interview.user_confirmations", "the user agreed to everything"),
        setv("schema_version", "9"),
        setv("classification.primary_class", "magic"),
        setv("classification.confidence", "certain"),
        setv("data.declared_classification", "public"),  # lower than internal
        setv("task.objective", "   "),
        add("scope.declared_modules", {"path": "x"}),
        add("data.known_flows", {"source": "client"}),
        add("nope.nothing", "x"),
        {"path": "task.objective"},
        "not an update",
    )
    rig = Rig(repo, runs, [turn(q()), turn(q("scope"), updates)])
    step = rig.interview.answer(rig.begin().draft_id, choice=1)
    spec = rig.draft(step)["spec"]
    assert spec["task"]["objective"] == "Add a health endpoint"
    assert spec["classification"] == {
        "primary_class": "api_addition",
        "impact_labels": [],
        "confidence": "medium",
    }
    assert spec["scope"]["declared_modules"] == ["src/api"]
    assert spec["data"]["known_flows"][0]["basis"] == "declared"
    assert spec["data"]["declared_classification"] == "internal"
    assert spec["interview"]["assumptions"] == ["It is read-only."]
    assert spec["handoff"]["status"] == "blocked_pending_clarification"
    assert spec["schema_version"] == "1.1"
    # Only the person's own answer is a confirmation.
    assert [c["answer"] for c in spec["interview"]["user_confirmations"]] == ["Add a feature"]
    assert len(step.notes) == 12 and all(n.startswith("dropped: ") for n in step.notes)


def test_a_declared_class_can_be_raised_and_then_stops_the_cloud_interview(
    repo: Path, runs: Path
) -> None:
    rig = Rig(
        repo,
        runs,
        [turn(q()), turn(q("data"), (setv("data.declared_classification", "confidential"),))],
    )
    step = rig.interview.answer(rig.begin().draft_id, text="It handles payroll records")
    assert step.status == "ready" and len(rig.seen) == 2
    assert any("may not be sent to a cloud model" in n for n in step.notes)
    assert rig.draft(step)["spec"]["data"]["declared_classification"] == "confidential"


# ── Answers ──────────────────────────────────────────────────────────────────


def test_the_persons_answer_is_a_confirmation_written_by_code(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, [turn(q(recommended=2)), turn(q("scope")), turn(q("data"))])
    step = rig.begin()
    step = rig.interview.answer(step.draft_id, choice=2)
    step = rig.interview.answer(step.draft_id, text="Only the public API")
    confirmations = rig.draft(step)["spec"]["interview"]["user_confirmations"]
    assert [(c["answer"], c["accepted_recommendation"]) for c in confirmations] == [
        ("Fix a bug", True),
        ("Only the public API", False),
    ]
    prompt = rig.seen[-1][1]
    assert (
        "Only the public API" in prompt and "Fix a bug" in prompt
    )  # the history reaches the model


def test_a_skipped_question_is_listed_as_open_and_is_not_a_confirmation(
    repo: Path, runs: Path
) -> None:
    rig = Rig(repo, runs, [turn(q()), turn(q("scope"))])
    step = rig.interview.answer(rig.begin().draft_id, skip=True)
    itv = rig.draft(step)["spec"]["interview"]
    assert (
        itv["unresolved_questions"] == ["What should change?"] and itv["user_confirmations"] == []
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"choice": 1, "text": "x"},
        {"choice": 9},
        {"choice": 0},
        {"text": "  "},
        {"choice": True},
    ],
)
def test_an_answer_is_one_option_or_one_text_or_a_skip(
    repo: Path, runs: Path, kwargs: dict[str, Any]
) -> None:
    rig = Rig(repo, runs, [turn(q())])
    step = rig.begin()
    with pytest.raises(gi.InterviewError):
        rig.interview.answer(step.draft_id, **kwargs)
    assert rig.draft(step)["status"] == "asking" and len(rig.seen) == 1


def test_there_is_nothing_to_answer_when_no_question_waits(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, [turn(done=True)])
    step = rig.begin()
    assert step.status == "ready"
    with pytest.raises(gi.InterviewError, match="no question"):
        rig.interview.answer(step.draft_id, choice=1)


# ── Replies that cannot be used ──────────────────────────────────────────────

BAD = [
    "not json at all",
    turn(),  # neither a question nor done
    turn(q(options=("only one",))),
    turn(q(options=("a", "a"))),
    turn(q(recommended=0)),
    turn(q(recommended=4)),
    turn(q(area="weather")),
    turn(q(text="")),
    {"updates": "x", "question": None, "done": True},
    {"done": "yes"},
    {**turn(q()), "extra": 1},
    turn(q(options=("a", "b", "c", "d", "e", "f"))),
]


@pytest.mark.parametrize("bad", BAD)
def test_a_reply_that_cannot_be_used_is_asked_for_again(repo: Path, runs: Path, bad: Any) -> None:
    rig = Rig(repo, runs, [bad, turn(q())])
    step = rig.begin()
    assert step.status == "asking" and len(rig.seen) == 2
    assert '"previous_reply_problem": null' in rig.seen[0][1]
    assert '"previous_reply_problem": "' in rig.seen[1][1]


def test_two_unusable_replies_stop_the_interview_when_the_escalation_cannot_coordinate(
    repo: Path, runs: Path
) -> None:
    rig = Rig(repo, runs, ["no", "still no", turn(q())])
    with pytest.raises(gi.InterviewError, match="asking claude_cli_default instead was not") as e:
        rig.begin()
    assert rig.backends() == [MODEL, MODEL] and e.value.draft_id
    draft = json.loads((runs / ".ground" / e.value.draft_id / "draft.json").read_text())
    assert draft["status"] == "model_pending"
    step = rig.interview.resume(e.value.draft_id)  # the model is asked again, and now answers
    assert step.status == "asking" and rig.backends() == [MODEL] * 3


def test_two_unusable_replies_go_once_to_the_escalation_model_when_it_may_coordinate(
    repo: Path, runs: Path
) -> None:
    rig = Rig(repo, runs, ["no", "still no", turn(q())], escalation_coordinates=True)
    step = rig.begin()
    assert rig.backends() == [MODEL, MODEL, ESCALATION] and step.model == ESCALATION
    assert step.status == "asking"


def test_an_unusable_escalation_reply_stops_the_interview(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, ["a", "b", "c"], escalation_coordinates=True)
    with pytest.raises(gi.InterviewError, match="and then claude_cli_default gave unusable"):
        rig.begin()
    assert len(rig.seen) == 3


def test_a_quota_error_pauses_the_interview_and_nothing_goes_to_another_model(
    repo: Path, runs: Path
) -> None:
    rig = Rig(
        repo,
        runs,
        [AdapterResult("quota_exhausted", "limit"), turn(q())],
        escalation_coordinates=True,
    )
    with pytest.raises(gi.InterviewPaused, match="no quota left") as e:
        rig.begin()
    assert rig.backends() == [MODEL] and e.value.draft_id
    step = rig.interview.resume(e.value.draft_id)
    assert step.status == "asking" and rig.backends() == [MODEL, MODEL]


def test_a_model_that_is_not_ready_is_reported_and_the_draft_is_kept(
    repo: Path, runs: Path
) -> None:
    rig = Rig(repo, runs, [])
    rig.coordinator.preflight = lambda: report(unready=(MODEL,))
    with pytest.raises(gi.InterviewError, match="is not ready") as e:
        rig.begin()
    assert rig.seen == [] and e.value.draft_id


# ── Ending ───────────────────────────────────────────────────────────────────


def test_the_question_limit_ends_the_interview_and_lists_what_was_not_asked(
    repo: Path, runs: Path
) -> None:
    rig = Rig(repo, runs, [turn(q()), turn(q("scope")), turn(q("data"))], max_questions=2)
    step = rig.begin()
    step = rig.interview.answer(step.draft_id, choice=1)
    step = rig.interview.answer(step.draft_id, choice=1)
    assert step.status == "ready" and step.asked == 2
    assert any("limit of 2 questions" in n for n in step.notes)
    open_ = rig.draft(step)["spec"]["interview"]["unresolved_questions"]
    assert open_ == [
        f"Not discussed: {a}" for a in ("classification", "guardrails", "data", "acceptance")
    ]


def test_done_stops_asking_and_keeps_the_waiting_question_open(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, [turn(q())])
    step = rig.interview.done(rig.begin().draft_id)
    assert step.status == "ready" and step.summary is not None and len(rig.seen) == 1
    assert "What should change?" in rig.draft(step)["spec"]["interview"]["unresolved_questions"]


def test_confirm_writes_a_valid_spec_and_the_handoff_is_computed_by_code(
    repo: Path, runs: Path
) -> None:
    rig = Rig(repo, runs, [])
    step = complete(rig)
    assert step.status == "ready" and step.summary and "GET /health returns 200" in step.summary
    done = rig.interview.confirm(step.draft_id)
    assert done.spec["handoff"]["status"] == "pending_validation"
    assert load_entry_spec(done.path.read_bytes()) == done.spec
    assert done.path == runs / ".ground" / step.draft_id / "entry_spec.json"
    assert rig.interview.show(step.draft_id).status == "confirmed"


def test_a_spec_with_a_mandatory_area_never_discussed_is_blocked(repo: Path, runs: Path) -> None:
    replies = [
        turn(q()),
        turn(done=True, updates=(setv("task.objective", "Add a health endpoint"),)),
    ]
    rig = Rig(repo, runs, replies)
    step = rig.interview.answer(rig.begin().draft_id, choice=1)
    done = rig.interview.confirm(step.draft_id)
    assert done.spec["handoff"]["status"] == "blocked_pending_clarification"
    assert "Not discussed: acceptance" in done.spec["interview"]["unresolved_questions"]


def test_confirm_needs_a_finished_interview_and_an_objective(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, [turn(q()), turn(done=True)])
    step = rig.begin()
    with pytest.raises(gi.InterviewError, match="not finished"):
        rig.interview.confirm(step.draft_id)
    step = rig.interview.answer(step.draft_id, choice=1)
    with pytest.raises(gi.InterviewError, match="not a valid entry spec"):
        rig.interview.confirm(step.draft_id)  # no objective was ever recorded
    assert not (runs / ".ground" / step.draft_id / "entry_spec.json").exists()


def test_the_spec_an_interview_writes_is_accepted_by_start(
    repo: Path, runs: Path, tmp_path: Path
) -> None:
    rig = Rig(repo, runs, [])
    spec = rig.interview.confirm(complete(rig).draft_id).path

    work = tmp_path / "work"
    work.mkdir()
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, work / name)
    (work / "tests").mkdir()
    (work / "tests" / "test_x.py").write_text("def test_x(): ...\n")
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    runner = GroundingRig(work, tmp_path, spec).runner()
    result = runner.step()
    assert result.outcome is Outcome.ADVANCED and runner.state.current == "G", result


# ── The record ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("draft_id", ["../x", "", "ABCDEF123456", "0123456789abc", "nothere1234a"])
def test_a_draft_id_is_checked_before_it_is_used(repo: Path, runs: Path, draft_id: str) -> None:
    rig = Rig(repo, runs, [])
    with pytest.raises(gi.InterviewError):
        rig.interview.show(draft_id)


def test_the_provenance_chain_of_the_interview_calls_verifies(repo: Path, runs: Path) -> None:
    rig = Rig(repo, runs, ["no", turn(q())])
    rig.begin()
    chain = runs / ".ground" / "views" / "provenance_grounding.jsonl"
    events = [json.loads(line)["event"] for line in chain.read_text().splitlines()]
    assert events == ["model_call", "model_result"] * 2 and verify_provenance_chain(chain).ok


def test_the_interview_waits_longer_for_a_question_than_a_view_does(repo: Path, runs: Path) -> None:
    from agentic_pipeline.coordinator import (
        CALL_TIMEOUT_SECONDS,
        INTERVIEW_TIMEOUT_SECONDS,
        for_grounding,
    )

    coordinator = for_grounding(repo, runs)
    assert coordinator.timeout == INTERVIEW_TIMEOUT_SECONDS > CALL_TIMEOUT_SECONDS
    assert coordinator.run_dir == runs / ".ground" and coordinator.setup is None

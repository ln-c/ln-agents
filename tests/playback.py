"""Playback: a test-only backend that replays a recorded run's model outputs.

It is not in `src/` or the registry, and nothing in the real run path can build it: tests
inject it as the dispatcher's adapters mapping, so the real Runner, Dispatcher, gates and
provenance log run over a recorded design without calling a model.

Outputs are keyed by stage and operation (`<stage>/<operation>.json` under the fixture).
Replaying one changes exactly one field: a design document's `manifest_hash` names the
context manifest it was written against, which covers the commit and the policy digests, so
it is rebound from the recorded hash to the replay run's own. Anything else in the document
is written as recorded. A document carrying any other hash is refused, not rebound.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_pipeline.adapters import DECISION_TASKS, decision_requests, default_task, embed_inputs
from agentic_pipeline.dispatch import AdapterResult, DispatchGrant, Identity, PreparedCall
from agentic_pipeline.triage import decision_prompt

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTEXT = "pinned_context_manifest"
# A test double claims every isolation check; the registry vocabulary, not a guarantee.
ALL_ISOLATION = frozenset(
    {
        "proposal_path_layout",
        "sandbox_enforcement",
        "session_separation",
        "sealed_input_verification",
        "protected_paths_read_only",
        "network_disabled",
        "broker_mediated_inference",
    }
)


@dataclass(frozen=True, slots=True)
class Recording:
    root: Path
    run_id: str
    context_hash: str
    calls: Mapping[str, Mapping[str, Any]]  # "<stage>/<operation>" -> the recorded model_call

    @classmethod
    def load(cls, name: str, root: Path = FIXTURES) -> Recording:
        base = root / name
        index = json.loads((base / "recording.json").read_text(encoding="utf-8"))
        return cls(base, index["run_id"], index["context_hash"], index["calls"])

    @property
    def entry_spec(self) -> Path:
        return self.root / "entry_spec.json"

    def output(self, key: str) -> dict[str, Any]:
        return json.loads((self.root / f"{key}.json").read_text(encoding="utf-8"))

    def adapters(self) -> dict[str, PlaybackAdapter]:
        """One adapter per recorded backend: the mapping a test hands the Dispatcher."""
        backends = sorted({call["backend"] for call in self.calls.values()})
        return {backend: PlaybackAdapter(self, backend) for backend in backends}


@dataclass(slots=True)
class PlaybackAdapter:
    recording: Recording
    backend: str
    replayed: list[str] = field(default_factory=list)
    enforced_isolation: frozenset[str] = ALL_ISOLATION

    def _call(self, grant: DispatchGrant) -> Mapping[str, Any]:
        key = f"{grant.stage}/{grant.operation_id}"
        call = self.recording.calls.get(key)
        if call is None:
            raise LookupError(f"{key}: not in the recording")
        if call["backend"] != grant.backend:
            raise LookupError(
                f"{key}: recorded on {call['backend']}, dispatched to {grant.backend}"
            )
        return call

    def identity(self) -> Identity:
        call = next(c for c in self.recording.calls.values() if c["backend"] == self.backend)
        return Identity(
            model=call["model"],
            model_version=call["model_version"],
            access_method=f"playback of {self.recording.run_id}: {call['access_method']}",
        )

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        """The prompt the real adapter would build, so a decision's prompt hash binds as live."""
        self._call(grant)
        if grant.operation_id in DECISION_TASKS:
            prompt = decision_prompt(decision_requests(grant))
        else:
            prompt = f"{default_task(grant)}\n\n{embed_inputs(grant.inputs)}".encode()
        return PreparedCall(**asdict(self.identity()), prompt=prompt)

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        recorded = self._call(grant)
        key = f"{grant.stage}/{grant.operation_id}"
        document = self.recording.output(key)
        if "manifest_hash" in document:
            if document["manifest_hash"] != self.recording.context_hash:
                return AdapterResult("error", f"{key}: manifest_hash is not the recorded context")
            path = grant.inputs[CONTEXT] / "context_manifest.json"
            document["manifest_hash"] = json.loads(path.read_text(encoding="utf-8"))["hash"]
        if grant.operation_id in DECISION_TASKS:
            asked = [request["id"] for request in json.loads(call.prompt)]
            answered = [result["id"] for result in document["results"]]
            if answered != asked:
                return AdapterResult("error", f"{key}: recorded answers are not for these requests")
        output = grant.write_path / f"{grant.operation_id}.json"
        output.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        self.replayed.append(key)
        return AdapterResult("ok", "playback", recorded["resolved_model"])

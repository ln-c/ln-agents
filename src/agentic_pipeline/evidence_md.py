"""The decision a human makes at H1, R1 or R2, as a Markdown report they can read.

The report is a view, not what gets signed. An approval binds to the JSON evidence, whose
digests close the report under "What you sign". It is rebuilt only from sealed state, gate
functions and frozen policy, so two renders of the same run are identical.

Model output appears as data: characters that would become links, HTML or table breaks are
escaped, so a proposal cannot make the report say something it does not show.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from agentic_pipeline.approvals import APPROVE_GUARD, REMEDIATION_CAUSES
from agentic_pipeline.gates import (
    CANDIDATE_OPERATIONS,
    CRITIC_STAGES,
    DESIGN_INPUTS,
    SCORERS,
    Design,
    VetoReport,
    _backend,
    _load_json,
    _sealed,
    build_scores,
    triage_report,
    usable_critiques,
)
from agentic_pipeline.run_state import PolicySnapshot

if TYPE_CHECKING:
    from agentic_pipeline.runner import HandlerContext

CANDIDATE_NAMES = {
    "proposal_a_sealed": "Design A",
    "proposal_b_sealed": "Design B",
    "synthesis_sealed": "the synthesis",
}
STAGE_OF = {"proposal_a": "A1", "proposal_b": "A1", "synthesis": "A3"}
REVIEWED = {critic: candidate for candidate, (critic, _, _) in SCORERS.items()}
NODE_TITLES = {"H1": "Design decision", "R1": "Merge decision", "R2": "Deploy decision"}
ESCAPES = str.maketrans({c: f"\\{c}" for c in "\\|<>[]"})
PROSE = frozenset({"details", "description", "summary", "responsibility"})


def choices(node: str, guards: Sequence[str], evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every decision a human can sign here, one per candidate or cause where one is needed."""
    found: list[dict[str, Any]] = []
    for guard in guards:
        if node == "H1" and guard == APPROVE_GUARD:
            for candidate in evidence["survivors"]:
                name = CANDIDATE_NAMES.get(candidate, candidate)
                found.append(_choice(guard, f"Approve {name}", candidate=candidate))
        elif node == "R1" and guard == "deny":
            for cause in REMEDIATION_CAUSES:
                label = f"Deny: {cause.replace('_', ' ')}"
                found.append(_choice(guard, label, remediation_cause=cause))
        else:
            found.append(_choice(guard, guard.replace("_", " ").capitalize()))
    return found


def _choice(
    decision: str,
    label: str,
    *,
    candidate: str | None = None,
    remediation_cause: str | None = None,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "label": label,
        "candidate": candidate,
        "remediation_cause": remediation_cause,
    }


def render(
    ctx: HandlerContext,
    policy: PolicySnapshot,
    evidence: Mapping[str, Any],
    offered: Sequence[Mapping[str, Any]],
) -> str:
    """The Markdown report for the human state `ctx.node`."""
    title = NODE_TITLES.get(ctx.node, "Decision")
    lines = [
        f"# {title} for run `{ctx.run_id}` ({ctx.node})",
        "",
        "Nothing in this report approves anything. A decision is a document you sign with your "
        "ssh key; `run decide` checks the signature and that the evidence below is unchanged.",
        "",
        "## Your choices",
        "",
        *(_choice_line(c) for c in offered),
        "",
    ]
    if ctx.node == "H1":
        lines += _design(ctx, policy)
    lines += _signed(evidence)
    return "\n".join(lines).rstrip() + "\n"


def _choice_line(choice: Mapping[str, Any]) -> str:
    extra = [f"`{choice['decision']}`"]
    if choice["candidate"]:
        extra.append(f"candidate `{choice['candidate']}`")
    if choice["remediation_cause"]:
        extra.append(f"cause `{choice['remediation_cause']}`")
    return f"- **{choice['label']}** ({', '.join(extra)})"


# ── H1 ───────────────────────────────────────────────────────────────────────


def _design(ctx: HandlerContext, policy: PolicySnapshot) -> list[str]:
    design = Design(ctx)
    report = VetoReport(ctx)
    scores = build_scores(ctx, policy, report)
    _, triage = triage_report(ctx, policy)
    _, critiques = usable_critiques(ctx)
    review_path = _sealed(ctx, ["synthesis_review_sealed"])["synthesis_review_sealed"]
    reviews = {**critiques, "synthesis_review": _load_json(review_path, "synthesis_review")}
    lines = _overview(ctx, design, report, scores)
    for candidate in DESIGN_INPUTS:
        lines += _candidate(ctx, candidate, design.docs.get(candidate), scores)
    lines += _findings(ctx, reviews, triage, design.docs.get("synthesis_sealed") or {})
    return lines


def _author(ctx: HandlerContext, candidate: str) -> str:
    operation = CANDIDATE_OPERATIONS[candidate]
    return f"`{_backend(ctx, STAGE_OF[operation], operation)}`"


def _overview(
    ctx: HandlerContext, design: Design, report: VetoReport, scores: Mapping[str, Any]
) -> list[str]:
    lines = [
        "## Candidates at a glance",
        "",
        "| Candidate | Written by | Vetoes fired | Vetoes undetermined | Weighted score | Rank |",
        "|---|---|---|---|---|---|",
    ]
    ranking = scores["ranking"]
    for candidate in DESIGN_INPUTS:
        scored = scores["candidates"].get(candidate, {})
        weighted = scored.get("weighted")
        rank = ranking.index(candidate) + 1 if candidate in ranking else "-"
        lines.append(
            f"| {CANDIDATE_NAMES[candidate]} | {_author(ctx, candidate)} "
            f"| {_list(report.fired.get(candidate))} | {_list(report.undetermined.get(candidate))} "
            f"| {'-' if weighted is None else f'{weighted:.2f}'} | {rank} |"
        )
    flags = scores["flags"]
    lines += [
        "",
        f"Near tie: **{_yes(flags['near_tie'])}**. "
        f"High uncertainty: **{_yes(flags['high_uncertainty'])}**. "
        f"Vetoes not evaluated by code: {_list(report.unevaluated)}.",
        "",
    ]
    for candidate, scored in sorted(scores["candidates"].items()):
        for reason in scored["reasons"]:
            lines.append(f"- {CANDIDATE_NAMES[candidate]}: {_text(reason)}")
    return [*lines, ""] if lines[-1] else lines


def _candidate(
    ctx: HandlerContext, candidate: str, doc: Mapping[str, Any] | None, scores: Mapping[str, Any]
) -> list[str]:
    name = CANDIDATE_NAMES[candidate]
    lines = [f"## {name[0].upper()}{name[1:]} (`{candidate}`)", ""]
    if doc is None:
        return [*lines, "Its output is not a valid proposal, so it cannot be chosen.", ""]
    lines += [f"Written by {_author(ctx, candidate)}.", "", "### Components", ""]
    for component in doc["topology"]["components"]:
        where = [str(component[k]) for k in ("kind", "location") if component.get(k)]
        rest = {k: v for k, v in component.items() if k not in ("name", "kind", "location")}
        about = " ".join(str(v) if k in PROSE else f"{k}: {v}" for k, v in rest.items())
        head = f"- **{_text(component['name'])}**"
        lines.append(f"{head} ({_text(', '.join(where))})" if where else head)
        if about:
            lines.append(f"  {_text(about)}")
    lines.append("")
    lines += _items("Dependencies it adds", doc.get("dependencies"), _dependency)
    lines += _items("Data flows", doc.get("data_flows"), _flow)
    lines += _items("Contracts it changes", doc.get("contracts_touched"), _contract)
    lines += _items("Constraints it addresses", doc.get("constraints_addressed"), _text)
    criteria = [
        v
        for v in scores["values"]
        if v["candidate"] == candidate and v["type"] == "criterion_score"
    ]
    if criteria:
        lines += ["### Scores from its review (0 worst, 4 best)", ""]
        lines += [", ".join(f"{v['criterion']} **{v['value']}**" for v in criteria), ""]
    return lines


def _items(heading: str, items: Any, show: Any) -> list[str]:
    if items is None:
        unstated = "_Not stated, so the veto that depends on it is not evaluated._"
        return [f"### {heading}", "", unstated, ""]
    if not items:
        return [f"### {heading}", "", "None.", ""]
    return [f"### {heading}", "", *(f"- {show(item)}" for item in items), ""]


def _dependency(dep: Mapping[str, Any]) -> str:
    rest = {k: v for k, v in dep.items() if k not in ("name", "ecosystem")}
    tail = "; ".join(f"{k}: {v}" for k, v in rest.items())
    return f"{_code(dep['name'])} ({_text(dep['ecosystem'])}){': ' + _text(tail) if tail else ''}"


def _flow(flow: Mapping[str, Any]) -> str:
    rest = {k: v for k, v in flow.items() if k not in ("id", "data_class", "destination")}
    tail = "; ".join(f"{k}: {v}" for k, v in rest.items())
    head = (
        f"{_code(flow['id'])}: {_text(flow['data_class'])} to {_text(flow.get('destination', '?'))}"
    )
    return f"{head}{': ' + _text(tail) if tail else ''}"


def _contract(contract: Mapping[str, Any]) -> str:
    return f"{_code(contract['path'])}: {_text(contract['change'])}"


def _findings(
    ctx: HandlerContext,
    reviews: Mapping[str, Any],
    triage: Mapping[str, Any],
    synthesis: Mapping[str, Any],
) -> list[str]:
    judged = {(f["critique"], f["finding_id"]): f for f in triage["findings"]}
    resolved = {(r["critique"], r["finding_id"]): r for r in synthesis.get("resolutions", [])}
    lines = [
        "## Findings",
        "",
        f"Each critique finding was judged by `{_text(triage['model'])}`: the probability that "
        f"the design really has the problem. A finding at or above {triage['threshold']} counts "
        "as valid.",
        "",
    ]
    if triage["waived_blocking"]:
        lines += ["**Blocking findings judged not valid (waived):**", ""]
        for f in triage["waived_blocking"]:
            lines.append(f"- {f['critique']} {f['finding_id']} (p = {f['probability']:.2f})")
        lines.append("")
    for operation, doc in reviews.items():
        target = REVIEWED[operation]
        sealed = f"{operation}_sealed"
        reviewer = _backend(ctx, CRITIC_STAGES[operation], operation)
        lines += [f"### `{operation}`: `{reviewer}` on {CANDIDATE_NAMES[target]}", ""]
        for finding in doc["findings"]:
            key = (sealed, finding["id"])
            verdict = judged.get(key)
            valid = (
                f"{'valid' if verdict['valid'] else 'not valid'} (p = {verdict['probability']:.2f})"
                if verdict
                else "not judged"
            )
            head = [f"**{_text(finding['id'])}**", finding["severity"].replace("_", " "), valid]
            if finding.get("subject"):
                head.append(f"*{_text(finding['subject'])}*")
            lines += [f"- {' · '.join(head)}", f"  {_text(finding['description'])}"]
            if resolution := resolved.get(key):
                note = f": {resolution['note']}" if resolution.get("note") else ""
                lines.append(f"  Synthesis: {_text(resolution['disposition'] + note)}")
        lines.append("")
    return lines


# ── shared ───────────────────────────────────────────────────────────────────


def _signed(evidence: Mapping[str, Any]) -> list[str]:
    lines = [
        "## What you sign",
        "",
        "Your signature binds to this evidence. If any of it changes, the approval is refused.",
        "",
    ]
    for key in sorted(evidence):
        value = evidence[key]
        if isinstance(value, Mapping) and key == "policy":
            lines.append(f"- `{key}`: {len(value)} frozen policy files")
        else:
            lines.append(f"- `{key}`: {_code(value)}")
    return [*lines, ""]


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _list(items: Sequence[str] | None) -> str:
    return ", ".join(f"`{i}`" for i in items) if items else "none"


def _text(value: Any) -> str:
    """Model text on one line, unable to become a link, HTML or a table break."""
    return " ".join(str(value).split()).translate(ESCAPES)


def _code(value: Any) -> str:
    """A value as inline code: escapes do not apply there, so backticks are dropped."""
    shown = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return f"`{' '.join(shown.replace('`', '').split())}`"

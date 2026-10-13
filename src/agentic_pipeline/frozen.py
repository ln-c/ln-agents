"""Handlers read policy only through here, so they see the bytes the run froze."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentic_pipeline.run_state import PolicySnapshot, RunStateError, load_policy

if TYPE_CHECKING:
    from agentic_pipeline.runner import HandlerContext


class PolicyChanged(Exception):
    pass


def frozen_policy(ctx: HandlerContext) -> PolicySnapshot:
    """Policy as read now, which must still be the bytes this run froze at start."""
    try:
        snap = load_policy(ctx.root)
    except RunStateError as exc:
        raise PolicyChanged(str(exc)) from exc
    if snap.digests != dict(ctx.policy_digests):
        raise PolicyChanged("graph or policy changed since the run started")
    return snap

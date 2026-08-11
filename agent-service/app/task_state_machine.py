"""Task lifecycle rules kept separate from individual Agent run transitions."""

from typing import FrozenSet


TERMINAL_STATES: FrozenSet[str] = frozenset({"closed", "cancelled"})

TRANSITIONS = {
    "draft": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"assigned", "in_progress", "attention_required", "cancelled"}),
    "assigned": frozenset({"queued", "in_progress", "attention_required", "cancelled"}),
    "in_progress": frozenset({"waiting_confirmation", "awaiting_proposer_close", "attention_required", "cancelled"}),
    "waiting_confirmation": frozenset({"in_progress", "attention_required", "cancelled"}),
    "awaiting_proposer_close": frozenset({"in_progress", "assigned", "attention_required", "closed", "cancelled"}),
    "attention_required": frozenset({"queued", "assigned", "in_progress", "cancelled"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """A repeated state request is safe; all other jumps are explicit."""
    return current == target or target in TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise ValueError("非法任务状态转换：{} -> {}".format(current, target))

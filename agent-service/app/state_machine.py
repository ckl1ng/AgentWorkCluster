"""Explicit Agent run transitions shared by API and workers."""

from typing import FrozenSet


TERMINAL_STATES: FrozenSet[str] = frozenset({"completed", "failed", "cancelled"})
TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"waiting_confirmation", "completed", "failed", "cancelled"}),
    "waiting_confirmation": frozenset({"running", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """A repeated request for the current state is safe; other jumps are denied."""
    return current == target or target in TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise ValueError("非法运行状态转换：{} -> {}".format(current, target))

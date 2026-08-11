import unittest

from app.state_machine import can_transition, require_transition
from app.task_state_machine import can_transition as can_task_transition, require_transition as require_task_transition


class RunStateMachineTest(unittest.TestCase):
    def test_valid_lifecycle_and_idempotent_transition(self):
        self.assertTrue(can_transition("queued", "running"))
        self.assertTrue(can_transition("running", "waiting_confirmation"))
        self.assertTrue(can_transition("waiting_confirmation", "running"))
        self.assertTrue(can_transition("running", "completed"))
        self.assertTrue(can_transition("completed", "completed"))

    def test_terminal_and_invalid_transitions_are_rejected(self):
        self.assertFalse(can_transition("completed", "running"))
        self.assertFalse(can_transition("queued", "completed"))
        with self.assertRaises(ValueError):
            require_transition("cancelled", "running")

    def test_task_requires_proposer_close_state(self):
        self.assertTrue(can_task_transition("in_progress", "awaiting_proposer_close"))
        self.assertTrue(can_task_transition("awaiting_proposer_close", "closed"))
        self.assertFalse(can_task_transition("in_progress", "closed"))
        with self.assertRaises(ValueError):
            require_task_transition("closed", "in_progress")

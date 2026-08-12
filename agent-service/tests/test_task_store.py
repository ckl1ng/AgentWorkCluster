import tempfile
import threading
import unittest

from cryptography.fernet import Fernet

from app.main import AgentStore, TaskEventHub


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = AgentStore(self.directory.name + "/agent.db", Fernet.generate_key().decode("ascii"))

    def tearDown(self):
        self.directory.cleanup()

    def test_task_context_isolated_and_result_waits_for_proposer(self):
        first = self.store.create_task(7, {"title": "first", "goal": "keep this private"})
        second = self.store.create_task(7, {"title": "second", "goal": "a different task"})

        self.assertEqual(first["state"], "queued")
        self.store.transition_task(first["id"], 7, "in_progress", "task started")
        waiting = self.store.transition_task(first["id"], 7, "awaiting_proposer_close", "result ready", "done")
        self.assertEqual(waiting["state"], "awaiting_proposer_close")
        first_context = self.store.task_context_events(first["id"], 7)
        second_context = self.store.task_context_events(second["id"], 7)
        self.assertIn("keep this private", [event["content"] for event in first_context])
        self.assertNotIn("keep this private", [event["content"] for event in second_context])
        self.assertEqual(self.store.list_notifications(7, unread_only=True)[0]["task_id"], first["id"])

    def test_only_proposer_can_close_or_cancel(self):
        task = self.store.create_task(7, {"title": "ownership", "goal": "verify ownership"})
        self.store.transition_task(task["id"], 7, "in_progress", "task started")
        self.store.transition_task(task["id"], 7, "awaiting_proposer_close", "result ready", "done")

        self.assertIsNone(self.store.transition_task(task["id"], 8, "closed", "forged close", "forged"))
        self.assertEqual(self.store.get_task(task["id"], 7)["state"], "awaiting_proposer_close")
        closed = self.store.transition_task(task["id"], 7, "closed", "accepted", "accepted result")
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["closed_by_id"], "7")

    def test_dispatch_events_are_summaries_not_context(self):
        task = self.store.create_task(7, {"title": "dispatch", "goal": "secret work details"})
        events = self.store.task_dispatch_events(7, task_id=task["id"])
        self.assertTrue(events)
        self.assertNotIn("secret work details", " ".join(event["summary"] for event in events))
        task_outbox = [event for event in self.store.pending_outbox_events() if event["aggregate_type"] == "task"]
        self.assertTrue(task_outbox)
        self.assertNotIn("secret work details", str(task_outbox[0]["payload"]))
        self.assertEqual(task_outbox[0]["payload"]["task_id"], task["id"])

    def test_task_list_is_a_sidebar_projection_without_work_content(self):
        task = self.store.create_task(7, {"title": "projection", "goal": "do not expose this work"})
        self.store.transition_task(task["id"], 7, "in_progress", "任务开始执行")
        self.store.transition_task(task["id"], 7, "awaiting_proposer_close", "等待验收", "private result")

        item = self.store.list_tasks(7)[0]
        self.assertNotIn("goal", item)
        self.assertNotIn("result_summary", item)
        self.assertEqual(item["last_dispatch_event"]["summary"], "等待验收")
        self.assertEqual(item["unread_count"], 1)

    def test_global_task_subscription_is_scoped_to_owner(self):
        async def exercise():
            hub = TaskEventHub()
            owner_queue = await hub.subscribe_all(7)
            other_queue = await hub.subscribe_all(8)
            event = {"task_id": "task-1", "owner_user_id": 7, "sequence": 1}
            await hub.publish(event)
            self.assertEqual(await owner_queue.get(), event)
            self.assertTrue(other_queue.empty())
            await hub.unsubscribe_all(7, owner_queue)
            await hub.unsubscribe_all(8, other_queue)

        import asyncio
        asyncio.run(exercise())

    def test_task_dispatch_events_replay_after_a_sequence_gap(self):
        task = self.store.create_task(7, {"title": "replay", "goal": "replay durable events"})
        initial = self.store.task_dispatch_events(7, task_id=task["id"])
        self.assertEqual(initial[-1]["sequence"], 1)
        self.store.transition_task(task["id"], 7, "in_progress", "任务开始执行")
        replayed = self.store.task_dispatch_events(7, task_id=task["id"], after_sequence=initial[-1]["sequence"])
        self.assertEqual([(event["sequence"], event["event_type"]) for event in replayed], [(2, "task.in_progress")])

    def test_concurrent_task_close_uses_the_state_version_guard(self):
        task = self.store.create_task(7, {"title": "concurrent", "goal": "close once"})
        self.store.transition_task(task["id"], 7, "in_progress", "开始")
        waiting = self.store.transition_task(task["id"], 7, "awaiting_proposer_close", "待验收", "result")
        barrier = threading.Barrier(2)
        outcomes = []

        def close_once():
            barrier.wait()
            try:
                outcomes.append(self.store.transition_task(
                    task["id"], 7, "closed", "收尾", "accepted", expected_state_version=waiting["state_version"],
                )["state"])
            except ValueError as exc:
                outcomes.append(str(exc))

        first, second = threading.Thread(target=close_once), threading.Thread(target=close_once)
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual(outcomes.count("closed"), 1)
        self.assertEqual(sum("状态已更新" in item for item in outcomes), 1)
        self.assertEqual(self.store.get_task(task["id"], 7)["state"], "closed")

    def test_assigned_cloud_task_freezes_task_context_and_requires_executor_result_tool(self):
        agent = self.store.create_agent(7, {
            "name": "Cloud worker", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        task = self.store.create_task(7, {
            "title": "execute", "goal": "perform the isolated task", "assigned_agent_id": agent["id"],
        })
        run = self.store.get_run(task["run_id"], 7)
        snapshot = self.store.run_snapshot(run["id"])

        self.assertEqual(task["state"], "assigned")
        self.assertEqual(run["task_id"], task["id"])
        self.assertEqual(run["assignment_id"], task["assignment_id"])
        self.assertEqual(snapshot["task_prompt_version"], 1)
        self.assertIn("multi-agent task cluster", snapshot["system_prompt"])
        self.assertIn("Different tasks are strictly isolated", snapshot["system_prompt"])
        self.assertEqual(snapshot["task_context_manifest"][0]["kind"], "task.goal")
        self.assertIn("perform the isolated task", snapshot["task_context_messages"][0]["content"])
        self.store.sync_task_run_state(run["id"], "running")
        candidate = self.store.sync_task_run_state(run["id"], "completed", content="completed work")
        self.assertEqual(candidate["state"], "in_progress")
        self.assertIn("completed work", [event["content"] for event in self.store.task_context_events(task["id"], 7)])
        submitted = self.store.execute_task_tool(run["id"], "tool-1", "submit_result", {
            "result": "completed work", "evidence_manifest": {"checks": ["unit"]}, "risk_summary": "none",
        })
        self.assertEqual(submitted["state"], "awaiting_proposer_close")
        self.assertEqual(self.store.get_task(task["id"], 7)["state"], "awaiting_proposer_close")

    def test_task_runs_are_scoped_to_the_task_and_owner(self):
        agent = self.store.create_agent(7, {
            "name": "Run reader", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        first = self.store.create_task(7, {"title": "first", "goal": "first goal", "assigned_agent_id": agent["id"]})
        second = self.store.create_task(7, {"title": "second", "goal": "second goal", "assigned_agent_id": agent["id"]})

        runs = self.store.task_runs(first["id"], 7)
        self.assertEqual([run["id"] for run in runs], [first["run_id"]])
        self.assertNotEqual(second["run_id"], runs[0]["id"])
        self.assertIsNone(self.store.task_runs(first["id"], 8))

    def test_task_budget_limits_snapshot_concurrency_and_usage(self):
        agent = self.store.create_agent(7, {
            "name": "Budget executor", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system", "max_tokens": 128,
        })
        task = self.store.create_task(7, {
            "title": "budget", "goal": "remain bounded", "assigned_agent_id": agent["id"],
            "budget_snapshot": {"max_total_tokens": 5, "max_tool_calls": 1, "max_concurrent_runs": 1},
        })
        run = self.store.get_run(task["run_id"], 7)
        self.assertEqual(self.store.run_snapshot(run["id"])["max_tokens"], 5)
        self.assertEqual(self.store.create_run(task["conversation_id"], 7, "duplicate", task["id"], task["assignment_id"])["error"], "Task 并发运行数已达到上限")

        self.assertTrue(self.store.try_start_run(run["id"]))
        self.store.sync_task_run_state(run["id"], "running")
        self.assertTrue(self.store.record_tool_invocation(run["id"], "task:post_progress", "call-1", "started"))
        self.assertFalse(self.store.record_tool_invocation(run["id"], "task:post_progress", "call-2", "started"))
        self.store.update_usage(run["id"], {"total_tokens": 6}, {})
        self.assertEqual(self.store.get_task(task["id"], 7)["state"], "attention_required")

    def test_task_confirmation_and_recovery_keep_task_scope(self):
        agent = self.store.create_agent(7, {
            "name": "Recovery executor", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        task = self.store.create_task(7, {"title": "recovery", "goal": "recover only this task", "assigned_agent_id": agent["id"]})
        run = self.store.get_run(task["run_id"], 7)
        snapshot = self.store.run_snapshot(run["id"])
        self.assertTrue(self.store.try_start_run(run["id"]))
        self.store.sync_task_run_state(run["id"], "running")
        confirmation = self.store.create_confirmation(run["id"], "call-1", "write", {"path": "safe"}, {"messages": []})
        self.assertEqual(confirmation["task_id"], task["id"])
        self.assertEqual(self.store.task_confirmations(task["id"], 7)[0]["id"], confirmation["id"])
        self.assertIsNone(self.store.task_confirmations(task["id"], 8))
        self.store.update_run(run["id"], "waiting_confirmation")
        self.store.sync_task_run_state(run["id"], "waiting_confirmation")
        self.assertEqual(self.store.get_task(task["id"], 7)["state"], "waiting_confirmation")
        self.assertTrue(self.store.try_start_run(run["id"], recover=True))
        self.assertEqual(self.store.run_snapshot(run["id"])["task_context_manifest"], snapshot["task_context_manifest"])

    def test_task_budget_schema_rejects_unknown_or_invalid_values(self):
        with self.assertRaises(ValueError):
            self.store.create_task(7, {"title": "bad", "goal": "bad", "budget_snapshot": {"credits": 1}})
        with self.assertRaises(ValueError):
            self.store.create_task(7, {"title": "bad", "goal": "bad", "budget_snapshot": {"max_tool_calls": -1}})

    def test_delegate_task_creates_isolated_child_with_budgeted_handoff(self):
        parent_agent = self.store.create_agent(7, {
            "name": "Parent", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        child_agent = self.store.create_agent(7, {
            "name": "Child", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        parent = self.store.create_task(7, {
            "title": "parent", "goal": "parent-only secret", "assigned_agent_id": parent_agent["id"],
            "budget_snapshot": {"max_total_tokens": 100, "max_concurrent_runs": 2, "max_depth": 2, "max_subtasks": 2},
        })
        self.assertTrue(self.store.try_start_run(parent["run_id"]))
        self.store.sync_task_run_state(parent["run_id"], "running")
        delegated = self.store.execute_task_tool(parent["run_id"], "delegate-1", "delegate_task", {
            "target_agent_id": child_agent["id"], "title": "child", "goal": "inspect selected input",
            "input_package": "only this explicitly selected handoff", "budget_snapshot": {"max_total_tokens": 20, "max_depth": 1, "max_subtasks": 1},
            "reason": "needs an independent review",
        })
        self.assertEqual(delegated["status"], "ok")
        child = self.store.get_task(delegated["child_task_id"], 7)
        self.assertEqual(child["run_id"], delegated["child_run_id"])
        self.assertEqual(child["parent_task_id"], parent["id"])
        self.assertEqual(child["root_task_id"], parent["id"])
        self.assertEqual(child["proposer_kind"], "agent")
        self.assertEqual(child["proposer_id"], parent_agent["id"])
        self.assertEqual(child["current_assignment"]["executor_id"], child_agent["id"])
        child_context = self.store.task_context_events(child["id"], 7)
        contents = [event["content"] for event in child_context]
        self.assertIn("only this explicitly selected handoff", contents)
        self.assertNotIn("parent-only secret", contents)
        handoff = self.store.db.execute("SELECT * FROM task_handoffs WHERE to_task_id = ?", (child["id"],)).fetchone()
        self.assertEqual(handoff["from_task_id"], parent["id"])

        self.assertTrue(self.store.try_start_run(child["run_id"]))
        self.store.sync_task_run_state(child["run_id"], "running")
        delivered = self.store.execute_task_tool(child["run_id"], "result-1", "submit_result", {
            "result": "child's selected conclusion", "evidence_manifest": {"check": "ok"}, "risk_summary": "none",
        })
        self.assertEqual(delivered["state"], "awaiting_proposer_close")
        collected = self.store.execute_task_tool(parent["run_id"], "collect-1", "collect_child_result", {"child_task_id": child["id"]})
        self.assertEqual(collected["result"], "child's selected conclusion")
        self.assertIn("child's selected conclusion", [event["content"] for event in self.store.task_context_events(parent["id"], 7)])
        closed = self.store.execute_task_tool(parent["run_id"], "close-child-1", "close_delegated_task", {
            "child_task_id": child["id"], "result_summary": "accepted child result",
        })
        self.assertEqual(closed["state"], "closed")

    def test_delegate_task_rejects_depth_and_parent_budget_bypass(self):
        first_agent = self.store.create_agent(7, {
            "name": "First", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        second_agent = self.store.create_agent(7, {
            "name": "Second", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        parent = self.store.create_task(7, {
            "title": "bounded", "goal": "bounded", "assigned_agent_id": first_agent["id"],
            "budget_snapshot": {"max_total_tokens": 10, "max_concurrent_runs": 2, "max_depth": 1, "max_subtasks": 1},
        })
        self.assertTrue(self.store.try_start_run(parent["run_id"]))
        self.store.sync_task_run_state(parent["run_id"], "running")
        over_budget = self.store.execute_task_tool(parent["run_id"], "delegate-over", "delegate_task", {
            "target_agent_id": second_agent["id"], "title": "too much", "goal": "too much", "input_package": "x",
            "budget_snapshot": {"max_total_tokens": 11}, "reason": "x",
        })
        self.assertEqual(over_budget["status"], "error")
        self.assertIn("超过父任务", over_budget["error"])

    def test_task_create_idempotency_and_reassignment_keep_attempt_history(self):
        first_agent = self.store.create_agent(7, {
            "name": "First", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        second_agent = self.store.create_agent(7, {
            "name": "Second", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        payload = {
            "title": "idempotent", "goal": "do one task", "assigned_agent_id": first_agent["id"],
            "idempotency_key": "request-1",
        }
        task = self.store.create_task(7, payload)
        repeated = self.store.create_task(7, payload)
        self.assertEqual(repeated["id"], task["id"])
        self.assertEqual(len(self.store.task_assignments(task["id"], 7)), 1)

        reassigned = self.store.assign_cloud_task(task["id"], 7, second_agent["id"], "assign-2")
        assignments = self.store.task_assignments(task["id"], 7)
        self.assertEqual(reassigned["assignment_id"], assignments[-1]["id"])
        self.assertEqual([item["attempt"] for item in assignments], [1, 2])
        self.assertEqual(assignments[0]["state"], "superseded")
        self.assertEqual(assignments[1]["state"], "assigned")
        run = self.store.get_run(reassigned["run_id"], 7)
        self.assertEqual(run["assignment_id"], assignments[-1]["id"])

    def test_resume_task_creates_a_fresh_assignment_and_run(self):
        agent = self.store.create_agent(7, {
            "name": "Rework executor", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        task = self.store.create_task(7, {"title": "rework", "goal": "improve the result", "assigned_agent_id": agent["id"]})
        first_assignment = self.store.task_assignments(task["id"], 7)[0]
        self.store.sync_task_run_state(task["run_id"], "running")
        self.store.submit_task_result(
            task["id"], 7, first_assignment["id"], {"kind": "cloud_agent", "id": agent["id"]}, "first result",
        )

        resumed = self.store.resume_task(task["id"], 7, "rework-1")

        self.assertEqual(resumed["state"], "assigned")
        self.assertNotEqual(resumed["run_id"], task["run_id"])
        assignments = self.store.task_assignments(task["id"], 7)
        self.assertEqual([item["attempt"] for item in assignments], [1, 2])
        self.assertEqual(assignments[0]["state"], "completed")
        self.assertEqual(assignments[1]["state"], "assigned")
        self.assertEqual(self.store.get_run(resumed["run_id"], 7)["state"], "queued")

    def test_executor_submission_requires_matching_assignment_and_creates_result(self):
        agent = self.store.create_agent(7, {
            "name": "Executor", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        task = self.store.create_task(7, {
            "title": "result", "goal": "produce evidence", "assigned_agent_id": agent["id"],
        })
        assignment = self.store.task_assignments(task["id"], 7)[0]
        self.store.sync_task_run_state(task["run_id"], "running")
        principal = {"kind": "cloud_agent", "id": agent["id"]}

        with self.assertRaises(PermissionError):
            self.store.submit_task_result(task["id"], 7, assignment["id"], {"kind": "cloud_agent", "id": "other"}, "forged")

        submitted = self.store.submit_task_result(
            task["id"], 7, assignment["id"], principal, "verified outcome",
            evidence_manifest={"checks": ["unit"]}, risk_summary="none",
        )
        self.assertEqual(submitted["state"], "awaiting_proposer_close")
        results = self.store.task_results(task["id"], 7)
        self.assertEqual(results[0]["result"], "verified outcome")
        self.assertEqual(results[0]["evidence_manifest"], {"checks": ["unit"]})

    def test_handoff_requires_source_principal_and_writes_only_destination_context(self):
        agent = self.store.create_agent(7, {
            "name": "Handoff executor", "base_url": "https://model.example/v1", "api_key": "secret",
            "model_id": "test-model", "system_prompt": "system",
        })
        source = self.store.create_task(7, {
            "title": "source", "goal": "private source", "assigned_agent_id": agent["id"],
        })
        destination = self.store.create_task(7, {"title": "destination", "goal": "private destination"})
        handoff = self.store.record_task_handoff(
            7, source["id"], destination["id"], {"kind": "cloud_agent", "id": agent["id"]},
            "cloud_agent", agent["id"], {"selection": ["summary"]}, "authorized transfer",
        )
        self.assertIsNotNone(handoff)
        destination_context = self.store.task_context_events(destination["id"], 7)
        self.assertIn("authorized transfer", [event["content"] for event in destination_context])
        self.assertIsNone(self.store.record_task_handoff(
            7, source["id"], destination["id"], {"kind": "cloud_agent", "id": "other"},
            "cloud_agent", agent["id"], {}, "forged transfer",
        ))


if __name__ == "__main__":
    unittest.main()

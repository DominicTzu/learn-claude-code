import importlib.util
import io
import itertools
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI


MODULE_PATH = Path(__file__).resolve().parents[1] / "s14_cron_scheduler" / "code.py"
REAL_THREAD = threading.Thread


def tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": arguments,
    }}


def reply(content="done", calls=None, finish_reason=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return httpx.Response(200, json={
        "id": "test-completion", "object": "chat.completion", "created": 0,
        "model": "test-model", "choices": [{
            "index": 0, "message": message,
            "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
        }],
    })


class CronSchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name).resolve()
        self.requests = []
        self.events = []
        self.workers = []
        self.release_events = []
        self.output = io.StringIO()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(redirect_stdout(self.output))

        def make_client(**kwargs):
            return OpenAI(
                **kwargs, max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(self.complete)),
            )

        # Never read the user's .env, call a real API, or load real scheduling rules.
        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key", "LLM_MODEL_ID": "test-model",
            "LLM_BASE_URL": "https://example.invalid/v1",
        }), patch("dotenv.load_dotenv"), patch("pathlib.Path.cwd", return_value=self.workdir), \
                patch("openai.OpenAI", side_effect=make_client), \
                patch("threading.Thread") as thread_factory:
            spec = importlib.util.spec_from_file_location("s14_under_test", MODULE_PATH)
            self.agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.agent)
            thread_factory.assert_not_called()
        self.addCleanup(self.agent.client.close)
        contexts.enter_context(patch.object(self.agent.random, "randint", side_effect=itertools.count(1)))
        contexts.enter_context(patch.object(self.agent.threading, "Thread", side_effect=self.make_worker))
        self.addCleanup(self.finish_workers)

    def make_worker(self, *args, **kwargs):
        worker = REAL_THREAD(*args, **kwargs)
        self.workers.append(worker)
        return worker

    def join_workers(self):
        for worker in list(self.workers):
            if worker.ident is not None:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), "test worker did not stop")

    def finish_workers(self):
        for event in self.release_events:
            event.set()
        self.join_workers()

    def complete(self, request):
        self.requests.append(json.loads(request.content))
        if not self.events:
            raise AssertionError("unexpected extra API request")
        event = self.events.pop(0)
        return event() if callable(event) else event

    def run_loop(self, history=None):
        if history is None:
            history = [{"role": "user", "content": "test request"}]
        with self.agent.agent_lock:
            self.agent.agent_loop(history, {})
        return history

    def schedule(self, cron="* * * * *", prompt="read-only check", **kwargs):
        kwargs.setdefault("durable", False)
        job = self.agent.schedule_job(cron, prompt, **kwargs)
        self.assertIsInstance(job, self.agent.CronJob)
        return job

    def assert_tool_pairs(self, messages):
        pending = []
        for message in messages:
            if message["role"] == "tool":
                self.assertIn(message["tool_call_id"], pending)
                pending.remove(message["tool_call_id"])
            else:
                self.assertEqual(pending, [], "notification interrupted tool results")
                pending = [call["id"] for call in message.get("tool_calls", [])]
        self.assertEqual(pending, [])

    def test_import_is_idle_and_direct_answer_uses_eleven_openai_schemas(self):
        self.assertEqual(self.requests, [])
        self.assertEqual(self.agent.scheduled_jobs, {})
        self.events = [reply()]
        history = self.run_loop()
        request = self.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["max_tokens"], 8000)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(len(request["tools"]), 11)
        for schema in request["tools"]:
            self.assertEqual(schema["type"], "function")
            self.assertIn("parameters", schema["function"])
            self.assertNotIn("input_schema", schema)
        self.assertEqual(history[-1]["content"], "done")

    def test_model_can_register_list_and_cancel_rules_with_paired_results(self):
        self.events = [reply(None, [
            tool_call("register", "schedule_cron", json.dumps({
                "cron": "0 9 * * *", "prompt": "read-only check", "durable": False,
            })),
            tool_call("list", "list_crons", "{}"),
        ]), reply(None, [tool_call("cancel", "cancel_cron", '{"job_id":"cron_000001"}')]), reply()]
        history = self.run_loop()
        outputs = [message["content"] for message in history if message["role"] == "tool"]
        self.assertIn("Scheduled cron_000001", outputs[0])
        self.assertIn("cron_000001", outputs[1])
        self.assertIn("Cancelled", outputs[2])
        self.assertEqual(self.agent.scheduled_jobs, {})
        for request in self.requests:
            self.assert_tool_pairs(request["messages"])

    def test_bad_tool_arguments_and_unknown_tools_return_errors(self):
        self.events = [reply(None, [
            tool_call("invalid-json", "schedule_cron", "{"),
            tool_call("not-object", "list_crons", "[]"),
            tool_call("unknown", "no_such_tool", "{}"),
            tool_call("invalid-cron", "schedule_cron", '{"cron":"bad","prompt":"check"}'),
            tool_call("wrong-bool", "schedule_cron", '{"cron":"* * * * *","prompt":"check","durable":"false"}'),
        ]), reply()]
        history = self.run_loop()
        self.assertEqual(len([m for m in history if m["role"] == "tool"]), 5)
        self.assertEqual(self.agent.scheduled_jobs, {})
        self.assert_tool_pairs(history)

    def test_truncation_does_not_execute_or_store_partial_calls(self):
        self.events = [reply("partial", [tool_call("bad", "schedule_cron", "{")], "length")]
        history = self.run_loop()
        self.assertIn("Output truncated", history[-1]["content"])
        self.assertNotIn("tool_calls", history[-1])
        self.assertEqual(self.agent.scheduled_jobs, {})

    def test_api_error_is_recorded_and_agent_lock_released(self):
        self.schedule()
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        self.events = [httpx.Response(500, json={"error": {"message": "offline failure"}})]
        self.assertTrue(self.agent.process_queued_jobs())
        self.assertIn("[Error]", self.agent.session_history[-1]["content"])
        self.assertFalse(self.agent.agent_lock.locked())
        self.assertFalse(self.agent.has_cron_queue())

    def test_validation_and_documented_time_examples(self):
        for cron in ["* * * * *", "*/5 * * * *", "0 9 * * *", "0 9 * * 1-5", "30 9,18 * * *"]:
            with self.subTest(cron=cron):
                self.assertIsNone(self.agent.validate_cron(cron))
        for cron in [None, "", "0 9 * *", "60 9 * * *", "0 24 * * *", "*/0 * * * *", "1-10/2 * * * *", "0 9 * * 7", "0 9 * * MON"]:
            with self.subTest(cron=cron):
                self.assertIsNotNone(self.agent.validate_cron(cron))
        self.assertTrue(self.agent.cron_matches("*/5 * * * *", datetime(2026, 11, 3, 9, 5, 21)))
        self.assertFalse(self.agent.cron_matches("*/5 * * * *", datetime(2026, 11, 3, 9, 8)))

    def test_day_of_month_and_weekday_use_or(self):
        matches = self.agent.cron_matches
        self.assertTrue(matches("0 9 1 * 1", datetime(2026, 12, 1, 9)))
        self.assertTrue(matches("0 9 1 * 1", datetime(2026, 11, 2, 9)))
        self.assertFalse(matches("0 9 1 * 1", datetime(2026, 11, 3, 9)))
        self.assertTrue(matches("0 9 * * 0", datetime(2026, 11, 1, 9)))

    def test_deduplication_includes_date(self):
        job = self.schedule("0 9 * * *")
        for now in [datetime(2026, 11, 3, 9, 0, 1), datetime(2026, 11, 3, 9, 0, 59), datetime(2026, 11, 4, 9)]:
            self.agent.check_scheduled_jobs(now)
        self.assertEqual(self.agent.cron_queue, [job, job])
        self.assertEqual(self.agent._last_fired[job.id], "2026-11-04 09:00")
        self.assertEqual(self.requests, [])

    def test_one_shot_removed_before_processing(self):
        job = self.schedule(recurring=False, durable=True)
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        self.assertNotIn(job.id, self.agent.scheduled_jobs)
        self.assertEqual(self.agent.cron_queue, [job])
        self.assertEqual(json.loads(self.agent.DURABLE_PATH.read_text()), [])
        self.assertEqual(self.requests, [])

    def test_cancel_does_not_remove_queued_work(self):
        job = self.schedule(durable=True)
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        self.agent.cancel_job(job.id)
        self.assertEqual(self.agent.cron_queue, [job])
        self.assertEqual(self.agent.scheduled_jobs, {})
        self.assertEqual(json.loads(self.agent.DURABLE_PATH.read_text()), [])
        self.assertEqual(self.agent.consume_cron_queue(), [job])
        self.assertEqual(self.agent.consume_cron_queue(), [])

    def test_only_durable_rules_reload_without_catchup(self):
        durable = self.schedule("0 9 * * *", durable=True)
        self.schedule()
        self.agent.scheduled_jobs.clear()
        self.agent.load_durable_jobs()
        self.assertEqual(list(self.agent.scheduled_jobs), [durable.id])
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 10))
        self.assertFalse(self.agent.has_cron_queue())
        self.agent.check_scheduled_jobs(datetime(2026, 11, 4, 9))
        self.agent.consume_cron_queue()
        self.agent._last_fired.clear()
        self.agent.check_scheduled_jobs(datetime(2026, 11, 4, 9, 0, 20))
        self.assertTrue(self.agent.has_cron_queue())

    def test_corrupt_records_do_not_hide_later_good_rules(self):
        job = self.agent.CronJob("good", "0 9 * * *", "check", True, True)
        records = [None, {"id": "missing"}, {**asdict(job), "cron": "bad"}, asdict(job)]
        self.agent.DURABLE_PATH.write_text(json.dumps(records))
        self.agent.load_durable_jobs()
        self.assertEqual(list(self.agent.scheduled_jobs), ["good"])
        self.assertIn("skipping invalid job", self.output.getvalue())

    def test_corrupt_file_is_reported_without_overwriting(self):
        self.agent.DURABLE_PATH.write_text("{")
        self.agent.load_durable_jobs()
        self.assertIn("cron load error", self.output.getvalue())
        self.assertEqual(self.agent.DURABLE_PATH.read_text(), "{")

    def test_all_rule_mutation_saves_hold_cron_lock(self):
        original = self.agent.save_durable_jobs

        def save():
            self.assertTrue(self.agent.cron_lock.locked())
            original()

        with patch.object(self.agent, "save_durable_jobs", side_effect=save) as saver:
            recurring = self.schedule(durable=True)
            self.agent.cancel_job(recurring.id)
            self.schedule(recurring=False, durable=True)
            self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        self.assertEqual(saver.call_count, 4)

    def test_bad_rule_does_not_stop_other_rules(self):
        self.agent.scheduled_jobs["bad"] = self.agent.CronJob("bad", "bad * * * *", "bad", True, False)
        good = self.schedule()
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        self.assertEqual(self.agent.cron_queue, [good])
        self.assertIn("cron error", self.output.getvalue())

    def test_rule_id_collision_does_not_overwrite(self):
        original = self.schedule()
        with patch.object(self.agent.random, "randint", side_effect=[1, 2]):
            new = self.schedule()
        self.assertNotEqual(original.id, new.id)
        self.assertEqual(len(self.agent.scheduled_jobs), 2)

    def test_idle_queue_processor_automatically_calls_model(self):
        self.schedule(prompt="first request")
        self.schedule(prompt="second request")
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        self.events = [reply("scheduled work received")]
        self.assertTrue(self.agent.process_queued_jobs())
        self.assertEqual([m["content"] for m in self.requests[0]["messages"] if m["role"] == "user"],
                         ["[Scheduled] first request", "[Scheduled] second request"])
        self.assertFalse(self.agent.process_queued_jobs())
        self.assertFalse(self.agent.agent_lock.locked())

    def test_busy_agent_leaves_requests_queued(self):
        job = self.schedule()
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        with self.agent.agent_lock:
            self.assertFalse(self.agent.process_queued_jobs())
        self.assertEqual(self.agent.cron_queue, [job])
        self.assertEqual(self.requests, [])

    def test_queue_is_rechecked_after_acquiring_lock(self):
        with patch.object(self.agent, "has_cron_queue", side_effect=[True, False]):
            self.assertFalse(self.agent.process_queued_jobs())
        self.assertFalse(self.agent.agent_lock.locked())
        self.assertEqual(self.requests, [])

    def test_unexpected_local_error_releases_agent_lock(self):
        self.schedule()
        self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
        with patch.object(self.agent, "run_agent_turn_locked", side_effect=RuntimeError("local error")):
            with self.assertRaises(RuntimeError):
                self.agent.process_queued_jobs()
        self.assertFalse(self.agent.agent_lock.locked())

    def test_cron_arriving_during_tools_follows_all_tool_results(self):
        self.schedule(prompt="check after tools")

        def read_file(path):
            self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
            return "file content"

        self.agent.TOOL_HANDLERS["read_file"] = read_file
        self.events = [reply(None, [
            tool_call("read", "read_file", '{"path":"config"}'),
            tool_call("list", "list_crons", "{}"),
        ]), reply()]
        history = self.run_loop()
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "tool", "tool", "user", "assistant"])
        self.assert_tool_pairs(self.requests[1]["messages"])
        self.assertIn("[Scheduled]", history[-2]["content"])

    def test_cron_arriving_during_final_reply_is_delivered_next_turn(self):
        self.schedule(prompt="new event")

        def finish():
            self.agent.check_scheduled_jobs(datetime(2026, 11, 3, 9))
            return reply("first answer")

        self.events = [finish, reply("scheduled answer")]
        with self.agent.agent_lock:
            self.agent.run_agent_turn_locked("initial question")
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.agent.process_queued_jobs())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.agent.session_history[-2]["content"], "[Scheduled] new event")

    def test_memory_index_is_refreshed_before_next_request(self):
        self.agent.MEMORY_DIR.mkdir()
        self.agent.MEMORY_INDEX.write_text("old memory")

        def update_memory(path):
            self.agent.MEMORY_INDEX.write_text("new memory")
            return "updated"

        self.agent.TOOL_HANDLERS["read_file"] = update_memory
        self.events = [reply(None, [tool_call("read", "read_file", '{"path":"ignored"}')]), reply()]
        self.run_loop()
        self.assertIn("old memory", self.requests[0]["messages"][0]["content"])
        self.assertIn("new memory", self.requests[1]["messages"][0]["content"])

    def test_background_flag_is_explicit_and_bash_only(self):
        decide = self.agent.should_run_background
        self.assertTrue(decide("bash", {"command": "pytest"}))
        self.assertFalse(decide("bash", {"command": "pytest", "run_in_background": False}))
        self.assertFalse(decide("schedule_cron", {"run_in_background": True}))
        with self.assertRaises(ValueError):
            decide("bash", {"command": "pwd", "run_in_background": "false"})

    def test_background_completion_notification_follows_tool_group(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)

        def bash(**kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("release timed out")
            return "background output"

        def read(path):
            self.assertTrue(started.wait(timeout=2))
            self.assertFalse(release.is_set())
            release.set()
            self.join_workers()
            return "config"

        self.agent.TOOL_HANDLERS.update(bash=bash, read_file=read)
        self.events = [reply(None, [
            tool_call("bg", "bash", '{"command":"build","run_in_background":true}'),
            tool_call("read", "read_file", '{"path":"config"}'),
        ]), reply()]
        history = self.run_loop()
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "tool", "tool", "user", "assistant"])
        self.assertIn("background output", history[-2]["content"])
        self.assertIn("bg_0001 started", history[2]["content"])
        self.assert_tool_pairs(self.requests[1]["messages"])

    def test_background_failure_is_collected_once(self):
        self.agent.TOOL_HANDLERS["bash"] = Mock(side_effect=RuntimeError("failed <command>"))
        job_id = self.agent.start_background_task("call-bg", "bash", {"command": "sample"})
        self.join_workers()
        self.assertEqual(self.agent.background_tasks[job_id]["status"], "failed")
        notifications = self.agent.collect_background_results()
        self.assertIn("failed &lt;command&gt;", notifications[0])
        self.assertEqual(self.agent.collect_background_results(), [])

    def test_background_thread_start_failure_removes_running_record(self):
        with patch.object(self.agent.threading, "Thread", side_effect=RuntimeError("no thread")):
            with self.assertRaises(RuntimeError):
                self.agent.start_background_task("call-bg", "bash", {"command": "sample"})
        self.assertEqual(self.agent.background_tasks, {})

    def test_nonzero_exit_and_timeout_are_failures(self):
        with patch.object(self.agent.subprocess, "run", return_value=subprocess.CompletedProcess("sample", 3, "", "bad")):
            with self.assertRaisesRegex(RuntimeError, "Exit code 3"):
                self.agent.run_bash("sample")
        with patch.object(self.agent.subprocess, "run", side_effect=subprocess.TimeoutExpired("sample", 120)):
            with self.assertRaisesRegex(RuntimeError, "Timeout"):
                self.agent.run_bash("sample")

    def test_scheduler_loop_checks_after_sleep(self):
        with patch.object(self.agent.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                patch.object(self.agent, "check_scheduled_jobs") as check:
            with self.assertRaises(KeyboardInterrupt):
                self.agent.cron_scheduler_loop()
        check.assert_called_once_with()

    def test_processor_loop_survives_one_local_failure(self):
        with patch.object(self.agent.time, "sleep", side_effect=[None, None, KeyboardInterrupt]), \
                patch.object(self.agent, "process_queued_jobs", side_effect=[RuntimeError("bad"), False]) as process:
            with self.assertRaises(KeyboardInterrupt):
                self.agent.queue_processor_loop()
        self.assertEqual(process.call_count, 2)
        self.assertIn("queue processor error", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()

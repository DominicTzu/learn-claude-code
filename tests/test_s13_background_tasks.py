import importlib.util
import io
import itertools
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI


MODULE_PATH = Path(__file__).resolve().parents[1] / "s13_background_tasks" / "code.py"
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


class BackgroundTaskTests(unittest.TestCase):
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

        # Import with test credentials and a temporary workspace, never the real .env.
        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key", "LLM_MODEL_ID": "test-model",
            "LLM_BASE_URL": "https://example.invalid/v1",
        }), patch("dotenv.load_dotenv"), patch("pathlib.Path.cwd", return_value=self.workdir), \
                patch("openai.OpenAI", side_effect=make_client):
            spec = importlib.util.spec_from_file_location("s13_under_test", MODULE_PATH)
            self.agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.agent)
        self.addCleanup(self.agent.client.close)
        contexts.enter_context(patch.object(self.agent.threading, "Thread", side_effect=self.make_worker))
        contexts.enter_context(patch.object(self.agent.random, "randint", side_effect=itertools.count(1)))
        # Finish all workers before restoring mocks or removing their temporary files.
        self.addCleanup(self.finish_workers)

    def make_worker(self, *args, **kwargs):
        worker = REAL_THREAD(*args, **kwargs)
        self.workers.append(worker)
        return worker

    def join_workers(self):
        for worker in list(self.workers):
            if worker.ident is not None:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), "background worker did not stop")

    def finish_workers(self):
        for event in self.release_events:
            event.set()
        self.join_workers()

    def blocking_bash(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)

        def run(**kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test worker release timed out")
            return "background output"

        self.agent.TOOL_HANDLERS["bash"] = run
        return started, release

    def complete(self, request):
        self.requests.append(json.loads(request.content))
        if not self.events:
            raise AssertionError("unexpected extra API request")
        event = self.events.pop(0)
        return event() if callable(event) else event

    def run_loop(self, history=None):
        if history is None:
            history = [{"role": "user", "content": "test request"}]
        self.agent.agent_loop(history, {})
        return history

    def start_job(self, command="echo sample"):
        return self.agent.start_background_task("call-bg", "bash", {
            "command": command, "run_in_background": True,
        })

    def assert_tool_pairs(self, messages):
        pending = []
        for message in messages:
            if message["role"] == "tool":
                self.assertIn(message["tool_call_id"], pending)
                pending.remove(message["tool_call_id"])
            else:
                self.assertEqual(pending, [], "notification split a tool-call group")
                pending = [call["id"] for call in message.get("tool_calls", [])]
        self.assertEqual(pending, [])

    def test_direct_answer_and_eight_function_schemas(self):
        self.events = [reply()]
        history = self.run_loop()
        request = self.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["max_tokens"], 8000)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(len(request["tools"]), 8)
        bash = next(t["function"] for t in request["tools"] if t["function"]["name"] == "bash")
        self.assertEqual(bash["parameters"]["properties"]["run_in_background"]["type"], "boolean")
        self.assertEqual(history[-1]["content"], "done")
        self.assertIn("done", self.output.getvalue())

    def test_explicit_boolean_overrides_heuristic(self):
        decide = self.agent.should_run_background
        self.assertTrue(decide("bash", {"command": "pytest"}))
        self.assertFalse(decide("bash", {"command": "pytest", "run_in_background": False}))
        self.assertTrue(decide("bash", {"command": "pwd", "run_in_background": True}))
        self.assertFalse(decide("bash", {"command": "pwd"}))
        self.assertFalse(decide("write_file", {"run_in_background": True}))
        for arguments in ({}, {"command": []}, {"command": "pwd", "run_in_background": "false"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                decide("bash", arguments)

    def test_explicit_false_executes_synchronously(self):
        bash = Mock(return_value="tests passed")
        self.agent.TOOL_HANDLERS["bash"] = bash
        self.events = [reply(None, [tool_call("sync", "bash", json.dumps({
            "command": "pytest", "run_in_background": False,
        }))]), reply()]
        history = self.run_loop()
        bash.assert_called_once_with(command="pytest", run_in_background=False)
        self.assertEqual(self.workers, [])
        self.assertEqual(history[2]["content"], "tests passed")
        self.assert_tool_pairs(history)

    def test_dispatch_returns_before_completion(self):
        started, release = self.blocking_bash()
        bg_id = self.start_job()
        self.assertTrue(started.wait(timeout=2))
        self.assertEqual(self.agent.background_tasks[bg_id]["status"], "running")
        self.assertEqual(self.agent.collect_background_results(), [])
        self.assertTrue(self.workers[0].daemon)
        release.set()
        self.join_workers()
        self.assertEqual(self.agent.background_tasks[bg_id]["status"], "completed")
        self.assertEqual(self.agent.background_results[bg_id], "background output")

    def test_named_worker_receives_registered_id_and_copied_arguments(self):
        bash = Mock(return_value="tests passed")
        self.agent.TOOL_HANDLERS["bash"] = bash
        original_input = {"command": "pytest", "run_in_background": True}
        thread = Mock()

        # Delay execution so the handoff between start() and the worker is inspectable.
        with patch.object(self.agent.threading, "Thread", return_value=thread) as make_thread:
            bg_id = self.agent.start_background_task("call-A", "bash", original_input)

        thread.start.assert_called_once_with()
        bash.assert_not_called()
        self.assertEqual(self.agent.background_tasks[bg_id], {
            "tool_call_id": "call-A", "command": "pytest", "status": "running",
        })
        thread_options = make_thread.call_args.kwargs
        self.assertIs(thread_options["target"], self.agent.run_background_worker)
        self.assertTrue(thread_options["daemon"])
        self.assertEqual(thread_options["args"][:2], (bg_id, "bash"))
        self.assertIsNot(thread_options["args"][2], original_input)

        original_input["command"] = "changed after dispatch"
        thread_options["target"](*thread_options["args"])
        bash.assert_called_once_with(command="pytest", run_in_background=True)
        self.assertEqual(self.agent.background_tasks[bg_id]["status"], "completed")
        self.assertEqual(self.agent.background_results[bg_id], "tests passed")

    def test_foreground_work_continues_and_completion_during_request_is_injected(self):
        started, release = self.blocking_bash()

        def read_config(path):
            self.assertTrue(started.wait(timeout=2))
            self.assertFalse(release.is_set())
            return "configuration"

        self.agent.TOOL_HANDLERS["read_file"] = read_config

        def finish_during_model_request():
            release.set()
            self.join_workers()
            return reply("Checking background results.")

        self.events = [reply(None, [
            tool_call("bg", "bash", '{"command":"build","run_in_background":true}'),
            tool_call("read", "read_file", '{"path":"config.txt"}'),
        ]), finish_during_model_request, reply("all done")]
        history = self.run_loop()
        self.assertEqual(len(self.requests), 3)
        results = [m for m in history if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["bg", "read"])
        self.assertIn("Background task bg_0001 started", results[0]["content"])
        self.assertEqual(results[1]["content"], "configuration")
        notifications = [m for m in history if m["role"] == "user" and "<task_notification>" in m["content"]]
        self.assertEqual(len(notifications), 1)
        self.assertIn("background output", notifications[0]["content"])
        self.assertEqual(history[-1]["content"], "all done")
        for request in self.requests:
            self.assert_tool_pairs(request["messages"])
        self.assertEqual(self.agent.background_tasks, {})

    def test_fast_completion_notification_follows_all_tool_results(self):
        self.agent.TOOL_HANDLERS["bash"] = lambda **kwargs: "quick output"

        def read_after_worker(path):
            self.join_workers()
            return "file content"

        self.agent.TOOL_HANDLERS["read_file"] = read_after_worker
        self.events = [reply(None, [
            tool_call("bg", "bash", '{"command":"pwd","run_in_background":true}'),
            tool_call("read", "read_file", '{"path":"file.txt"}'),
        ]), reply()]
        history = self.run_loop()
        self.assertEqual([m["role"] for m in history],
                         ["user", "assistant", "tool", "tool", "user", "assistant"])
        self.assert_tool_pairs(history)
        self.assertIn("quick output", self.requests[1]["messages"][-1]["content"])

    def test_late_completion_is_delivered_on_next_user_turn_without_polling(self):
        started, release = self.blocking_bash()
        self.events = [reply(None, [tool_call(
            "bg", "bash", '{"command":"pwd","run_in_background":true}',
        )]), reply("Background command started.")]
        history = self.run_loop()
        self.assertTrue(started.wait(timeout=2))
        self.assertEqual(len(self.requests), 2)
        release.set()
        self.join_workers()
        self.assertEqual(len(self.requests), 2)
        history.append({"role": "user", "content": "What happened?"})
        self.events = [reply("Here is the result.")]
        self.run_loop(history)
        self.assertIn("background output", self.requests[-1]["messages"][-1]["content"])
        self.assertEqual(self.agent.background_results, {})
        self.assert_tool_pairs(history)

    def test_background_success_failure_and_timeout_use_actual_outcome(self):
        cases = [
            (subprocess.CompletedProcess("cmd", 0, "Error: just printed text", ""), "completed"),
            (subprocess.CompletedProcess("cmd", 7, "", "bad command"), "failed"),
            (subprocess.TimeoutExpired("cmd", 120), "failed"),
        ]
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(self.agent.subprocess, "run", **options):
                    bg_id = self.start_job()
                    self.join_workers()
                self.assertEqual(self.agent.background_tasks[bg_id]["status"], expected)
                notification = ET.fromstring(self.agent.collect_background_results()[0])
                self.assertEqual(notification.findtext("status"), expected)
                if expected == "failed":
                    self.assertIn("Error:", notification.findtext("summary"))

    def test_unexpected_worker_exception_is_reported_as_failed(self):
        self.agent.TOOL_HANDLERS["bash"] = Mock(side_effect=RuntimeError("worker failed"))
        self.start_job()
        self.join_workers()
        notification = ET.fromstring(self.agent.collect_background_results()[0])
        self.assertEqual(notification.findtext("status"), "failed")
        self.assertIn("worker failed", notification.findtext("summary"))

    def test_thread_start_failure_cleans_running_record_and_returns_tool_error(self):
        cannot_start = Mock()
        cannot_start.start.side_effect = RuntimeError("cannot start thread")
        self.events = [reply(None, [tool_call(
            "bg", "bash", '{"command":"pwd","run_in_background":true}',
        )]), reply()]
        with patch.object(self.agent.threading, "Thread", return_value=cannot_start):
            history = self.run_loop()
        self.assertEqual(self.agent.background_tasks, {})
        self.assertIn("cannot start thread", history[2]["content"])
        self.assert_tool_pairs(history)

    def test_collection_is_once_only_and_escapes_notification_fields(self):
        output = "<payload>&data" * 30
        command = "echo '<tag>&'"
        self.agent.TOOL_HANDLERS["bash"] = lambda **kwargs: output
        self.start_job(command)
        self.join_workers()
        notifications = self.agent.collect_background_results()
        self.assertEqual(len(notifications), 1)
        parsed = ET.fromstring(notifications[0])
        self.assertEqual(parsed.findtext("command"), command)
        self.assertEqual(parsed.findtext("summary"), output[:200] + "... (truncated)")
        self.assertEqual(self.agent.collect_background_results(), [])
        self.assertEqual(self.agent.background_tasks, {})
        self.assertEqual(self.agent.background_results, {})

    def test_collection_removes_finished_tasks_but_keeps_running_tasks(self):
        # Seed all three states to check filtering and removal independently of timing.
        self.agent.background_tasks.update({
            "bg_0001": {"command": "first", "status": "completed"},
            "bg_0002": {"command": "second", "status": "running"},
            "bg_0003": {"command": "third", "status": "failed"},
        })
        self.agent.background_results.update({
            "bg_0001": "first result", "bg_0003": "third failed",
        })

        notifications = self.agent.collect_background_results()
        parsed = [ET.fromstring(notification) for notification in notifications]
        self.assertEqual([item.findtext("task_id") for item in parsed], ["bg_0001", "bg_0003"])
        self.assertEqual([item.findtext("status") for item in parsed], ["completed", "failed"])
        self.assertEqual([item.findtext("summary") for item in parsed], ["first result", "third failed"])
        self.assertEqual(self.agent.background_tasks, {
            "bg_0002": {"command": "second", "status": "running"},
        })
        self.assertEqual(self.agent.background_results, {})
        self.assertEqual(self.agent.collect_background_results(), [])

    def test_two_collectors_cannot_take_the_same_result(self):
        self.agent.TOOL_HANDLERS["bash"] = lambda **kwargs: "done"
        self.start_job()
        self.join_workers()
        barrier = threading.Barrier(3)
        collected = []
        errors = []

        def collect():
            try:
                barrier.wait(timeout=2)
                collected.extend(self.agent.collect_background_results())
            except Exception as error:
                errors.append(error)

        collectors = [REAL_THREAD(target=collect) for _ in range(2)]
        for collector in collectors:
            collector.start()
        barrier.wait(timeout=2)
        for collector in collectors:
            collector.join(timeout=3)
            self.assertFalse(collector.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(collected), 1)

    def test_bad_arguments_and_non_bash_background_requests_do_not_start_threads(self):
        self.events = [reply(None, [
            tool_call("json", "bash", "{"),
            tool_call("object", "bash", "[]"),
            tool_call("missing", "bash", "{}"),
            tool_call("boolean", "bash", '{"command":"pwd","run_in_background":"true"}'),
            tool_call("nonbash", "create_task", '{"subject":"task","run_in_background":true}'),
            tool_call("unknown", "unknown", "{}"),
        ]), reply()]
        history = self.run_loop()
        for message in history[2:7]:
            self.assertTrue(message["content"].startswith("Error:"))
        self.assertEqual(history[7]["content"], "Unknown tool: unknown")
        self.assertEqual(self.workers, [])
        self.assert_tool_pairs(history)

    def test_task_records_remain_distinct_from_background_lifecycle(self):
        root = self.agent.create_task("schema", "description")
        dependent = self.agent.create_task("api", blockedBy=[root.id])
        self.agent.claim_task(root.id)
        self.agent.TOOL_HANDLERS["bash"] = lambda **kwargs: "done"
        bg_id = self.start_job()
        self.join_workers()
        self.assertEqual(self.agent.load_task(root.id).status, "in_progress")
        self.assertFalse(self.agent.can_start(dependent.id))
        self.assertNotIn(bg_id, self.agent.run_list_tasks())
        self.agent.complete_task(root.id)
        self.assertTrue(self.agent.can_start(dependent.id))
        self.assertEqual(json.loads(self.agent.get_task(root.id))["description"], "description")
        with self.assertRaises(ValueError):
            self.agent._task_path("../outside")

    def test_prompt_refresh_after_file_tool(self):
        self.events = [reply(None, [tool_call("write", "write_file", json.dumps({
            "path": ".memory/MEMORY.md", "content": "new index",
        }))]), reply()]
        self.run_loop()
        self.assertNotIn("new index", self.requests[0]["messages"][0]["content"])
        self.assertIn("new index", self.requests[1]["messages"][0]["content"])

    def test_truncated_response_never_starts_background_command(self):
        self.events = [reply("partial", [tool_call(
            "truncated", "bash", '{"command":"pwd","run_in_background":true}',
        )], finish_reason="length")]
        history = self.run_loop()
        self.assertEqual(self.workers, [])
        self.assertIn("Output truncated", history[-1]["content"])
        self.assert_tool_pairs(history)

    def test_api_failure_is_displayed_as_text(self):
        self.events = [httpx.Response(401, json={"error": {
            "message": "invalid key", "type": "authentication_error",
        }})]
        history = self.run_loop()
        self.assertIn("[Error] AuthenticationError", history[-1]["content"])
        self.assertIn("[Error] AuthenticationError", self.output.getvalue())

    def test_real_short_shell_command_finishes_in_worker(self):
        command = f"{shlex.quote(sys.executable)} -c 'print(\"background smoke\")'"
        bg_id = self.start_job(command)
        self.join_workers()
        self.assertEqual(self.agent.background_tasks[bg_id]["status"], "completed")
        self.assertEqual(self.agent.background_results[bg_id], "background smoke")


if __name__ == "__main__":
    unittest.main()

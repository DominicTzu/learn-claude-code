import importlib.util
import io
import itertools
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI


MODULE_PATH = Path(__file__).resolve().parents[1] / "s12_task_system" / "code.py"


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


class TaskSystemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name).resolve()
        self.requests = []
        self.events = []
        self.output = io.StringIO()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(redirect_stdout(self.output))
        self.agent = self.load_agent()
        # Avoid rare random ID collisions in tests; production ID behavior is unchanged.
        contexts.enter_context(patch.object(self.agent.random, "randint",
                                           side_effect=itertools.count(1)))

    def load_agent(self):
        def make_client(**kwargs):
            return OpenAI(
                **kwargs, max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(self.complete)),
            )

        # No real .env reads, network requests, or writes to the project's .tasks directory.
        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key", "LLM_MODEL_ID": "test-model",
            "LLM_BASE_URL": "https://example.invalid/v1",
        }), patch("dotenv.load_dotenv"), patch("pathlib.Path.cwd", return_value=self.workdir), \
                patch("openai.OpenAI", side_effect=make_client):
            spec = importlib.util.spec_from_file_location("s12_under_test", MODULE_PATH)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)
        self.addCleanup(agent.client.close)
        return agent

    def complete(self, request):
        self.requests.append(json.loads(request.content))
        if not self.events:
            raise AssertionError("unexpected extra API request")
        event = self.events.pop(0)
        return event() if callable(event) else event

    def run_loop(self, context=None):
        history = [{"role": "user", "content": "test request"}]
        self.agent.agent_loop(history, context or {})
        return history

    def assert_tool_pairs(self, messages):
        pending = []
        for message in messages:
            if message["role"] == "tool":
                self.assertIn(message["tool_call_id"], pending)
                pending.remove(message["tool_call_id"])
            else:
                self.assertEqual(pending, [])
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
        self.assertEqual({t["function"]["name"] for t in request["tools"]},
                         set(self.agent.TOOL_HANDLERS))
        self.assertTrue(all(t["type"] == "function" for t in request["tools"]))
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(history[-1]["content"], "done")
        self.assertIn("done", self.output.getvalue())

    def test_task_persists_across_module_reload(self):
        title = "\u4efb\u52a1"
        task = self.agent.create_task(title, "full description")
        self.agent.claim_task(task.id, "worker")
        raw = self.agent._task_path(task.id).read_text(encoding="utf-8")
        self.assertIn(title, raw)
        stored = json.loads(raw)
        self.assertEqual(stored["status"], "in_progress")
        reloaded = self.load_agent()
        self.assertEqual(reloaded.get_task(task.id), self.agent.get_task(task.id))
        self.assertEqual(reloaded.list_tasks()[0].owner, "worker")

    def test_all_dependencies_must_be_completed_before_claim(self):
        first = self.agent.create_task("schema")
        second = self.agent.create_task("configuration")
        dependent = self.agent.create_task("api", blockedBy=[first.id, second.id])
        self.assertFalse(self.agent.can_start(dependent.id))
        self.assertIn("Blocked by:", self.agent.claim_task(dependent.id))
        self.agent.claim_task(first.id)
        self.agent.complete_task(first.id)
        self.assertFalse(self.agent.can_start(dependent.id))
        self.agent.claim_task(second.id)
        self.assertIn("Unblocked: api", self.agent.complete_task(second.id))
        self.assertTrue(self.agent.can_start(dependent.id))
        self.assertEqual(self.agent.load_task(dependent.id).status, "pending")
        self.assertIn("Claimed", self.agent.claim_task(dependent.id))

    def test_missing_dependency_blocks_without_creating_a_file(self):
        task = self.agent.create_task("blocked", blockedBy=["task_missing"])
        self.assertFalse(self.agent.can_start(task.id))
        self.assertIn("task_missing", self.agent.claim_task(task.id))
        self.assertEqual(len(self.agent.list_tasks()), 1)

    def test_state_transitions_reject_skips_and_duplicate_claims(self):
        task = self.agent.create_task("work")
        self.assertIn("cannot complete", self.agent.complete_task(task.id))
        self.agent.claim_task(task.id, "first")
        self.assertIn("cannot claim", self.agent.claim_task(task.id, "second"))
        self.assertEqual(self.agent.load_task(task.id).owner, "first")
        self.agent.complete_task(task.id)
        self.assertIn("cannot complete", self.agent.complete_task(task.id))
        self.assertIn("cannot claim", self.agent.claim_task(task.id))

    def test_invalid_create_arguments_do_not_persist_tasks(self):
        for kwargs in ({"subject": 123}, {"description": []}, {"blockedBy": "task_id"},
                       {"blockedBy": [123]}, {"blockedBy": ["../outside"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.agent.create_task(**{"subject": "work", **kwargs})
        self.assertEqual(self.agent.list_tasks(), [])

    def test_task_paths_reject_traversal(self):
        for task_id in ("../outside", "/tmp/outside", "nested/task", "..\\outside", "", None):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.agent._task_path(task_id)

    def test_task_paths_and_listing_reject_external_symlinks(self):
        outside = self.workdir / "outside.json"
        outside.write_text("{}")
        (self.agent.TASKS_DIR / "task_link.json").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.agent.load_task("task_link")
        with self.assertRaises(ValueError):
            self.agent.list_tasks()

    def test_get_includes_description_without_automatic_prompt_injection(self):
        task = self.agent.create_task("short title", "details only in get_task")
        self.assertIn(task.id, self.agent.run_list_tasks())
        self.assertNotIn(task.description, self.agent.run_list_tasks())
        self.assertEqual(json.loads(self.agent.run_get_task(task.id))["description"], task.description)
        prompt = self.agent.get_system_prompt(self.agent.update_context({}, []))
        self.assertNotIn(task.subject, prompt)
        self.assertNotIn(task.description, prompt)

    def test_all_task_tools_in_one_agent_workflow(self):
        def create_dependent_and_finish_root():
            root = self.agent.list_tasks()[0]
            return reply("Working on schema.", calls=[
                tool_call("create-api", "create_task", json.dumps({
                    "subject": "api", "blockedBy": [root.id],
                })),
                tool_call("claim-schema", "claim_task", json.dumps({"task_id": root.id})),
                tool_call("finish-schema", "complete_task", json.dumps({"task_id": root.id})),
            ])

        def inspect_and_finish_dependent():
            dependent = next(t for t in self.agent.list_tasks() if t.subject == "api")
            arguments = json.dumps({"task_id": dependent.id})
            return reply(None, calls=[
                tool_call("get-api", "get_task", arguments),
                tool_call("list-all", "list_tasks", "{}"),
                tool_call("claim-api", "claim_task", arguments),
                tool_call("finish-api", "complete_task", arguments),
            ])

        self.events = [reply(None, calls=[
            tool_call("create-schema", "create_task", '{"subject":"schema"}'),
        ]), create_dependent_and_finish_root, inspect_and_finish_dependent, reply()]
        history = self.run_loop()
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(len(self.agent.list_tasks()), 2)
        self.assertTrue(all(t.status == "completed" for t in self.agent.list_tasks()))
        self.assert_tool_pairs(history)
        for request in self.requests:
            self.assert_tool_pairs(request["messages"])
        self.assertIn("Working on schema.", self.output.getvalue())
        self.assertIn("done", self.output.getvalue())

    def test_bad_arguments_and_missing_tasks_return_matching_tool_results(self):
        self.events = [reply(None, calls=[
            tool_call("bad-json", "create_task", "{"),
            tool_call("bad-type", "create_task", "[]"),
            tool_call("missing-argument", "create_task", "{}"),
            tool_call("missing-claim", "claim_task", '{"task_id":"task_missing"}'),
            tool_call("missing-complete", "complete_task", '{"task_id":"task_missing"}'),
            tool_call("missing-get", "get_task", '{"task_id":"task_missing"}'),
            tool_call("unknown", "unknown", "{}"),
        ]), reply()]
        history = self.run_loop()
        self.assert_tool_pairs(history)
        for message in history[2:8]:
            self.assertTrue(message["content"].startswith("Error:"))
        self.assertEqual(history[8]["content"], "Unknown: unknown")
        self.assertEqual(self.agent.list_tasks(), [])

    def test_file_tools_refresh_memory_prompt_and_preserve_results(self):
        self.events = [reply(None, calls=[
            tool_call("write-index", "write_file", json.dumps({
                "path": ".memory/MEMORY.md", "content": "updated index",
            })),
            tool_call("read-index", "read_file", '{"path":".memory/MEMORY.md"}'),
        ]), reply()]
        history = self.run_loop()
        self.assertNotIn("updated index", self.requests[0]["messages"][0]["content"])
        self.assertIn("updated index", self.requests[1]["messages"][0]["content"])
        self.assertEqual(history[3]["content"], "updated index")
        self.assert_tool_pairs(history)

    def test_prompt_cache_uses_runtime_values_and_invalidates(self):
        context = self.agent.update_context({}, [])
        with patch.object(self.agent, "assemble_system_prompt",
                          wraps=self.agent.assemble_system_prompt) as assemble:
            first = self.agent.get_system_prompt(context)
            self.assertEqual(first, self.agent.get_system_prompt(dict(reversed(list(context.items())))))
            self.assertEqual(assemble.call_count, 1)
            changed = self.agent.get_system_prompt({
                **context, "enabled_tools": ["get_task"], "workspace": "/example",
            })
            self.assertIn("Available tools: get_task.", changed)
            self.assertIn("Working directory: /example", changed)
            self.assertEqual(assemble.call_count, 2)

    def test_truncated_tool_response_does_not_modify_tasks(self):
        self.events = [reply("partial", calls=[
            tool_call("truncated", "create_task", '{"subject":"work"}'),
        ], finish_reason="length")]
        history = self.run_loop()
        self.assertEqual(self.agent.list_tasks(), [])
        self.assert_tool_pairs(history)
        self.assertIn("Output truncated", history[-1]["content"])
        self.assertIn("partial", self.output.getvalue())

    def test_api_error_becomes_visible_assistant_text(self):
        self.events = [httpx.Response(401, json={"error": {
            "message": "invalid key", "type": "authentication_error",
        }})]
        history = self.run_loop()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("[Error] AuthenticationError", history[-1]["content"])
        self.assertIn("[Error] AuthenticationError", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI


MODULE_PATH = Path(__file__).resolve().parents[1] / "s11_error_recovery" / "code.py"


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


def api_error(status, code="test_error", headers=None):
    return httpx.Response(status, headers=headers, json={
        "error": {"message": code, "type": "api_error", "code": code},
    })


class ErrorRecoveryTests(unittest.TestCase):
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

        # Use real SDK request/response processing without secrets or network access.
        def make_client(**kwargs):
            return OpenAI(
                **kwargs,
                http_client=httpx.Client(transport=httpx.MockTransport(self.complete)),
            )

        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key", "LLM_MODEL_ID": "test-model",
            "LLM_BASE_URL": "https://example.invalid/v1",
            "LLM_FALLBACK_MODEL_ID": "", "FALLBACK_MODEL_ID": "",
            "LLM_MAX_TOKENS": "8000", "LLM_ESCALATED_MAX_TOKENS": "8000",
        }), patch("dotenv.load_dotenv"), patch("pathlib.Path.cwd", return_value=self.workdir), \
                patch("openai.OpenAI", side_effect=make_client):
            spec = importlib.util.spec_from_file_location("s11_under_test", MODULE_PATH)
            self.agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.agent)
        self.addCleanup(self.agent.client.close)
        self.sleep = contexts.enter_context(patch.object(self.agent.time, "sleep"))
        contexts.enter_context(patch.object(self.agent.random, "uniform", return_value=0))

    def complete(self, request):
        self.requests.append(json.loads(request.content))
        if not self.events:
            raise AssertionError("unexpected extra API request")
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    def run_loop(self, history=None):
        if history is None:
            history = [{"role": "user", "content": "test request"}]
        self.agent.agent_loop(history, {})
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

    def test_direct_answer_uses_shared_configuration(self):
        self.events = [reply()]
        history = self.run_loop()
        request = self.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["max_tokens"], 8000)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(self.agent.client.max_retries, 0)
        self.assertEqual(history[-1]["content"], "done")
        self.assertIn("done", self.output.getvalue())
        self.assertEqual({tool["function"]["name"] for tool in request["tools"]},
                         set(self.agent.TOOL_HANDLERS))

    def test_request_retry_does_not_reexecute_tools_and_refreshes_prompt(self):
        write = Mock(wraps=self.agent.run_write)
        self.agent.TOOL_HANDLERS["write_file"] = write
        self.events = [reply(None, calls=[
            tool_call("write", "write_file", json.dumps({
                "path": ".memory/MEMORY.md", "content": "new index entry",
            })),
            tool_call("read", "read_file", '{"path":".memory/MEMORY.md"}'),
        ]), api_error(500), reply()]
        history = self.run_loop()
        write.assert_called_once()
        self.assert_tool_pairs(history)
        self.assertEqual(history[3]["content"], "new index entry")
        self.assertIn("new index entry", self.requests[1]["messages"][0]["content"])
        self.assertEqual(self.requests[1], self.requests[2])

    def test_invalid_arguments_and_unknown_tools_have_paired_results(self):
        self.events = [reply(None, calls=[
            tool_call("bad-json", "bash", "{"),
            tool_call("bad-type", "bash", "[]"),
            tool_call("missing-argument", "bash", "{}"),
            tool_call("unknown", "missing", "{}"),
        ]), reply()]
        with patch.object(self.agent.subprocess, "run") as run:
            history = self.run_loop()
        run.assert_not_called()
        self.assert_tool_pairs(history)
        for result in history[2:5]:
            self.assertTrue(result["content"].startswith("Error: invalid tool arguments:"))
        self.assertEqual(history[5]["content"], "Unknown: missing")

    def test_rate_limit_uses_retry_after(self):
        self.events = [api_error(429, headers={"Retry-After": "2.5"}), reply()]
        self.run_loop()
        self.sleep.assert_called_once_with(2.5)
        self.assertEqual(self.requests[0], self.requests[1])

    def test_retry_delay_and_header_parsing(self):
        self.assertEqual([self.agent.retry_delay(i) for i in (0, 1, 2, 10)],
                         [0.5, 1, 2, 32])
        self.assertEqual(self.agent.retry_delay(0, 0), 0)
        for header, expected in (("0", 0), ("3", 3), ("invalid", None), ("nan", None)):
            with self.subTest(header=header):
                error = SimpleNamespace(response=api_error(429, headers={"Retry-After": header}))
                self.assertEqual(self.agent.get_retry_after(error), expected)
        error = SimpleNamespace(response=api_error(429, headers={
            "Retry-After": "Thu, 01 Jan 1970 00:16:50 GMT",
        }))
        with patch.object(self.agent.time, "time", return_value=1000):
            self.assertEqual(self.agent.get_retry_after(error), 10)

    def test_consecutive_overload_switches_actual_request_model(self):
        for status in (503, 529):
            with self.subTest(status=status), patch.object(self.agent, "FALLBACK_MODEL", "backup"):
                self.requests.clear()
                self.events = [api_error(status) for _ in range(3)] + [reply()]
                self.run_loop()
                self.assertEqual([r["model"] for r in self.requests],
                                 ["test-model", "test-model", "test-model", "backup"])
                self.events = [reply()]
                self.run_loop()
                self.assertEqual(self.requests[-1]["model"], "test-model")

    def test_other_failure_breaks_consecutive_overload_count(self):
        self.events = [api_error(s) for s in (503, 429, 503, 503)] + [reply()]
        with patch.object(self.agent, "FALLBACK_MODEL", "backup"):
            self.run_loop()
        self.assertTrue(all(r["model"] == "test-model" for r in self.requests))

    def test_connection_and_timeout_errors_retry(self):
        self.events = [httpx.ConnectError("offline"), httpx.ReadTimeout("timed out"), reply()]
        history = self.run_loop()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(history[-1]["content"], "done")

    def test_permanent_errors_do_not_retry(self):
        for status, code in ((401, "invalid_api_key"), (403, "permission_denied"),
                             (400, "unsupported_parameter"), (429, "insufficient_quota")):
            with self.subTest(status=status, code=code):
                self.requests.clear()
                self.events = [api_error(status, code)]
                history = self.run_loop()
                self.assertEqual(len(self.requests), 1)
                self.assertTrue(history[-1]["content"].startswith("[Error]"))
        self.sleep.assert_not_called()

    def test_retry_exhaustion_does_not_sleep_after_final_failure(self):
        self.events = [api_error(500) for _ in range(3)]
        with patch.object(self.agent, "MAX_RETRIES", 2):
            history = self.run_loop()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn("Max retries (2) exceeded", history[-1]["content"])

    def test_context_compaction_preserves_multi_tool_group(self):
        calls = [tool_call(str(i), "read_file", '{"path":"test.txt"}') for i in range(6)]
        history = [
            {"role": "user", "content": "old request"},
            {"role": "assistant", "tool_calls": calls},
            *[{"role": "tool", "tool_call_id": str(i), "content": "result"} for i in range(6)],
        ]
        self.events = [api_error(400, "context_length_exceeded"), reply()]
        self.run_loop(history)
        self.assert_tool_pairs(self.requests[1]["messages"])
        self.assertIn("[Reactive compact]", history[0]["content"])
        self.assertEqual(len(self.requests[1]["messages"][2:]), 7)
        self.assertNotIn("old request", json.dumps(self.requests[1]))
        self.assertIn("done", self.output.getvalue())
        self.sleep.assert_not_called()

    def test_context_compaction_is_attempted_only_once(self):
        self.events = [api_error(400, "context_length_exceeded") for _ in range(2)]
        with patch.object(self.agent, "reactive_compact",
                          wraps=self.agent.reactive_compact) as compact:
            history = self.run_loop()
        compact.assert_called_once()
        self.assertEqual(len(self.requests), 2)
        self.assertIn("Context too large", history[-1]["content"])

    def test_token_escalation_retries_unchanged_history(self):
        self.events = [reply("discard this", finish_reason="length"), reply("complete answer")]
        with patch.object(self.agent, "ESCALATED_MAX_TOKENS", 16000):
            history = self.run_loop()
        self.assertEqual([r["max_tokens"] for r in self.requests], [8000, 16000])
        self.assertEqual(self.requests[0]["messages"], self.requests[1]["messages"])
        self.assertNotIn("discard this", json.dumps(history))
        self.assertNotIn("discard this", self.output.getvalue())

    def test_text_continuation_preserves_and_displays_all_parts(self):
        self.events = [reply("part one", finish_reason="length"),
                       reply("part two", finish_reason="length"), reply("part three")]
        history = self.run_loop()
        self.assertTrue(all(r["max_tokens"] == 8000 for r in self.requests))
        self.assertEqual([m["content"] for m in history if m["role"] == "assistant"],
                         ["part one", "part two", "part three"])
        for text in ("part one", "part two", "part three"):
            self.assertIn(text, self.output.getvalue())

    def test_truncated_tool_calls_are_not_executed_or_sent_back(self):
        read = Mock(return_value="file content")
        self.agent.TOOL_HANDLERS["read_file"] = read
        self.events = [
            reply(None, [tool_call("truncated", "read_file", '{"path":')], "length"),
            reply(None, [tool_call("complete", "read_file", '{"path":"ok.txt"}')]),
            reply(),
        ]
        history = self.run_loop()
        read.assert_called_once_with(path="ok.txt")
        self.assert_tool_pairs(history)
        self.assertFalse(any(call["id"] == "truncated" for m in history
                             for call in m.get("tool_calls", [])))
        for request in self.requests:
            self.assert_tool_pairs(request["messages"])

    def test_continuation_budget_is_bounded(self):
        self.events = [reply("partial", finish_reason="length") for _ in range(3)]
        with patch.object(self.agent, "MAX_RECOVERY_RETRIES", 2):
            history = self.run_loop()
        self.assertEqual(len(self.requests), 3)
        self.assertIn("recovery limit reached", history[-1]["content"])

    def test_truncated_tool_recovery_exhaustion_keeps_history_valid(self):
        self.events = [reply(None, [tool_call("bad", "bash", "{")], "length") for _ in range(2)]
        with patch.object(self.agent, "MAX_RECOVERY_RETRIES", 1), \
                patch.object(self.agent.subprocess, "run") as run:
            history = self.run_loop()
        run.assert_not_called()
        self.assert_tool_pairs(history)
        self.assertIn("recovery limit reached", history[-1]["content"])


if __name__ == "__main__":
    unittest.main()

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI


MODULE_PATH = Path(__file__).resolve().parents[1] / "s10_system_prompt" / "code.py"


def tool_call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class SystemPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name).resolve()
        self.requests = []
        self.responses = []
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(redirect_stdout(io.StringIO()))

        # Keep real SDK serialization, but prevent .env reads and network traffic.
        def make_client(**kwargs):
            return OpenAI(
                **kwargs,
                http_client=httpx.Client(transport=httpx.MockTransport(self.complete)),
            )

        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL_ID": "test-model",
            "LLM_BASE_URL": "https://example.invalid/v1",
        }), patch("dotenv.load_dotenv"), patch("pathlib.Path.cwd", return_value=self.workdir), \
                patch("openai.OpenAI", side_effect=make_client):
            spec = importlib.util.spec_from_file_location("s10_under_test", MODULE_PATH)
            self.agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.agent)
        self.addCleanup(self.agent.client.close)

    def complete(self, request):
        self.requests.append(json.loads(request.content))
        message = self.responses.pop(0)
        return httpx.Response(200, json={
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "message": {"role": "assistant", **message},
            }],
        })

    def run_loop(self, context=None):
        history = [{"role": "user", "content": "test request"}]
        self.agent.agent_loop(history, context or {})
        return history

    def test_direct_answer_and_openai_request_shape(self):
        self.responses = [{"content": "done"}]
        history = self.run_loop()
        request = self.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(history[-1]["content"], "done")
        self.assertEqual(
            {tool["function"]["name"] for tool in request["tools"]},
            set(self.agent.TOOL_HANDLERS),
        )
        for tool in request["tools"]:
            self.assertEqual(tool["type"], "function")
            self.assertIn("parameters", tool["function"])

    def test_multiple_tool_results_match_calls(self):
        self.responses = [{"tool_calls": [
            tool_call("write-1", "write_file", json.dumps({
                "path": "sample.txt", "content": "hello",
            })),
            tool_call("read-1", "read_file", '{"path":"sample.txt"}'),
        ]}, {"content": "done"}]
        history = self.run_loop()
        self.assertEqual(
            [m["role"] for m in history],
            ["user", "assistant", "tool", "tool", "assistant"],
        )
        self.assertEqual([m["tool_call_id"] for m in history[2:4]], ["write-1", "read-1"])
        self.assertEqual(history[3]["content"], "hello")
        self.assertEqual(self.requests[1]["messages"][1:], history[:-1])
        self.assertEqual(self.requests[0]["messages"][0], self.requests[1]["messages"][0])

    def test_invalid_arguments_and_unknown_tool_return_results(self):
        self.responses = [{"tool_calls": [
            tool_call("invalid-json", "bash", "{"),
            tool_call("not-an-object", "bash", "[]"),
            tool_call("missing-command", "bash", "{}"),
            tool_call("unknown-tool", "missing", "{}"),
        ]}, {"content": "done"}]
        with patch.object(self.agent.subprocess, "run") as run:
            history = self.run_loop()
        run.assert_not_called()
        for message in history[2:5]:
            self.assertTrue(message["content"].startswith("Error: invalid tool arguments:"))
        self.assertEqual(history[5]["content"], "Unknown: missing")
        self.assertEqual(len(self.requests), 2)

    def test_cache_ignores_dict_key_order_and_invalidates_on_change(self):
        context = self.agent.update_context({}, [])
        reordered = dict(reversed(list(context.items())))
        with patch.object(self.agent, "assemble_system_prompt",
                          wraps=self.agent.assemble_system_prompt) as assemble:
            first = self.agent.get_system_prompt(context)
            self.assertEqual(first, self.agent.get_system_prompt(reordered))
            self.assertEqual(assemble.call_count, 1)
            changed = self.agent.get_system_prompt({**context, "memories": "new memory"})
            self.assertIn("new memory", changed)
            self.assertEqual(assemble.call_count, 2)

    def test_assembly_uses_runtime_tools_and_workspace(self):
        prompt = self.agent.assemble_system_prompt({
            "enabled_tools": ["read_file"], "workspace": "/example", "memories": "",
        })
        self.assertIn("Available tools: read_file.", prompt)
        self.assertIn("Working directory: /example", prompt)
        self.assertNotIn("Relevant memories:", prompt)

    def test_memory_section_loads_index_only_and_handles_removal(self):
        self.assertEqual(self.agent.update_context({}, [])["memories"], "")
        self.agent.run_write(".memory/MEMORY.md", "- [sample](sample.md): summary")
        self.agent.run_write(".memory/sample.md", "body not automatically loaded")
        context = self.agent.update_context({}, [])
        prompt = self.agent.get_system_prompt(context)
        self.assertIn("summary", prompt)
        self.assertNotIn("body not automatically loaded", prompt)
        self.agent.run_write(".memory/MEMORY.md", "  \n")
        self.assertEqual(self.agent.update_context({}, [])["memories"], "")
        self.agent.MEMORY_INDEX.unlink()
        prompt = self.agent.get_system_prompt(self.agent.update_context({}, []))
        self.assertNotIn("Relevant memories:", prompt)

    def test_tool_write_refreshes_next_request_prompt(self):
        self.responses = [{"tool_calls": [tool_call(
            "write-memory", "write_file", json.dumps({
                "path": ".memory/MEMORY.md", "content": "new index entry",
            }),
        )]}, {"content": "done"}]
        self.run_loop()
        self.assertNotIn("Relevant memories:", self.requests[0]["messages"][0]["content"])
        self.assertIn("new index entry", self.requests[1]["messages"][0]["content"])

    def test_external_index_edit_refreshes_first_request_prompt(self):
        stale_context = self.agent.update_context({}, [])
        self.agent.get_system_prompt(stale_context)
        self.agent.run_write(".memory/MEMORY.md", "external edit")
        self.responses = [{"content": "done"}]
        self.run_loop(stale_context)
        self.assertIn("external edit", self.requests[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()

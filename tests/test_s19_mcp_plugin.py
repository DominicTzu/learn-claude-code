"""默认离线测试；设置 S19_MCP_TEST_COMMAND 后额外启动真正的 Playwright MCP。"""

import asyncio
import importlib.util
import io
import json
import os
import threading
import tempfile
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from mcp.types import CallToolResult, ImageContent, TextContent, Tool
from openai import AsyncOpenAI


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "s19_mcp_plugin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load_module("s19_plugin", "mcp_plugin.py")
teaching = load_module("s19_teaching", "mcp_teaching.py")
NAVIGATE = Tool(name="browser_navigate", description="Navigate", inputSchema={
    "type": "object", "properties": {"url": {"type": "string"}},
    "required": ["url"], "additionalProperties": False,
})


def call(number, arguments='{"url":"https://example.com"}', name=None):
    return {"id": f"call_{number}", "type": "function", "function": {
        "name": name or "mcp__playwright__browser_navigate", "arguments": arguments,
    }}


def model_client(responses, requests):
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "completion_test", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{
                "index": 0, "finish_reason": "stop", "message": responses.pop(0),
            }],
        })
    return AsyncOpenAI(
        api_key="test-not-a-real-key", base_url="https://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


class TeachingTests(unittest.TestCase):
    def test_connect_then_discover_then_dispatch(self):
        connected = {}
        self.assertEqual(len(teaching.assemble_tool_pool(connected)[0]), 1)
        teaching.connect_mcp("docs", connected)
        teaching.connect_mcp("docs", connected)
        tools, routes = teaching.assemble_tool_pool(connected)
        self.assertEqual(len(tools), 2)
        result = teaching.dispatch(call(1, '{"query":"MCP"}', "mcp__docs__search"), connected, routes)
        self.assertIn("Playwright MCP", result["content"][0]["text"])
        with self.assertRaises(ValueError):
            teaching.connect_mcp("arbitrary-command", connected)


class HelperTests(unittest.TestCase):
    def test_command_does_not_forward_model_secrets(self):
        with patch.object(plugin.shutil, "which", return_value="/bin/node"):
            params = plugin.server_parameters(["node", "server.js"])
        self.assertIsNone(params.env)
        self.assertEqual(params.args, ["server.js"])
        for value in ([], "node server.js", ["node", 1]):
            with self.assertRaises(ValueError):
                plugin.server_parameters(value)

    def test_allowlist_and_original_name_mapping(self):
        tools, routes = plugin.assemble_tool_pool({NAVIGATE.name: NAVIGATE}, plugin.CHAT_ALLOWED)
        self.assertEqual(routes[tools[0]["function"]["name"]], NAVIGATE.name)
        self.assertEqual(tools[0]["function"]["parameters"], NAVIGATE.inputSchema)
        self.assertEqual(plugin.assemble_tool_pool({NAVIGATE.name: NAVIGATE}, set()), ([], {}))

    def test_normalization_collision_and_length_are_rejected(self):
        discovered = {name: Tool(name=name, inputSchema={"type": "object"}) for name in ["a.b", "a/b"]}
        with self.assertRaises(ValueError):
            plugin.assemble_tool_pool(discovered, set(discovered))
        long_name = "a" * 65
        with self.assertRaises(ValueError):
            plugin.assemble_tool_pool({long_name: NAVIGATE}, {long_name})

    def test_json_and_schema_validation(self):
        self.assertEqual(plugin.parse_arguments(NAVIGATE, '{"url":"https://example.com"}'), {"url": "https://example.com"})
        for value in ("{", "[]", "{}", '{"url":5}', '{"url":"x","unexpected":true}'):
            with self.assertRaises(ValueError):
                plugin.parse_arguments(NAVIGATE, value)

    def test_external_schema_reference_is_not_fetched(self):
        tool = Tool(name="unsafe", inputSchema={"$ref": "https://test.invalid/schema"})
        with self.assertRaisesRegex(ValueError, "外部引用"):
            plugin.parse_arguments(tool, "{}")

    def test_result_preserves_error_blocks_and_structured_data(self):
        result = CallToolResult(isError=True, content=[
            TextContent(type="text", text="first"), TextContent(type="text", text="second"),
            ImageContent(type="image", data="AA==", mimeType="image/png"),
        ], structuredContent={"detail": "failure"})
        payload = json.loads(plugin.result_text(result))
        self.assertTrue(payload["isError"])
        self.assertEqual(payload["content"][:2], ["first", "second"])
        self.assertIn("省略 image", payload["content"][2])
        self.assertEqual(payload["structuredContent"], {"detail": "failure"})

    def test_large_results_are_explicitly_truncated(self):
        result = CallToolResult(content=[TextContent(type="text", text="x" * 20000)])
        text = plugin.result_text(result)
        self.assertIn("结果已截断", text)
        self.assertLess(len(text), 17000)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination(self):
        session = SimpleNamespace(list_tools=AsyncMock(side_effect=[
            SimpleNamespace(tools=[NAVIGATE], nextCursor="page2"),
            SimpleNamespace(tools=[Tool(name="browser_close", inputSchema={})], nextCursor=None),
        ]))
        self.assertEqual(len(await plugin.discover_tools(session)), 2)
        self.assertEqual(session.list_tools.call_args_list[1].kwargs, {"cursor": "page2"})

    async def test_duplicate_tools_and_repeated_cursor_stop(self):
        for pages in ([
            SimpleNamespace(tools=[NAVIGATE], nextCursor="page2"),
            SimpleNamespace(tools=[NAVIGATE], nextCursor=None),
        ], [
            SimpleNamespace(tools=[], nextCursor="same"),
            SimpleNamespace(tools=[], nextCursor="same"),
        ]):
            session = SimpleNamespace(list_tools=AsyncMock(side_effect=pages))
            with self.assertRaises(ValueError):
                await plugin.discover_tools(session)

    async def run_turn(self, responses, session, confirm):
        requests, messages = [], [{"role": "user", "content": "test"}]
        async with model_client(responses, requests) as client:
            with redirect_stdout(io.StringIO()):
                await plugin.run_turn(client, "test-model", session, {NAVIGATE.name: NAVIGATE}, messages, confirm)
        return requests, messages

    async def test_real_sdk_serializes_complete_tool_history(self):
        session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[TextContent(type="text", text="page ready")])))
        confirm = AsyncMock(return_value=True)
        requests, messages = await self.run_turn([
            {"role": "assistant", "content": None, "tool_calls": [call(1)]},
            {"role": "assistant", "content": "done"},
        ], session, confirm)
        session.call_tool.assert_awaited_once_with("browser_navigate", {"url": "https://example.com"})
        self.assertEqual(requests[1]["messages"][-2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call_1")
        self.assertIn("page ready", requests[1]["messages"][-1]["content"])
        self.assertEqual(messages[-1]["content"], "done")

    async def test_invalid_unknown_and_denied_calls_do_not_execute(self):
        session = SimpleNamespace(call_tool=AsyncMock())
        confirm = AsyncMock(return_value=False)
        _, messages = await self.run_turn([
            {"role": "assistant", "tool_calls": [call(1, "{"), call(2, "{}", "unsafe"), call(3)]},
            {"role": "assistant", "content": "not executed"},
        ], session, confirm)
        session.call_tool.assert_not_awaited()
        confirm.assert_awaited_once()
        results = [m for m in messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["call_1", "call_2", "call_3"])

    async def test_connection_failure_is_not_retried_and_remaining_calls_are_paired(self):
        session = SimpleNamespace(call_tool=AsyncMock(side_effect=TimeoutError("timeout")))
        messages, requests = [{"role": "user", "content": "test"}], []
        async with model_client([{"role": "assistant", "tool_calls": [call(1), call(2)]}], requests) as client:
            with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                await plugin.run_turn(client, "test-model", session, {NAVIGATE.name: NAVIGATE}, messages, AsyncMock(return_value=True))
        session.call_tool.assert_awaited_once()
        self.assertEqual(len(requests), 1)
        self.assertIn("状态可能未知", messages[-2]["content"])
        self.assertEqual(messages[-1]["tool_call_id"], "call_2")

    async def test_execution_budget_still_fills_every_tool_result(self):
        session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[])))
        with patch.object(plugin, "MAX_ROUNDS", 1), patch.object(plugin, "MAX_CALLS_PER_ROUND", 1):
            _, messages = await self.run_turn([
                {"role": "assistant", "tool_calls": [call(1), call(2)]},
            ], session, AsyncMock(return_value=True))
        session.call_tool.assert_awaited_once()
        self.assertEqual(messages[-1]["tool_call_id"], "call_2")
        self.assertIn("上限", messages[-1]["content"])


@unittest.skipUnless(os.getenv("S19_MCP_TEST_COMMAND"), "未设置真实 MCP 测试启动命令")
class LiveMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_navigation_snapshot_and_click(self):
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'''<!doctype html><title>MCP smoke</title>
                <h1>MCP smoke ready</h1><button onclick="this.textContent='Completed'">Mark complete</button>'''
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        # 只服务上面写死的测试页面，不开放项目文件，也不访问私人浏览器资料。
        http = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        output = tempfile.TemporaryDirectory(prefix="s19-mcp-test-")
        try:
            command = json.loads(os.environ["S19_MCP_TEST_COMMAND"])
            command += ["--snapshot-mode", "none", "--output-dir", output.name]
            async with plugin.open_mcp(command) as (session, info):
                tools = await plugin.discover_tools(session)
                self.assertIn("browser_navigate", tools)
                self.assertIn("browser_click", tools)
                try:
                    args = plugin.parse_arguments(tools["browser_navigate"], json.dumps({"url": f"http://127.0.0.1:{http.server_port}"}))
                    result = await session.call_tool("browser_navigate", args)
                    self.assertFalse(result.isError, plugin.result_text(result))
                    snapshot = await session.call_tool("browser_snapshot", {})
                    self.assertFalse(snapshot.isError, plugin.result_text(snapshot))
                    self.assertIn("MCP smoke ready", plugin.result_text(snapshot))
                    # target 是固定版本服务器实际公布的参数，而不是手写 Python 点击。
                    args = plugin.parse_arguments(tools["browser_click"], '{"target":"button"}')
                    result = await session.call_tool("browser_click", args)
                    self.assertFalse(result.isError, plugin.result_text(result))
                    snapshot = await session.call_tool("browser_snapshot", {})
                    self.assertFalse(snapshot.isError, plugin.result_text(snapshot))
                    self.assertIn("Completed", plugin.result_text(snapshot))
                    print(f"REAL MCP: {info.serverInfo.name}; protocol={info.protocolVersion}; tools={len(tools)}; navigation/click/snapshot passed")
                finally:
                    await session.call_tool("browser_close", {})
        finally:
            await asyncio.to_thread(http.shutdown)
            http.server_close()
            worker.join(timeout=5)
            output.cleanup()


if __name__ == "__main__":
    unittest.main()

"""CLI 教学示例：真实临时 Git、离线模型消息，以及可选的真实浏览器。"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "s19_mcp_vs_cli" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lesson = load_module("cli_lesson", "code.py")
browser = load_module("cli_browser_lesson", "playwright_cli_demo.py")


class RunnerTests(unittest.TestCase):
    def test_allowlist_rejects_shell_and_extra_flags_without_running(self):
        with patch.object(lesson.subprocess, "run") as run:
            for argv in ("git status", ["git", "status", "--short", ";", "echo"],
                         ["git", "reset", "--hard"], ["git", "-c", "x=y", "status"],
                         ["python", "-c", "print(1)"], [], None):
                self.assertEqual(lesson.run_cli(argv, ROOT)["status"], "rejected")
            run.assert_not_called()

    def test_process_receives_array_no_shell_fixed_cwd_and_bounded_wait(self):
        with patch.object(lesson.shutil, "which", return_value="/bin/git"), patch.object(
            lesson.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="clean", stderr="warning")
        ) as run:
            result = lesson.run_cli(["git", "status", "--short"], ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stderr"], "warning")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/bin/git", "--no-pager", "-c", "core.fsmonitor=false", "status", "--short"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["cwd"], ROOT)
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_missing_program_and_failed_command(self):
        with patch.object(lesson.shutil, "which", return_value=None):
            result = lesson.run_cli(["git", "--version"], ROOT)
            self.assertIsNone(result["exit_code"])
            self.assertIn("找不到", result["stderr"])
        with patch.object(lesson.shutil, "which", return_value="/bin/git"), patch.object(
            lesson.subprocess, "run", return_value=SimpleNamespace(returncode=128, stdout="", stderr="not a repository")
        ):
            result = lesson.run_cli(["git", "status", "--short"], ROOT)
            self.assertEqual(result["exit_code"], 128)
            self.assertEqual(result["status"], "error")

    def test_timeout_preserves_partial_output_and_does_not_retry(self):
        error = subprocess.TimeoutExpired(["git"], 15, output=b"partial", stderr=b"diagnostic")
        with patch.object(lesson.shutil, "which", return_value="/bin/git"), patch.object(
            lesson.subprocess, "run", side_effect=error
        ) as run:
            result = lesson.run_cli(["git", "--version"], ROOT)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["stdout"], "partial")
        self.assertIn("不自动重试", result["stderr"])
        run.assert_called_once()

    def test_large_output_is_labeled(self):
        text = lesson.output_text("x" * 9000)
        self.assertLess(len(text), 8100)
        self.assertIn("输出已截断", text)

    def test_credentials_and_git_overrides_are_not_inherited(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "never-forward", "GIT_CONFIG_COUNT": "99"}):
            for env in (lesson.child_environment(), browser.cli_environment()):
                self.assertNotIn("LLM_API_KEY", env)
                self.assertNotIn("GIT_CONFIG_COUNT", env)


@unittest.skipUnless(shutil.which("git"), "需要 Git")
class RealGitTests(unittest.TestCase):
    def test_readonly_queries_against_temporary_repo(self):
        with tempfile.TemporaryDirectory(prefix="cli-git-test-") as directory:
            repo = Path(directory)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, env=lesson.child_environment(),
                                      capture_output=True, text=True, check=True)

            git("init", "-q")
            file = repo / "sample.txt"
            file.write_text("first\n", encoding="utf-8")
            git("add", "sample.txt")
            git("-c", "user.name=Lesson", "-c", "user.email=lesson@example.invalid",
                "commit", "--no-gpg-sign", "-qm", "Initial lesson fixture")
            file.write_text("first\nsecond\n", encoding="utf-8")
            before_head = git("rev-parse", "HEAD").stdout
            before_index = (repo / ".git" / "index").read_bytes()
            for argv in lesson.ALLOWED_COMMANDS:
                result = lesson.run_cli(argv, repo)
                self.assertEqual(result["exit_code"], 0, result)
            status = lesson.run_cli(["git", "status", "--short"], repo)
            self.assertIn("sample.txt", status["stdout"])
            self.assertEqual(file.read_text(encoding="utf-8"), "first\nsecond\n")
            self.assertEqual(before_index, (repo / ".git" / "index").read_bytes())
            self.assertEqual(before_head, git("rev-parse", "HEAD").stdout)


def tool_call(number, arguments, name="run_cli"):
    return {"id": f"call_{number}", "type": "function", "function": {"name": name, "arguments": arguments}}


def fake_client(responses, requests):
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test_completion", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": responses.pop(0)}],
        })
    return OpenAI(api_key="not-a-real-key", base_url="https://test.invalid/v1", max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(handle)))


class ModelLoopTests(unittest.TestCase):
    def test_sdk_preserves_assistant_and_matching_results(self):
        requests, messages = [], [{"role": "user", "content": "inspect"}]
        responses = [
            {"role": "assistant", "tool_calls": [tool_call(1, '{"argv":["git","status","--short"]}')]},
            {"role": "assistant", "content": "observed"},
        ]
        with fake_client(responses, requests) as client, patch.object(
            lesson, "run_cli", return_value={"status": "ok", "exit_code": 0, "stdout": " M sample.txt", "stderr": ""}
        ) as run, redirect_stdout(io.StringIO()):
            lesson.agent_turn(client, "test-model", messages, ROOT)
        run.assert_called_once_with(["git", "status", "--short"], ROOT)
        self.assertEqual(len(requests[0]["tools"]), 1)
        self.assertEqual(requests[1]["messages"][-2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call_1")
        self.assertIn("sample.txt", requests[1]["messages"][-1]["content"])

    def test_malformed_unknown_and_excess_calls_are_paired_but_not_executed(self):
        requests, messages = [], [{"role": "user", "content": "inspect"}]
        responses = [{"role": "assistant", "tool_calls": [
            tool_call(1, "{"), tool_call(2, "{}", "unknown"), tool_call(3, "[]"),
            tool_call(4, '{"argv":["git","--version"]}'),
        ]}]
        with fake_client(responses, requests) as client, patch.object(lesson, "MAX_ROUNDS", 1), patch.object(
            lesson, "MAX_CALLS_PER_ROUND", 3
        ), patch.object(lesson, "run_cli") as run, redirect_stdout(io.StringIO()):
            lesson.agent_turn(client, "test-model", messages, ROOT)
        run.assert_not_called()
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"],
                         ["call_1", "call_2", "call_3", "call_4"])


class BrowserRunnerTests(unittest.TestCase):
    def test_json_output_and_business_errors(self):
        for payload in ({"snapshot": []}, {"isError": True}, {"result": {"isError": True}}):
            response = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            with patch.object(browser.subprocess, "run", return_value=response) as run, redirect_stdout(io.StringIO()):
                if "snapshot" in payload:
                    self.assertEqual(browser.invoke(["node", "cli.js"], "one", ["snapshot"], ROOT), payload)
                else:
                    with self.assertRaises(RuntimeError):
                        browser.invoke(["node", "cli.js"], "one", ["snapshot"], ROOT)
            self.assertEqual(run.call_args.args[0], ["node", "cli.js", "-s=one", "--json", "snapshot"])
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_close_own_session_on_failure(self):
        with patch.object(browser.shutil, "which", return_value="/bin/node"), patch.object(
            browser, "invoke", side_effect=[RuntimeError("navigation failed"), {"status": "closed"}]
        ) as invoke, self.assertRaisesRegex(RuntimeError, "navigation failed"):
            browser.run_demo(["node", "cli.js"], "http://127.0.0.1")
        self.assertEqual(invoke.call_args_list[-1].args[2], ["close"])
        self.assertEqual(invoke.call_args_list[0].args[1], invoke.call_args_list[1].args[1])

    def test_invalid_launch_config_and_file_urls(self):
        for command in ([], "node cli.js", ["node", 5]):
            with self.assertRaises(ValueError):
                browser.run_demo(command, "https://example.com")
        with patch.object(browser.shutil, "which", return_value="/bin/node"):
            with self.assertRaises(ValueError):
                browser.run_demo(["node", "cli.js"], "file:///private/example")


@unittest.skipUnless(os.getenv("S19_CLI_TEST_COMMAND"), "未设置真实 Playwright CLI 启动命令")
class LiveBrowserTests(unittest.TestCase):
    def test_navigation_snapshot_click_and_cleanup(self):
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'''<!doctype html><title>CLI fixture</title><h1>CLI ready</h1>
                <button onclick="this.textContent='Clicked via CLI'">Click test</button>'''
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            command = json.loads(os.environ["S19_CLI_TEST_COMMAND"])
            with redirect_stdout(io.StringIO()) as output:
                results = browser.run_demo(command, f"http://127.0.0.1:{server.server_port}", "button")
            self.assertIn("CLI ready", json.dumps(results[1]))
            self.assertIn("Clicked via CLI", json.dumps(results[-1]))
            self.assertIn('"status": "closed"', output.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

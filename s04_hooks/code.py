#!/usr/bin/env python3
"""
s04: Hooks — move extension logic out of the loop, onto hooks.

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (tool_calls?)          │
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_calls ──────┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

Changes from s03:
  + HOOKS registry (event -> list of callbacks)
  + register_hook() / trigger_hooks()
  + context_inject_hook (UserPromptSubmit)
  + permission_hook, log_hook (PreToolUse)
  + large_output_hook (PostToolUse)
  + summary_hook (Stop)
  - check_permission() removed from loop body
    (logic moved into permission_hook, triggered via PreToolUse)

Run: python s04_hooks/code.py
Needs: pip install openai python-dotenv + .env 中配置 LLM_API_KEY、
       LLM_BASE_URL 和 LLM_MODEL_ID（例如 DeepSeek 的 OpenAI 兼容接口）
"""

import json
import os
import subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# 将 .env 中的 LLM_* 配置加载进当前进程的环境变量。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

WORKDIR = Path.cwd()
# OpenAI SDK 也可连接 OpenAI 兼容服务。DeepSeek 时 LLM_BASE_URL 指向其 API 地址。
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# ═══════════════════════════════════════════════════════════
#  FROM s02-s03 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # 先将 ../ 和符号链接解析成真实路径，避免通过相对路径逃出工作区。
    path = (WORKDIR / p).resolve()
    # is_relative_to 返回 path 是否仍在 WORKDIR 目录树中。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        # 模型只会请求调用 bash；真正启动终端进程的是 subprocess.run。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

TOOLS = [
    # OpenAI function calling 的工具说明。模型据此生成 tool_calls，但不会自行执行函数。
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace exact text in a file once.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
    }},
    {"type": "function", "function": {
        "name": "glob", "description": "Find files matching a glob pattern.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
    }},
]

# 工具名 -> Python 实现函数。agent loop 不需要知道每个工具的具体实现。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s04: Hook System (s03 permission logic now via hooks)
# ═══════════════════════════════════════════════════════════
# userpromptsubmit: query 进入 messages 前触发，用于注入上下文或修改 query。
# pretooluse: 工具调用前触发，用于权限检查、日志记录等
# posttooluse: 工具调用后触发，用于检查输出、日志记录等
# stop: agent_loop 即将退出时触发，用于打印统计信息或清理资源。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback): # callback 是一个函数对象，event 是事件名称（字符串），用于注册回调函数到指定的生命周期事件中。
    """把回调注册到一个生命周期事件；同一事件可按注册顺序有多个回调。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """依次触发事件的回调；第一个非 None 返回值会阻止后续回调和当前操作。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None: # result 非 None 表示回调函数希望阻止后续操作（例如权限拒绝），因此 agent_loop 会停止执行当前工具调用，并将 result 作为输出返回。
            return result
    return None


# s03 permission check logic, now wrapped as a hook
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def permission_hook(name: str, args: dict):
    """PreToolUse: 在执行前拦截危险工具请求；返回字符串即拒绝本次调用。"""
    if name == "bash":
        for pattern in DENY_LIST:
            if pattern in args.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in args.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {name}({args})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"): # 如果是y或yes，则允许执行，否则拒绝。
                    return "Permission denied by user"
    if name in ("write_file", "edit_file"):
        path = args.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {name}({args})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(name: str, args: dict):
    """PreToolUse: log every tool call."""
    args_preview = str(list(args.values())[:2])[:60] # args的values通常是一个字典，取前两个值并转换为字符串，截取前60个字符作为预览。
    print(f"\033[90m[HOOK] {name}({args_preview})\033[0m")
    return None

def large_output_hook(name: str, args: dict, output: str):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {name}: {len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str): # 用于在用户输入进入 messages 前注入上下文或修改 query，例如可以在这里添加一些系统提示或上下文信息，或者对用户输入进行预处理。
    """UserPromptSubmit: 用户输入进入 messages 前触发。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    """Stop: OpenAI 格式中，每条工具结果都是一条 role='tool' 历史消息。"""
    tool_count = sum(1 for message in messages if message.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same structure as s03, but no hard-coded check
#  s03: if not check_permission(name, args): ...
#  s04: if trigger_hooks("PreToolUse", name, args): ...
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        # 每轮将 system prompt 与完整对话历史发送给模型。
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}, *messages],
            tools=TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message

        # 必须保留模型原样返回的 tool_calls，后续 role='tool' 结果才能用 ID 对应它。
        messages.append(assistant_message.model_dump(exclude_none=True))

        # 没有 tool_calls 时，模型已完成回答；Stop hook 可选择注入一条消息要求继续。
        if not assistant_message.tool_calls:
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 一个模型回复可以提出多个工具调用；教学版按返回顺序逐个执行。
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
                # OpenAI 兼容接口把函数参数放在 JSON 字符串中，需要先转为 dict。
                args = json.loads(tool_call.function.arguments or "{}") # args通常是一个字典，包含工具调用的参数，例如 {"command": "ls -l"}。
            except json.JSONDecodeError as e:
                output = f"Error: invalid tool arguments: {e}"
            else:
                # s04 的关键：循环只触发生命周期事件，不硬编码权限或日志逻辑。
                blocked = trigger_hooks("PreToolUse", name, args) # trigger_hooks会调用所有注册在 PreToolUse 的回调函数，按注册顺序执行。如果有回调返回非 None 值，则表示阻止当前工具调用，并将该值作为输出返回。
                if blocked:
                    output = str(blocked)
                else:
                    handler = TOOL_HANDLERS.get(name)
                    output = handler(**args) if handler else f"Unknown: {name}"
                    trigger_hooks("PostToolUse", name, args, output)

            # 结果必须作为独立 role='tool' 消息追加，tool_call_id 与模型请求一一对应。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # agent_loop 返回时，最后一条消息就是没有 tool_calls 的模型文本回复。
        response_content = history[-1].get("content")
        if response_content:
            print(response_content)
        print()

#!/usr/bin/env python3
"""
s05: TodoWrite — add a planning tool on top of s04 hooks.

  +---------+      +-------+      +------------------+
  |  User   | ---> |  LLM  | ---> | TOOL_HANDLERS    |
  | prompt  |      |       |      |  bash            |
  +---------+      +---+---+      |  read_file       |
                        ^         |  write_file      |
                        | result  |  edit_file       |
                        +---------+  glob            |
                                      todo_write ← NEW
                                   +------------------+
                                        |
                         in-memory current_todos
                                        |
                        if rounds_since_todo >= 3:
                          inject <reminder>

Changes from s04:
  + todo_write tool + run_todo_write() implementation
  + Nag reminder (inject reminder after 3 rounds without todo update)
  + SYSTEM prompt includes "plan before execute" guidance
  + rounds_since_todo counter in agent_loop
  Loop unchanged: new tool auto-dispatches via TOOL_HANDLERS.

Run: python s05_todo_write/code.py
Needs: pip install openai python-dotenv + .env 中配置 LLM_API_KEY、
       LLM_BASE_URL 和 LLM_MODEL_ID（例如 DeepSeek 的 OpenAI 兼容接口）
"""

import ast
import json
import os
import subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# 从 .env 读取与 s01-s04 共用的 OpenAI 兼容服务配置。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

WORKDIR = Path.cwd()
# base_url 为空时连接 OpenAI；设置为 DeepSeek 等地址时走 OpenAI 兼容协议。
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]
# TodoWrite 的状态仅存在进程内存中，程序退出或重启后会清空。
CURRENT_TODOS: list[dict] = []

# s05 change: SYSTEM prompt adds planning guidance
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s04 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # 解析 ../ 和符号链接后，确认真实路径仍在工作目录内。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        # 真正执行终端命令的入口。模型只能请求 bash，不能自行运行命令。
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


# ═══════════════════════════════════════════════════════════
#  NEW in s05: todo_write tool — plan only, no execution
# ═══════════════════════════════════════════════════════════

def _normalize_todos(todos):
    """校验模型传入的 TODO 列表，返回 (规范化列表, 错误消息)。"""
    # 正常 function call 会传入 list；保留字符串解析是为了兼容错误格式的调用。
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos) # ast.literal_eval 可以安全地将字符串解析为 Python 对象，避免执行任意代码
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    """用完整的新列表覆盖当前计划，并在终端显示状态；不执行任何实际工作。"""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"] # \033[33m 是 ANSI 转义码，用于在终端中显示黄色文本
    for t in CURRENT_TODOS:
        # icon 显示任务状态：pending 用空格，in_progress 用蓝色箭头，completed 用绿色对勾。[t["status"]] 是字典访问，获取任务的状态值。
        # 比如 t["status"] 是 "in_progress"，那么 icon 就是 "\033[36m▸\033[0m"，表示蓝色箭头。
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}") # f-string 用于格式化字符串，{icon} 和 {t['content']} 会被替换为对应的值。
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

TOOLS = [
    # 每项是给模型的工具说明。模型根据 schema 生成 tool_calls，由 Python 执行。
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
    # s05: 这个工具只维护计划，不读取文件、不修改文件、也不执行命令。
    {"type": "function", "function": {
        "name": "todo_write", "description": "Create and manage a task list for your current coding session.",
        "parameters": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]},
    }},
]

# 工具名 -> 本地 Python 函数。新增工具只需注册到该映射。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """注册生命周期回调；同一事件的回调按添加顺序运行。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """依次触发回调；第一个非 None 返回值阻止当前操作。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# s04 hooks preserved
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

def permission_hook(name: str, args: dict):
    """PreToolUse: deny list check."""
    if name == "bash":
        for p in DENY_LIST:
            if p in args.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(name: str, args: dict):
    """PreToolUse: log tool calls."""
    print(f"\033[90m[HOOK] {name}\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit: log working directory."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop: OpenAI 格式中，每条工具执行结果都是一条 role='tool' 消息。"""
    tool_count = sum(1 for message in messages if message.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s04 + nag reminder counter
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0

def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        # s05: 连续 3 个工具调用轮次没有更新计划时，加入 reminder 提醒模型。
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}, *messages],
            tools=TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message
        # 必须保留原始 tool_calls，后面的 role='tool' 结果靠 tool_call_id 与之对应。
        messages.append(assistant_message.model_dump(exclude_none=True))

        # 没有工具调用时，模型已给出最终文本；Stop hook 可通过返回内容要求继续。
        if not assistant_message.tool_calls:
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 这里的一轮指一次“模型请求工具”的回复，而非单个工具调用。
        rounds_since_todo += 1
        # 同一回复可能包含多个 tool_calls；教学版按模型返回顺序逐个执行。
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
                # arguments 是 JSON 字符串，解析后才能用 **args 调用本地函数。
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as e:
                output = f"Error: invalid tool arguments: {e}"
            else:
                blocked = trigger_hooks("PreToolUse", name, args)
                if blocked:
                    output = str(blocked)
                else:
                    handler = TOOL_HANDLERS.get(name)
                    output = handler(**args) if handler else f"Unknown: {name}"
                    trigger_hooks("PostToolUse", name, args, output)

                    # todo_write 未被 hook 拦截并已执行后，重置提醒计数器。
                    if name == "todo_write":
                        rounds_since_todo = 0

            # 工具结果作为独立消息回传，模型下一轮会根据结果选择继续执行还是回复用户。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    print("s05: TodoWrite — plan before execute, nag if you forget")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # agent_loop 返回时，最后一条 history 通常是模型最终的文本回复。
        response_content = history[-1].get("content")
        if response_content:
            print(response_content)
        print()

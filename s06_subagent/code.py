#!/usr/bin/env python3
"""
s06: Subagent — spawn sub-agents with fresh messages[] for context isolation.

  Parent Agent                           Subagent
  +------------------+                  +------------------+
  | messages=[...]   |                  | messages=[task]  | <-- fresh
  |                  |   dispatch       |                  |
  | tool: task       | ---------------> | own while loop   |
  |   prompt="..."   |                  |   bash/read/...  |
  |                  |   summary only   |   (max 30 turns) |
  | result = "..."   | <--------------- | return last text |
  +------------------+                  +------------------+
        ^                                      |
        |       intermediate results DISCARDED  |
        +--------------------------------------+

  Subagent tools: bash, read, write, edit, glob (NO task — no recursion)

Changes from s05:
  + task tool + spawn_subagent() with fresh messages[]
  + Safety limit: max 30 turns per subagent
  + extract_text() helper
  Subagent cannot spawn sub-subagents (no task tool in sub_tools).
  Main loop unchanged: task auto-dispatches via TOOL_HANDLERS.

Run: python s06_subagent/code.py
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

# 与 s01-s05 一样，从 .env 读取 OpenAI 兼容服务的连接信息。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

WORKDIR = Path.cwd()
# 通过 base_url 可将 OpenAI SDK 指向 DeepSeek 等兼容服务。
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]
# Todo 列表只属于主 Agent 的当前进程；子 Agent 没有 todo_write 工具。
CURRENT_TODOS: list[dict] = []

SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "For complex sub-problems, use the task tool to spawn a subagent."
)

# s06: 子 Agent 使用自己的系统提示，明确禁止继续分派。
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s05 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # resolve() 会消除 ../ 和符号链接，再检查真实路径是否仍在工作目录内。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        # 模型只能请求 bash；实际运行终端命令的是 Python 的 subprocess.run。
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

def _normalize_todos(todos):
    """校验 todo_write 的列表输入，返回 (规范化列表, 错误消息)。"""
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
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

def run_todo_write(todos: list) -> str: # subagent没有此工具，只有主 agent 可以更新 todo。
    """更新主 Agent 的内存计划并显示状态，本身不执行文件或终端操作。"""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

TOOLS = [
    # OpenAI function calling 的 schema：模型据此生成 tool_calls，Python 再执行对应函数。
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
    {"type": "function", "function": {
        "name": "todo_write", "description": "Create and manage a task list for your current coding session.",
        "parameters": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]},
    }},
]

# 工具名 -> 主 Agent 本地实现；task 在后面注册，因为它依赖 spawn_subagent()。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s06: Subagent — fresh messages[], summary only
# ═══════════════════════════════════════════════════════════

SUB_TOOLS = [
    # 子 Agent 只拥有执行子任务需要的五个工具；没有 task 和 todo_write。
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
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
# 没有 task 工具，因此子 Agent 无法继续创建子 Agent，避免无限递归。

SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

def extract_text(content) -> str:
    """从 OpenAI assistant message 的 content 字段取出最终文本。"""
    return content if isinstance(content, str) else ""

def spawn_subagent(description: str) -> str:
    """用独立 messages 启动同步子 Agent，丢弃中间过程，只向父 Agent 返回最终摘要。"""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    # 这里不复制父 Agent 的 messages：子 Agent 只知道自己的任务，获得干净上下文。
    messages = [{"role": "user", "content": description}]

    for _ in range(30): # 循环控制的是子 Agent 的最大轮次，避免无限循环。每轮可能有多个工具调用，但总共不超过 30 轮。
        # 子 Agent 自己也有完整的“模型 -> 工具 -> 结果 -> 模型”循环。
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUB_SYSTEM}, *messages],
            tools=SUB_TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message
        messages.append(assistant_message.model_dump(exclude_none=True))

        # 没有工具调用即为子 Agent 最终回答，马上返回给父 Agent。
        if not assistant_message.tool_calls:
            break

        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as e:
                output = f"Error: invalid tool arguments: {e}"
            else:
                # 子 Agent 仍使用同一套 hooks，因此权限拦截会在父终端中显示。
                blocked = trigger_hooks("PreToolUse", name, args)
                if blocked:
                    output = str(blocked)
                else:
                    handler = SUB_HANDLERS.get(name)
                    output = handler(**args) if handler else f"Unknown: {name}"
                    trigger_hooks("PostToolUse", name, args, output)
                    print(f"  \033[90m[sub] {name}: {str(output)[:100]}\033[0m")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

    # 到达 30 轮上限时，最后一条可能是 tool 结果；向后寻找最近的 assistant 文本。
    result = extract_text(messages[-1].get("content"))
    if not result: # 如果最后一条不是 assistant 文本，则向前查找。
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg.get("content"))
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result  # only summary, entire message history discarded

# task 仅加入主 Agent；调用时会运行 spawn_subagent()，其返回摘要再作为工具结果交给主模型。
TOOLS.append({
    "type": "function",
    "function": {
        "name": "task",
        "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
        "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
    },
})
TOOL_HANDLERS["task"] = spawn_subagent


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """注册生命周期回调；同一事件可按顺序注册多个函数。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """依次执行回调；任一回调返回非 None 时阻止当前操作。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

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
    """Stop: OpenAI 历史中，每条 role='tool' 消息代表一次工具结果。"""
    tool_count = sum(1 for message in messages if message.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s05 + nag reminder, task auto-dispatches
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0

def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        # 连续 3 个工具调用轮次未更新 TODO 时，向模型注入提醒。
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
        # 原样保存 assistant tool_calls，才能使后续 tool_call_id 有对应对象。
        messages.append(assistant_message.model_dump(exclude_none=True))

        if not assistant_message.tool_calls:
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        rounds_since_todo += 1
        # 一个 assistant 回复可携带多个工具调用，教学版按返回顺序串行执行。
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
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

                    # 主 Agent 执行 todo_write handler 后，清零 reminder 计数。
                    if name == "todo_write":
                        rounds_since_todo = 0

            # 结果回传给主模型；task 的 output 就是子 Agent 最终摘要，而不是其完整历史。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    print("s06: Subagent — spawn sub-agents with fresh context, summary only")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # agent_loop 结束后，最后一条通常是主模型的最终文本回复。
        response_content = history[-1].get("content")
        if response_content:
            print(response_content)
        print()

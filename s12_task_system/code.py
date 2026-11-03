#!/usr/bin/env python3
"""
s12: Task System — file-persisted task graph with blockedBy dependencies.

Run:  python s12_task_system/code.py
Need: pip install openai python-dotenv
      .env 中配置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL_ID

Changes from s11:
  - Task dataclass (id, subject, description, status, owner, blockedBy)
  - TASKS_DIR = .tasks/ for persistent JSON storage
  - create_task / save_task / load_task / list_tasks / get_task
  - can_start: checks blockedBy all completed (missing deps = blocked)
  - claim_task: set owner + pending -> in_progress
  - complete_task: set completed + report unblocked downstream
  - 5 new tools: create_task, list_tasks, get_task, claim_task, complete_task

本章主线：创建任务并落盘 -> 检查依赖 -> 认领 -> 执行实际工作 -> 标记完成。
任务工具负责维护工作记录，不会自动执行任务，也不会自动启动子 Agent。
为突出任务系统，本章保留基础 agent loop，没有合入 s11 的完整错误恢复，
也没有复制 s08/s09 的压缩和记忆检索；这些能力可以作为独立层组合。
"""

import json
import os
import random
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass, asdict

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# 沿用前几章：SDK 是客户端，实际模型和服务地址由 .env 决定。
# override=True 使 .env 中的同名配置覆盖已有环境变量，不在源码里保存密钥。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

# cwd 是启动命令所在目录，不是 code.py 所在目录。建议从项目根目录启动，
# 这样每次运行都会使用同一份 .tasks 和 .memory，而不是在不同位置新建任务库。
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]

# ── Task System ──

TASKS_DIR = WORKDIR / ".tasks"
# exist_ok=True：目录存在时继续使用，不会清空已有任务。
TASKS_DIR.mkdir(exist_ok=True)


# @dataclass 根据字段自动生成 __init__、__repr__ 等方法，减少数据类的样板代码。
# 类型注解方便阅读和检查，但不会自动验证模型传入的数据，也不负责持久化。
@dataclass
class Task:
    """一个持久化工作单元；不同于 s06 的 task 工具，这里不创建子 Agent。"""
    id: str
    subject: str
    description: str
    status: str          # pending / in_progress / completed
    owner: str | None    # 负责人名称；None 表示尚未认领
    blockedBy: list[str] # 上游任务 ID：这些任务全部完成后才能认领当前任务


def _task_path(task_id: str) -> Path:
    """把工具返回的任务 ID 转为文件路径，不允许把 ID 当成任意文件路径。"""
    if (not isinstance(task_id, str) or not task_id
            or Path(task_id).name != task_id or "\\" in task_id):
        raise ValueError("task_id must be a file name, not a path")
    path = (TASKS_DIR / f"{task_id}.json").resolve()
    # resolve 还会展开符号链接，避免链接把任务读写引向 .tasks 之外。
    if not path.is_relative_to(TASKS_DIR.resolve()):
        raise ValueError("Task path escapes .tasks directory")
    return path


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建 pending 任务并立即落盘；创建记录本身不代表任务已经开始执行。"""
    # tools 中的 JSON schema 不是本地校验器，关键字段仍需在真正写文件前检查。
    if not isinstance(subject, str) or not isinstance(description, str):
        raise ValueError("subject and description must be strings")
    if blockedBy is not None and (
        not isinstance(blockedBy, list) or any(not isinstance(dep, str) for dep in blockedBy)
    ):
        raise ValueError("blockedBy must be a list of task IDs")
    for dep_id in blockedBy or []:
        _task_path(dep_id)
    # 时间戳 + 四位随机数是教学用 ID，并非严格无碰撞；生产系统应另做唯一性保障。
    # blockedBy=None 避免把可变列表 [] 作为默认参数在多次调用间共享。
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    """Task -> dict -> JSON 文本 -> 文件；修改对象后需要显式调用本函数保存。"""
    # asdict 取出 dataclass 字段；ensure_ascii=False 让中文标题直接可读。
    # 这里是覆盖写入，没有文件锁/原子替换，不是并发安全的任务数据库。
    _task_path(task.id).write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False), encoding="utf-8")


def load_task(task_id: str) -> Task:
    """文件 -> JSON 字典 -> Task；** 将字典按字段名展开成构造函数参数。"""
    return Task(**json.loads(_task_path(task_id).read_text(encoding="utf-8")))


def list_tasks() -> list[Task]:
    """每次扫描磁盘上的任务，而不是依赖本进程中的任务列表。"""
    # 重启后仍能读到记录，但不会恢复旧对话、模型上下文或未完成命令的执行现场。
    return [load_task(p.stem)
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """返回完整字段，尤其是 list_tasks 概览中没有展示的 description。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


def can_start(task_id: str) -> bool:
    """检查所有上游任务是否 completed；依赖不存在也视为阻塞，而不是忽略。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    # 没有依赖时循环不执行，直接允许开始。这里只检查状态，没有检测依赖环；
    # 若 A 等 B、B 又等 A，两者都会一直阻塞，需要上层修正任务图。
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领 = 检查 pending 和依赖 -> 写入负责人 -> 改为 in_progress。"""
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    # 这里只能防止顺序执行时重复认领；两个进程同时读取 pending 仍可能发生竞争，
    # 真正多 Agent 共享任务库时，需要把读取、检查、写入放进同一个锁/事务。
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """只允许 in_progress -> completed；标记完成不等于自动验收工作成果。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    # blockedBy 只保存上游引用，反向关系通过扫描得到，没有另存 blocks 字段。
    # 下面报告“所有带依赖且现在可开始”的 pending 任务，不仅是本次刚解锁的。
    # 报告可开始不等于自动执行；模型仍需调用 claim_task 并使用工具实际完成工作。
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: {enabled_tools}.",
    "workspace": "Working directory: {workspace}",
    "memory": "Relevant memories:\n{memories}",
}


def assemble_system_prompt(context: dict) -> str:
    """沿用 s10 的分段模板：固定顺序，工具/目录取实际值，记忆索引按需加入。"""
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"].format(
                    enabled_tools=", ".join(context["enabled_tools"])),
                PROMPT_SECTIONS["workspace"].format(workspace=context["workspace"])]
    memories = context.get("memories", "")
    if memories:
        sections.append(PROMPT_SECTIONS["memory"].format(memories=memories))
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    """状态不变时复用最近一次拼接结果；这不是模型回答或服务端 token 缓存。"""
    global _last_context_key, _last_prompt
    # sort_keys 消除字典键插入顺序的影响；system 字符串仍随每次请求发送。
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    """文件工具的路径限制；先解析 .. 和符号链接，再检查是否仍在工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """本地 Python 启动 shell 执行命令，模型本身不会直接操纵终端。"""
    try:
        # 捕获 stdout/stderr，最多等待 120 秒；cwd 只是起始目录，不是安全沙箱。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容，可选 limit 限制返回行数；错误也作为文本返回模型。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """创建父目录并覆盖写入；这是真实文件操作，任务完成状态不会替代它。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task tools

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """工具适配层：内部返回 Task 对象，对模型则返回包含任务 ID 的文本。"""
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """给模型返回轻量概览；需要具体要求时，再用 get_task 读取完整描述。"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    # 本章是单 Agent demo，因此 owner 固定为 agent，并未实现多执行者分派。
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


TOOLS = [
    # OpenAI function schema 告诉模型有哪些工具和参数，但不包含 Python 函数体。
    # 模型生成 tool_calls 后，程序才会解析参数并调用 TOOL_HANDLERS 中的函数。
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "limit": {"type": "integer"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "create_task",
        "description": "Create a new task with optional blockedBy dependencies.",
        "parameters": {"type": "object",
                       "properties": {
                           "subject": {"type": "string"},
                           "description": {"type": "string"},
                           "blockedBy": {"type": "array",
                                         "items": {"type": "string"}}},
                       "required": ["subject"]},
    }},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "List all tasks with status, owner, and dependencies.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_task", "description": "Get full details of a specific task by ID.",
        "parameters": {"type": "object",
                       "properties": {"task_id": {"type": "string"}},
                       "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "claim_task",
        "description": "Claim a pending task. Sets owner, changes status to in_progress.",
        "parameters": {"type": "object",
                       "properties": {"task_id": {"type": "string"}},
                       "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
        "parameters": {"type": "object",
                       "properties": {"task_id": {"type": "string"}},
                       "required": ["task_id"]},
    }},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """从实际工具/目录/记忆索引重建 prompt 状态，旧 context/messages 暂作预留参数。"""
    # .tasks 不会自动放进上下文；模型需用 list_tasks / get_task 得到任务信息。
    # .memory 这里只读索引，不像 s09 那样检索、注入完整记忆正文。
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop (simplified, focused on task system) ──

def append_assistant(messages: list, message: dict):
    """记录助手消息并打印文本，工具调用前的说明和最终答案都不会被静默丢弃。"""
    messages.append(message)
    if message.get("content"):
        print(message["content"])


def agent_loop(messages: list, context: dict):
    """基础工具循环：请求模型 -> 本地执行 -> 回传结果，直到模型不再请求工具。"""
    while True:
        # 每轮刷新，确保用户或工具修改 MEMORY.md 后，下一次模型请求能看到变化。
        context = update_context(context, messages)
        system = get_system_prompt(context)
        try:
            response = client.chat.completions.create(
                model=MODEL,
                # system 只拼在本次请求前面，不写进 history，避免过期版本不断累积。
                messages=[{"role": "system", "content": system}, *messages],
                tools=TOOLS,
                max_tokens=8000,
            )
        except Exception as e:
            # 本章只把请求失败显示出来，不实现 s11 的主动裁剪、升档、备用模型等恢复。
            append_assistant(messages, {"role": "assistant", "content":
                f"[Error] {type(e).__name__}: {e}"})
            return

        choice = response.choices[0]
        assistant_message = choice.message
        if choice.finish_reason == "length":
            # 不能把截断的任务参数拿去修改文件，也不能留下没有结果的 tool_calls。
            # 本章没有续写恢复：保留文本片段、给出错误，结束当前回合。
            append_assistant(messages, {"role": "assistant", "content":
                (assistant_message.content or "") +
                "\n[Error] Output truncated; tool calls from this response were not executed."})
            return

        # 保存完整 assistant 消息，尤其是 tool_calls；每个 tool 结果都要引用对应 ID。
        append_assistant(messages, assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            return

        # 一次模型响应可含多个调用，这里逐个执行。新任务 ID 在工具结果中返回；
        # 若下一个调用依赖这个新 ID，通常需要模型读到结果后再发起下一轮调用。
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")
            try:
                # arguments 是 JSON 字符串；解析成字典后，**args 才能展开成函数参数。
                args = json.loads(tool_call.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
                handler = TOOL_HANDLERS.get(name)
                output = handler(**args) if handler else f"Unknown: {name}"
            except (ValueError, TypeError, OSError) as error:
                # 包括 JSON/参数错误、任务不存在、文件损坏或无法写入等常见问题。
                # 错误也作为工具结果回传，让模型有机会修正参数，而不是直接崩溃。
                output = f"Error: {type(error).__name__}: {error}"
            print(str(output)[:300])
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })


if __name__ == "__main__":
    print("s12: task system")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # 对话历史只活在当前进程中，任务 JSON 独立持久化，两者不是同一份状态。
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        # 文本已由 append_assistant 显示，这里不再解析旧版 Anthropic 内容块。
        print()

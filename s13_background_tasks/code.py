#!/usr/bin/env python3
"""
s13：让耗时命令在后台运行。

运行：python s13_background_tasks/code.py
安装：pip install openai python-dotenv
配置：在 .env 中填写 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL_ID。

贯穿本章的例子：用户说“运行测试，等待时顺便读一下配置文件”。
假设模型选择用 bash 运行 pytest，并传入 run_in_background=true。

原来的做法：主线程运行 pytest -> 等测试结束 -> 读配置文件。
现在的做法：主线程把 pytest 交给后台线程 -> 继续读配置文件。
后台线程等 pytest 结束后，把结果放进字典；主线程稍后取出，再发给模型。

建议先找到 agent_loop，看它如何判断一个工具是否放到后台，然后依次看：
start_background_task -> run_background_worker -> collect_background_results
-> inject_background_notifications。

前半部分的 Task 和 prompt 组装沿用前几章，重点是后半部分的 Background Tasks。
"""

# 先把“谁在执行代码”分清楚，后面的注释会一直使用这三个名字：
#
# 主线程：从文件底部的 input 开始，接收问题、调用模型、处理模型要求的工具。
# 后台线程：只负责运行 run_background_worker，不和模型对话。
# shell 子进程：Python 请操作系统另外启动的程序，它实际执行 pytest 等命令。
#
# 主线程和后台线程属于同一个 Python 程序（同一个进程）。因此一个线程修改了
# background_tasks，另一个线程看到的也是修改后的那份字典，不必复制或发网络请求。
# shell 子进程是另一个程序，它不会直接修改这些字典，只把命令输出交回 Python。
#
# 后台线程不是第二个 Agent。“后台”也不是把终端窗口藏起来：
# 它只是让主线程不用停在 run_bash 那一行等待，而由另一个线程在那里等待。
# 等待的线程暂时不能执行下一行，这就叫“阻塞”；其他线程仍然可以往下执行。

import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from xml.sax.saxutils import escape

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off') # 意思是：不要用 ^A ^B ^C 等控制字符
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# 与前几章一致：OpenAI SDK 只是客户端，服务和模型由 .env 指定。
# override=True 让 .env 的同名配置覆盖环境变量，不要把真实密钥写进源码。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

# 从项目根目录启动，才能跨运行复用同一份 .tasks 和 .memory。
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory" # 隐藏文件，不提交到 git
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]

# ── Task System (from s12, synced) ──

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass # dataclass是 Python 3.7+ 的内置库，用于定义数据类。相比单纯的类，dataclass 自动生成 __init__ 和 __repr__ 方法，用于初始化和打印。
class Task:
    """沿用 s12 的持久化任务记录；后台执行结束不会自动把它标记 completed。"""
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    """任务 ID 只能是文件名，不能借助路径或符号链接访问任务目录之外。"""
    if (not isinstance(task_id, str) or not task_id # 如果 task_id 不是字符串，或者 task_id 是空字符串，或者 task_id 包含路径分隔符
            or Path(task_id).name != task_id or "\\" in task_id):
        raise ValueError("task_id must be a file name, not a path")
    path = (TASKS_DIR / f"{task_id}.json").resolve()
    if not path.is_relative_to(TASKS_DIR.resolve()):
        raise ValueError("Task path escapes .tasks directory")
    return path


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建工作记录并落盘，不会执行 description 中描述的工作。"""
    if not isinstance(subject, str) or not isinstance(description, str):
        raise ValueError("subject and description must be strings")
    if blockedBy is not None and ( # 如果 blockedBy 不是 None，并且 blockedBy 不是列表，或者 blockedBy 列表中存在不是字符串的元素
        not isinstance(blockedBy, list) or any(not isinstance(dep, str) for dep in blockedBy)
    ):
        raise ValueError("blockedBy must be a list of task IDs")
    for dep_id in blockedBy or []: # 对于 blockedBy 列表中的每一个元素，调用 _task_path 函数
        _task_path(dep_id)
    # 时间戳 + 随机数用于演示，不提供严格的唯一性保障；依赖环也没有自动检测。
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    """把 Task 存进文件；关掉程序再打开，还能读取这份工作记录。"""
    # 一步一步看：Task 对象 -> 普通字典 -> JSON 文本 -> 写入文件。
    # 将对象变成方便保存的文本，通常叫“序列化”。它不等于执行这个任务。
    task_data = asdict(task)
    task_json = json.dumps(task_data, indent=2, ensure_ascii=False)
    task_file = _task_path(task.id)
    # 这里没有防止两个程序同时写同一个文件，也没有防止写到一半程序突然退出。
    # 后面的 background_lock 只管后台任务的内存字典，不会自动保护这个文件。
    task_file.write_text(task_json, encoding="utf-8")


def load_task(task_id: str) -> Task:
    """反过来：读取文件中的 JSON 文本，重新得到一个 Task 对象。"""
    task_file = _task_path(task_id)
    task_json = task_file.read_text(encoding="utf-8")
    task_data = json.loads(task_json)
    # **task_data 把字典里的键值分别当参数传入，如 Task(id="...", subject="...", ...)。
    return Task(**task_data)


def list_tasks() -> list[Task]:
    return [load_task(p.stem)
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """读取完整任务字段，包括概览里没有展示的 description。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


def can_start(task_id: str) -> bool:
    """上游全部 completed 才允许认领；缺失的依赖也视为阻塞。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """把未开始的任务改为进行中，并记下由谁负责。"""
    # 程序先读文件，再检查状态，最后才把新状态写回文件，中间有好几步。
    # 如果两个独立 Agent 进程同时读取到 pending，就可能都认领成功。
    # 每个程序有自己的 background_lock，它们不会互相等待；这里没有处理这种情况。
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"] # deps 是一个列表，包含所有阻塞当前任务的依赖任务 ID，这些依赖任务要么不存在，要么状态不是 completed。
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """in_progress -> completed，只修改状态，不自动验收成果或启动下游任务。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    # 扫描所有当前可启动的有依赖任务，不仅限于本次刚被解锁的下游。
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
    """沿用 s10 的运行时组装，工具与目录使用 context 的实际值。"""
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
    """只缓存本地拼接的字符串，不缓存回答；每次 API 请求仍会携带 system。"""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    """限制文件工具的路径；这不会给 bash 子进程建立操作系统沙箱。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    """运行一条命令，等它结束，再返回它打印的文字。"""
    # 例如 command="pytest"。到 subprocess.run 这一行后，当前线程会停下来等测试。
    # 测试没结束，就不会执行下面的 output = ...；但其他线程可以继续做自己的事。
    # 所以，是否阻塞主循环，取决于“哪个线程调用了本函数”，不是函数名或参数名。
    # run_in_background 在这里不做判断：agent_loop 已经决定由哪个线程调用它。
    try:
        # subprocess 请操作系统启动另一个程序，不是在现有终端窗口里模拟打字。
        # shell 是读懂命令字符串并执行它的程序；例如它能理解 | 和 > 这些命令符号。
        completed_process = subprocess.run(
            command,
            shell=True,           # 把 command 字符串交给 shell 解释。
            cwd=WORKDIR,          # 从项目目录开始运行，不是限制只能访问项目目录。
            capture_output=True,  # 收集命令输出，留给 Python 处理，不直接全部打印在屏幕上。
            text=True,            # 收到的输出使用字符串，而不是 bytes 字节数据。
            timeout=120,          # 等待超时会报错；放到后台也仍然有这个限制。
        )

        # 能走到这里，说明 subprocess.run 已经返回。
        # stdout 是普通输出，stderr 是错误/诊断输出。它们是返回对象的两个属性。
        # 例如 stdout="3 passed\n"，stderr=""，合起来再去掉两端空白，就是 "3 passed"。
        output = completed_process.stdout + completed_process.stderr
        output = output.strip()
        # 两路输出这样拼接不保留原本交错出现的顺序。[:50000] 只截短要返回的文字，
        # 不是限制命令最多打印多少：上面的 run 已经把输出收集进内存了。
        if completed_process.returncode != 0:
            # returncode 是命令结束时交给操作系统的数字，0 通常表示成功，非 0 表示失败。
            # 不能只看有没有 stderr，或者输出里是否含有 "Error" 来判断成功与否。
            raise RuntimeError(
                f"Command exited with code {completed_process.returncode}:\n{output[:50000]}"
            )
        if output:
            return output[:50000]
        return "(no output)"
    except subprocess.TimeoutExpired as error:
        # raise 表示本函数没有正常完成；调用它的那一层要用 try/except 接住这个错误。
        # 本章没有实现对 shell 启动的所有后续子进程一起停止的完整管理。
        raise TimeoutError("Timeout (120s)") from error


def run_read(path: str, limit: int | None = None) -> str:
    """同步读取文件，limit 可限制返回行数。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """同步创建父目录并覆盖写入；这才是实际文件操作，不是 Task 状态更新。"""
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
    """把内部 Task 对象转成模型可读的文本，返回的 task_id 可用于后续认领。"""
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """只列 s12 的落盘任务，不是 background_tasks 中的后台进程列表。"""
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
    # 本章仍是单 Agent demo，owner 固定为 agent。
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# Schema 是“工具参数的格式说明”，供模型生成调用请求，不是执行工具的代码。
# 模型生成工具名和参数后，本地 Python 仍要解析、校验，再交给 TOOL_HANDLERS 执行。
# run_in_background 只是一个参数；真正创建线程的是下面的 start_background_task。
TOOLS = [
    # 与前几章一样使用 OpenAI function schema，只有 bash 多了后台执行选项。
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object",
                       "properties": {
                           "command": {"type": "string"},
                           "run_in_background": {
                               "type": "boolean",
                               "description": "True runs in background; false forces synchronous execution. "
                                              "When omitted, a slow-command heuristic is used.",
                           }},
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
                           "blockedBy": {"type": "array", "items": {"type": "string"}}},
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


# ── Background Tasks (s13 new) ──

# 这三个变量写在函数外面，因此主线程和后台线程用的是同一份。
# _bg_counter 只用来发编号：第一次加到 1，得到 bg_0001；第二次得到 bg_0002。
_bg_counter = 0

# 假设第一次启动 pytest，稍后这两个字典会变成：
# background_tasks = {
#     "bg_0001": {"tool_call_id": "call_A", "command": "pytest", "status": "running"}
# }
# background_results = {}  # 还没有结果，所以暂时没有 bg_0001 这一项。
#
# pytest 成功结束后，后台线程会把 status 改成 "completed"，并写入：
# background_results["bg_0001"] = "3 passed"
# 主线程看到 completed，就知道可以取结果了。失败时状态改为 failed，也会有错误文字。
# 这些数据没有写进文件，退出程序就丢失；它们也不会自动出现在模型的上下文里。
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}

# 为什么需要这把锁？先看一个错误写法可能出现的顺序：
# 后台线程：把状态改为 completed。
# 主线程：  恰好开始收集，看到 completed，就取走任务，但此时输出还没写好。
# 后台线程：这才准备写输出，已经晚了。
#
# 为了不让“更新到一半”和“开始读取”碰在一起，我们约定：
# 读写这两个字典之前，都先获取下面创建的同一把锁；做完之后再释放。
# 当前实现还会先写输出再改状态。不是说任意一行去掉锁就一定报错，而是让整组操作
# 有明确的先后关系，不依赖线程刚好在什么时候切换。
#
# Python 中把这个约定写成 with background_lock:，后面跟缩进的代码：
# 1. 走到 with 时，先尝试拿锁（acquire）。没人拿着，就能继续执行缩进内的代码。
# 2. 别的线程已拿着锁，就在 with 这一行等；等它释放后，才有机会继续。
# 3. 走出缩进范围，自动放回锁（release）。即使中间抛异常也会放回。
# 注意：自动放回锁不等于自动撤销已经改过的数据。
#
# 多处 with 用的都是这个 background_lock，不是每处各用一把锁。
# 它不会暂停其他线程的所有工作，只让同样来拿这把锁的代码等待。
# 例如后台运行 pytest 不拿锁，所以主线程仍能拿锁查看任务状态。
# 反过来，谁绕过 with 直接改字典，锁就管不到谁；锁没有自动绑定到字典上。
#
# 这里的专业名称是“线程互斥锁”：不同线程不能同时进入拿着同一把锁的代码段。
# 它不是“进程锁”，不阻止两个 shell 命令同时执行，也不保护磁盘文件。
# 写代码时还要避免自己等自己：拿着这把普通 Lock，再拿一次同一把锁，就会卡住。
# 拿着锁等后台结束，而后台又必须拿到锁才能结束，也会双方一直等待，这叫“死锁”。
# 所以下面不嵌套获取同一把锁，也不在拿着锁的时候等命令或线程结束。
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """看看命令有没有 install、test 等字样，粗略猜测它是否比较慢。"""
    if tool_name != "bash":
        return False
    command = tool_input.get("command", "").lower()
    slow_keywords = [
        "install", "build", "test", "deploy", "compile",
        "docker build", "pip install", "npm install",
        "cargo build", "pytest", "make",
    ]
    # 用普通循环逐个检查。例如 command="pytest"，检查到 "test" 时就返回 True。
    # 这里只看文字，不会先执行命令测时间；所以连 "echo test" 也可能被误认为慢操作。
    for keyword in slow_keywords:
        if keyword in command:
            return True
    return False


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """先看模型有没有明确要求；没说才根据命令文字猜。"""
    # 本章只有 bash 可以交给后台，读文件、写文件和 Task 工具仍由主线程执行。
    if tool_name != "bash":
        return False
    if not isinstance(tool_input.get("command"), str):
        raise ValueError("bash.command must be a string")

    # 这三个例子要分开处理：
    # {"command": "pytest", "run_in_background": True}  -> 放后台。
    # {"command": "pytest", "run_in_background": False} -> 明确要求等结果，不放后台。
    # {"command": "pytest"}                            -> 没说，让下面的函数猜。
    # 所以这里检查“有没有这个键”，不是检查“这个值是不是 True”。
    if "run_in_background" in tool_input:
        requested_background = tool_input["run_in_background"]
        if not isinstance(requested_background, bool):
            raise ValueError("run_in_background must be a boolean")
        return requested_background
    return is_slow_operation(tool_name, tool_input)


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """把工具名换成真正的 Python 函数，然后调用这个函数。"""
    # TOOL_HANDLERS["bash"] 保存的是 run_bash 函数本身，不是运行结果。
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"

    # 假设 tool_name="bash"、tool_input={"command": "pytest", "run_in_background": True}，
    # 下一行相当于 run_bash(command="pytest", run_in_background=True)。
    # ** 的作用只是把字典里的键值变成函数参数，不会自动创建线程。
    result = handler(**tool_input)
    return str(result)


def start_background_task(tool_call_id: str, tool_name: str, tool_input: dict) -> str:
    """由主线程调用：记下任务，安排后台执行，然后把编号返回给主线程。"""
    if tool_name != "bash":
        raise ValueError("Only bash can run in the background")

    # 第 1 步：准备这次命令的参数，尚未执行 pytest。
    # global 告诉 Python：下面的 += 1 修改的是函数外面的计数器，而不是新建同名变量。
    global _bg_counter
    # 复制一份参数，避免调用者稍后修改原字典，让后台拿到的 command 跟着变掉。
    # 这里的值只有字符串和布尔值，copy 就够了；嵌套列表/字典则不会被它一起复制。
    worker_input = tool_input.copy()
    command = worker_input["command"]

    # 第 2 步：先登记，再启动线程。否则后台命令很快结束时，可能还找不到自己的记录。
    # 现在只有主线程会启动任务；仍把编号和登记一起放在锁内，保持统一的读写约定。
    with background_lock:
        _bg_counter += 1
        # :04d 把数字补成 4 位，例如 1 -> "0001"，所以第一个编号是 "bg_0001"。
        bg_id = f"bg_{_bg_counter:04d}"
        background_tasks[bg_id] = {
            "tool_call_id": tool_call_id,  # 如 call_A：模型这一条工具调用的编号。
            "command": command,           # 如 pytest：实际要运行的命令。
            "status": "running",         # 已安排执行，还没有交回结果。
        }
    # 缩进已经退回到 with 外面：锁在这里已释放，不会一直拿到 pytest 结束。
    # bg_0001 是本地后台执行编号，不是 s12 的 task_... 工作记录编号，两者不自动关联。

    # 第 3 步：告诉 Thread“另外开一条执行路线，替我调用下面这个函数”。
    try:
        # target 放函数名，不带括号；args 按顺序提供这个函数的三个参数。
        # 它们合起来表示：稍后在后台执行 run_background_worker(bg_id, tool_name, worker_input)。
        # 如果直接写 run_background_worker(...)，就会现在在主线程执行，没有后台效果。
        thread = threading.Thread(
            target=run_background_worker,
            args=(bg_id, tool_name, worker_input),
            daemon=True,
        )
        # 上面只是准备好线程对象；这一行才真正启动后台执行。
        # start 不等函数运行完。主线程接着往下走，后台线程从 run_background_worker 开始走。
        thread.start()
    except Exception:
        # 连线程都没启动成功，就删掉刚才登记的 running，避免它永远停在那里。
        # pop(key, None) 是取出并删除；如果 key 不在字典里就返回 None，不再报另一个错。
        with background_lock:
            background_tasks.pop(bg_id, None)
        # 不带内容的 raise 把刚才那个错误继续交给调用者 agent_loop 处理。
        raise

    # 第 4 步：把编号交回 agent_loop，不是把 pytest 的结果交回去。
    # 不调用 thread.join()，因为 join 的意思正是“在这里等后台线程结束”。
    # 很短的命令可能已经完成，也可能还没开始执行；这里不靠猜时间判断完成与否。
    # daemon=True 表示退出程序时不必等这个线程。它不是“退出后仍可靠地继续运行”，
    # 也不保证 shell 和它启动的所有程序都会被清理；本章没有停止任务/重启恢复功能。
    print(f"  \033[33m[background] dispatched {bg_id}: {command[:40]}\033[0m")
    return bg_id


def run_background_worker(bg_id: str, tool_name: str, tool_input: dict):
    """由后台线程执行：运行命令，等结束，然后把结果写到主线程也能看到的字典里。"""
    # 为什么放成单独的函数？可以清楚看出：start_background_task 是主线程做的事，
    # 本函数是后台线程做的事。函数名本身没有魔法，是 Thread.start 让它在后台运行。
    # 定义函数的 def 不会执行函数体；程序读完这些定义，才从文件底部开始处理用户输入。
    try:
        # 第 1 步：真的执行命令。沿着调用往下看：
        # execute_tool("bash", ...) -> run_bash(...) -> subprocess.run("pytest", ...)。
        # 假设测试要 60 秒，这个线程就等 60 秒；主线程不必等，可以继续读配置文件。
        # 这一段没有拿 background_lock，否则主线程检查任务时也要陪着等 60 秒。
        output = execute_tool(tool_name, tool_input)
        status = "completed"
    except Exception as error:
        # 主线程此时可能正在做别的事。这里报错，不会让它那边的 try/except 自动接住。
        # 所以我们自己接住，把“失败”也当作一种结果交回去，而不是丢下 running 不管。
        output = f"Error: {type(error).__name__}: {error}"
        status = "failed"

    # 第 2 步：命令已经结束，才拿锁写结果。output/status 只是本次函数调用的变量，
    # 主线程不会去读这些局部变量；它会读下面两个全局字典。
    with background_lock:
        # 例：先写 background_results["bg_0001"] = "3 passed"。
        background_results[bg_id] = output
        # 再把 background_tasks["bg_0001"]["status"] 从 running 改成 completed。
        background_tasks[bg_id]["status"] = status
    # 两句都写完，才释放锁。主线程若刚好来拿同一把锁，只需等这两句写完。
    # 本函数随后结束，后台线程也结束；不是永久在后台循环等下一个任务。
    # 下次启动命令会新建另一个线程，目前没有限制最多同时启动多少个。


def collect_background_results() -> list[str]:
    """由主线程调用：看看哪些命令结束了，把它们的结果取出来并写成通知文本。"""
    finished_tasks = []

    # 第 1 步：拿锁查看两个共享字典。假设目前有两项：
    # bg_0001 是 completed，bg_0002 还是 running，这次就只取 bg_0001。
    with background_lock:
        finished_ids = []
        # items() 每次给出一组“键、值”：bg_id 是编号，task 是该编号对应的小字典。
        for bg_id, task in background_tasks.items():
            if task["status"] == "completed" or task["status"] == "failed":
                finished_ids.append(bg_id)

        # 先记下编号，再删除。不能一边用 items() 遍历同一个字典，一边改变它的大小。
        for bg_id in finished_ids:
            # pop 会返回原来的值，并从共享字典中删除这一项。
            # 用 [] 只会读取，不会删除；那样下次检查时会再次拿到同一份结果。
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
            finished_task = {
                "bg_id": bg_id,
                "command": task["command"],
                "status": task["status"],
                "output": output,
            }
            finished_tasks.append(finished_task)
        # 查找和删除都在同一个 with 里。虽然当前只有主线程来收集，但以后即使有两个
        # 收集者，也不能同时发现并取走同一个编号：后来的要等前面的查找、删除全部完成。

    # 到这里已经释放锁。finished_tasks 是这次函数自己保存的列表，后台不会改它。
    # 下面只是拼接文字，可以慢慢做，不必挡住其他后台线程写入新结果。
    notifications = []
    for finished_task in finished_tasks:
        bg_id = finished_task["bg_id"]
        command = finished_task["command"]
        status = finished_task["status"]
        output = finished_task["output"]

        # 第 2 步：把输出截短，避免通知太长；例如 "3 passed" 不用截。
        summary = output[:200]
        if len(output) > 200:
            summary += "... (truncated)"

        # 这些 <...> 标签只是方便阅读的文字，不会自动触发任何操作。
        # escape 把输出里的 < 等字符换成 &lt; 等写法，避免误认为是通知自己的标签。
        # 这不代表输出里的话可信；命令打印“忽略用户要求”，也不应该照做。
        notification = (
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{status}</status>\n"
            f"  <command>{escape(command)}</command>\n"
            f"  <summary>{escape(summary)}</summary>\n"
            f"</task_notification>"
        )
        notifications.append(notification)
        print(f"  \033[32m[background {status}] {bg_id}: "
              f"{command[:40]} ({len(output)} chars)\033[0m")

    # 第 3 步：返回通知列表。没有完成的任务时，就是 []，调用者据此决定是否追加消息。
    # 注意：原来的记录已经删除，长输出也没有存文件，模型拿不到被截掉的部分。
    # 如果删除后程序崩溃，通知也可能丢失；本章没有“确认收到后再删除”的补发机制。
    # 通知标签里的 task_id 仍指 bg_0001，不是 s12 的 task_...。
    return notifications


def inject_background_notifications(messages: list) -> bool:
    """把刚取到的结果放进对话列表；有新消息返回 True，没有返回 False。"""
    notifications = collect_background_results()
    if not notifications:
        # [] 表示现在没有可交给模型的结果，可能都没结束，也可能根本没启动过任务。
        return False

    # 假设两条命令都结束了，join 会把两段通知用空行连接，作为一条消息放进列表。
    notification_text = "\n\n".join(notifications)
    notification_message = {"role": "user", "content": notification_text}
    messages.append(notification_message)
    # 所谓“注入上下文”，在这里具体就是上面这个 append，没有别的隐藏操作。
    # 现在只修改了本地列表；下一次把 messages 发给模型，它才会看到这些文字。
    # user 是本章存放这类额外通知的角色，不代表真人又输入了一句话，更不是新的授权。
    #
    # 为什么不再发一条 tool_call_id="call_A" 的 tool 消息？
    # 因为 call_A 已经收到过“bg_0001 已启动”的回复，一次调用的结果已经配好。
    # 后来的完成消息另起一条，不拿同一个编号再回复一次。
    # 若模型一次请求工具 A、B，要先回复完 A、B，再追加通知，不能插在 A 和 B 的回复中间。
    print(f"  \033[32m[inject] {len(notifications)} background notification(s)\033[0m")
    return True


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """重新读取本轮 system 提示词需要的信息。"""
    # 只读 MEMORY.md 这份索引，不会顺便读它提到的所有文件。
    # context/messages 参数沿用前一章的函数形式，本函数目前没有用它们的旧内容。
    # 不要以为“定义了一个字典，模型就知道里面的值”：
    # Task 记录要用任务工具读，后台结果要用上面的通知函数加入 messages。
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


# ── Agent Loop (simplified, focused on background tasks) ──

def append_assistant(messages: list, message: dict):
    """把模型回复记进历史，并把其中的文字显示在终端。"""
    messages.append(message)
    if message.get("content"):
        print(message["content"])


def agent_loop(messages: list, context: dict):
    """一直由主线程运行：问模型下一步做什么，执行工具，再把消息发回去。"""
    # messages 就是文件底部的 history，同一个列表传进函数后换了一个参数名。
    # append 会修改原列表，所以本函数 return 后，history 里仍保留新增的消息。
    # 后台线程不碰这个列表；它只改后台状态字典。因此这里不需要给 messages 另加锁。
    #
    # 注意两个不同的“循环”：
    # 文件底部的循环：等用户输入一个问题。
    # 这里的 while：为这一个问题反复请求模型、处理工具，不必让用户每一步都重新输入。
    while True:
        # 第 1 步：先查看后台有没有新结果。有就加进 messages，没有就什么都不加。
        # 后台线程不会自动跳过来执行这里；必须主线程自己走到这一行才会检查。
        # 这种“每次走到某个位置就主动查一下”的办法，叫轮询。
        inject_background_notifications(messages)

        # 第 2 步：准备本次要发给模型的消息。
        # messages 是历史列表。request_messages 是临时新列表：最前面加一条 system，
        # 再把历史逐条放在后面。这样不会每轮都向 history 重复塞入 system。
        context = update_context(context, messages)
        system = get_system_prompt(context)
        request_messages = [{"role": "system", "content": system}]
        request_messages.extend(messages)
        try:
            # 走到这里，才真正通过网络把消息交给模型。主线程在这里等回复，
            # 但没有拿 background_lock，后台命令仍能执行，结束后也能写入结果。
            # 如果后台此时才完成，结果不在已经发出的请求里，要等后面再次检查、再次发送。
            response = client.chat.completions.create(
                model=MODEL,
                messages=request_messages,
                tools=TOOLS,
                max_tokens=8000,
            )
        except Exception as error:
            # SDK 自己可能会先重试某些网络错误；最终仍抛到这里，就记录并结束本次回答。
            # 这里没有另写 s11 那样的错误恢复循环。
            error_message = {
                "role": "assistant",
                "content": f"[Error] {type(error).__name__}: {error}",
            }
            append_assistant(messages, error_message)
            return

        # 第 3 步：读模型回复。choices 是候选回复列表，这里使用第一条。
        choice = response.choices[0]
        assistant_message = choice.message
        if choice.finish_reason == "length":
            # length 表示回复被长度限制截断，工具参数可能只写了一半，不能拿去运行。
            # 只保留已有文字并说明错误，不保存这批没有执行的 tool_calls。
            partial_text = assistant_message.content or ""
            error_message = {
                "role": "assistant",
                "content": partial_text +
                    "\n[Error] Output truncated; tool calls from this response were not executed.",
            }
            append_assistant(messages, error_message)
            return

        # SDK 给我们的是一个对象；model_dump 把它变成能放进历史的普通字典。
        # 这里要把 tool_calls 一起保存，否则后面只回复工具结果，模型看不到对应的请求。
        assistant_record = assistant_message.model_dump(exclude_none=True)
        append_assistant(messages, assistant_record)
        if not assistant_message.tool_calls:
            # 模型没有要求新工具，一般说明这轮回答可以结束了。
            # 但刚才等模型回复时，pytest 可能已经完成，所以结束前再检查一次。
            has_new_results = inject_background_notifications(messages)
            if has_new_results:
                # continue 回到 while 顶部，再请求一次模型；这次它能看到刚追加的结果。
                continue
            # return 离开整个 agent_loop，回到文件底部等待用户输入。
            # 即使还有命令没结束，也不反复问模型“好了没有”。等待本身不产生模型 token，
            # 没必要为了等待而不停发请求；晚到的结果留到用户下次提问时再收集。
            return

        # 第 4 步：模型要求调用工具，主线程就按列表顺序逐个处理。
        # 例如先 bash(pytest)，再 read_file(config)：前者启动后台就返回，后者随后执行。
        # 如果两项工作必须先后完成（如安装结束才能构建），就不能只凭“已启动”往下做。
        # 本章没有自动检查这种依赖，也没有把 s12 的 blockedBy 接到后台执行上。
        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            print(f"\033[36m> {tool_name}\033[0m")
            try:
                # 模型给的 arguments 最初是一段字符串，不是 Python 字典，例如：
                # '{"command": "pytest", "run_in_background": true}'。
                # loads 把文字读成字典，并把 JSON 的 true 读成 Python 的 True。
                arguments_json = tool_call.function.arguments or "{}"
                tool_input = json.loads(arguments_json)
                if not isinstance(tool_input, dict):
                    raise ValueError("tool arguments must be a JSON object")

                run_in_background = should_run_background(tool_name, tool_input)
                if run_in_background:
                    # 这里只启动，不等 pytest 结束。bg_id 是 "bg_0001" 这样的字符串。
                    bg_id = start_background_task(tool_call.id, tool_name, tool_input)
                    # 给模型的这条回复是“已经安排”，不是“测试通过”；实际结果稍后用通知传。
                    output = (f"[Background task {bg_id} started] "
                              f"Command: {tool_input['command']}. "
                              "Result will be available when complete.")
                else:
                    # 不放后台就由主线程直接执行，必须等这个函数返回才能处理下一个工具。
                    output = execute_tool(tool_name, tool_input)
            except Exception as error:
                # 参数写错、线程启动失败或前台工具报错，都把错误文字交回模型。
                # 已经启动的后台线程报错则由 run_background_worker 处理，不会跑到这里来。
                output = f"Error: {type(error).__name__}: {error}"
            print(str(output)[:300])

            # 第 5 步：每个工具调用都要有一条对应回复，tool_call_id 用模型给的原编号。
            # 这里不能把 call_A 换成 bg_0001，否则模型对不上“回复的是哪一次调用”。
            tool_result_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            }
            messages.append(tool_result_message)
        # for 结束后，没有 return，所以继续下一轮 while。
        # 即使 pytest 早就完成，也等这一批 tool 回复全放好，再在 while 顶部加完成通知。


if __name__ == "__main__":
    # 从终端直接运行本文件时，Python 先完成上面的变量赋值和函数定义，再进入这里。
    # def 只是定义函数，不会让函数体从上到下都运行一遍；被调用时才运行。
    # 测试代码 import 本文件时，__name__ 不是 "__main__"，因此不会停在 input 等输入。
    print("s13: background tasks")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # history 保存这一回运行期间的聊天。关掉程序，它和后台字典都会丢失。
    # .tasks 下的 JSON 文件则还在磁盘上，所以工作记录可以下次继续读。
    history = []
    context = update_context({}, [])
    while True:
        try:
            # 主线程在这里等用户按回车。后台可以继续执行，结束后也能把结果写进字典。
            # 但写字典不会替用户按回车，所以它不会自动让主线程跳到 agent_loop。
            # 这也是为什么本章的“后台完成”不一定立即出现在聊天里。
            query = input("\033[36ms13 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        # 文本已由 append_assistant 打印，不再解析 Anthropic 内容块。
        print()

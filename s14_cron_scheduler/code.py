#!/usr/bin/env python3
"""
s14：给 Agent 加上“到点发起工作”的机制。

运行：python s14_cron_scheduler/code.py
安装：pip install openai python-dotenv
配置：在 .env 中填写 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL_ID。
与 s01-s13 一样，通过 OpenAI 兼容接口调用配置的模型，例如 DeepSeek。

s13：现在启动慢命令，让它在后台执行，不挡住 Agent 后续工作。
s14：先记下规则，未来时间到了，即使用户没有再次输入，也能发起一轮 Agent。

贯穿本章的例子：“每天 9 点检查项目的测试结果”。
1. schedule_job：保存“每天 9 点”和“检查测试”这条规则。
2. check_scheduled_jobs：检查当前时间，到点就把请求放进 cron_queue。
3. process_queued_jobs：有请求且 Agent 空闲时，调用 agent_loop。
4. agent_loop：把请求加进聊天历史，再问模型具体要调用哪些工具。

建议配合本目录的 README_DETAIL.md 阅读。先看上面四个函数，再补时间匹配和锁。
本章是单进程教学实现：不保证关机运行、漏跑补偿、可靠重试或严格只执行一次。
"""

import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from xml.sax.saxutils import escape

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# SDK 是发送请求的客户端，不是模型本身；实际服务地址、密钥和模型名由 .env 决定。
# override=True 让 .env 中的同名值覆盖已有环境变量。不要把真实密钥写在源码里。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

# cwd 是启动程序时所在的目录，不一定是 code.py 所在目录。
# 从项目根目录启动，.tasks、.memory 和 .scheduled_tasks.json 就都位于根目录。
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]

# ── Task System (from s12, synced) ──

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    """s12 的工作清单记录，不是后台命令，也不是后面的 CronJob 定时规则。"""
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    # 与前章一样，只允许任务编号，不能利用 ../ 跑到任务目录外面。
    if (not isinstance(task_id, str) or not task_id
            or Path(task_id).name != task_id or "\\" in task_id):
        raise ValueError("task_id must be a file name, not a path")
    path = (TASKS_DIR / f"{task_id}.json").resolve()
    if not path.is_relative_to(TASKS_DIR.resolve()):
        raise ValueError("Task path escapes .tasks directory")
    return path


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建工作记录，不会自动执行 description 中描述的事情。"""
    if not isinstance(subject, str) or not isinstance(description, str):
        raise ValueError("subject and description must be strings")
    if blockedBy is not None and (
        not isinstance(blockedBy, list) or any(not isinstance(dep, str) for dep in blockedBy)
    ):
        raise ValueError("blockedBy must be a list of task IDs")
    for dep_id in blockedBy or []:
        _task_path(dep_id)
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    # Task 对象 -> 字典 -> JSON 文本 -> 文件。这叫序列化和持久化，不是执行任务。
    data = asdict(task)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    _task_path(task.id).write_text(text, encoding="utf-8")


def load_task(task_id: str) -> Task:
    data = json.loads(_task_path(task_id).read_text(encoding="utf-8"))
    # **data 把字典的每个键值展开成参数，如 Task(id="...", subject="...", ...)。
    return Task(**data)


def list_tasks() -> list[Task]:
    return [load_task(p.stem) for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """读取一条工作记录的完整内容，包括列表概览里没有的 description。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """只有 blockedBy 中的上游工作全部完成，才允许认领；找不到上游也算被阻塞。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领只是把工作记录改成 in_progress，不会在这里启动线程或运行命令。"""
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
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """由工具调用明确标记完成；定时规则触发、后台命令结束都不会自动调用它。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
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
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    """把身份、工具、目录和可选记忆拼成模型每轮收到的 system 提示词。"""
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    # 这里只缓存拼好的字符串，不缓存模型回答，也不是服务商的 prompt cache。
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    # resolve 整理 ../ 和符号链接；is_relative_to 检查结果是否仍在工作目录内。
    # 这只约束下面的文件工具，不会自动限制 bash 命令的权限。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    # “放后台”由 agent_loop 决定；本函数无论在哪个线程运行，都会等命令结束。
    # subprocess 请操作系统启动子进程。shell=True 表示把 command 交给 shell 解释。
    # cwd 只选初始目录，不是安全沙箱。不要用这个教学脚本运行不可信命令。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # stdout 是普通输出，stderr 是错误输出；returncode 为 0 通常表示成功。
        if r.returncode != 0:
            raise RuntimeError(f"Exit code {r.returncode}: {(r.stdout + r.stderr).strip()[:50000]}")
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Timeout (120s)") from error


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
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task tools

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
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
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ── Background Tasks (from s13, synced) ──

# 沿用 s13：这里只管“已经开始的命令”，不管“未来什么时候开始”。
# 后台线程和 Agent 所在线程共享这些字典，写入/取出时使用同一把锁。
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """模型没说是否后台运行时，根据命令里的关键词粗略猜；并不是真的测量耗时。"""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    for keyword in slow_keywords:
        if keyword in cmd:
            return True
    return False


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """只有 bash 能放后台。显式 False 表示必须等结果，不能再被关键词猜测覆盖。"""
    if tool_name != "bash":
        return False
    if not isinstance(tool_input.get("command"), str):
        raise ValueError("bash.command must be a string")
    if "run_in_background" in tool_input:
        requested = tool_input["run_in_background"]
        if not isinstance(requested, bool):
            raise ValueError("run_in_background must be a boolean")
        return requested
    return is_slow_operation(tool_name, tool_input)


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """模型只提供工具名和参数，真正执行的是本地 Python 函数。"""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"
    # 如 handler=run_read、tool_input={"path": "README.md"}，
    # handler(**tool_input) 就相当于 run_read(path="README.md")。
    return str(handler(**tool_input))


def start_background_task(tool_call_id: str, tool_name: str, tool_input: dict) -> str:
    """由当前运行 Agent 的线程登记后台命令，然后马上返回编号，不等执行结束。"""
    global _bg_counter
    if tool_name != "bash":
        raise ValueError("Only bash can run in the background")
    worker_input = tool_input.copy()
    # 先登记再启动，否则极快的命令可能结束时还找不到自己的记录。
    with background_lock:
        _bg_counter += 1
        bg_id = f"bg_{_bg_counter:04d}"
        background_tasks[bg_id] = {
            "tool_call_id": tool_call_id,
            "command": worker_input["command"],
            "status": "running",
        }
    try:
        # target 是函数本身，不加括号；args 是交给这个函数的参数。
        # Thread(...) 准备线程，start() 才让另一个线程开始调用该函数。
        thread = threading.Thread(
            target=run_background_worker,
            args=(bg_id, tool_name, worker_input),
            daemon=True,
        )
        thread.start()
    except Exception:
        with background_lock:
            background_tasks.pop(bg_id, None)
        raise
    # 不 join：join 会在这里等线程结束，又回到前台等待的效果。
    print(f"  \033[33m[background] dispatched {bg_id}: {worker_input['command'][:40]}\033[0m")
    return bg_id


def run_background_worker(bg_id: str, tool_name: str, tool_input: dict):
    """由后台工作线程执行：等命令结束，把成功或失败都写成结果。"""
    try:
        # 运行慢命令时不持有锁，否则别的线程连查看结果字典也要陪着等。
        output = execute_tool(tool_name, tool_input)
        status = "completed"
    except Exception as error:
        # 后台线程里的异常不会自动传到启动它的线程，必须在这里处理。
        output = f"Error: {type(error).__name__}: {error}"
        status = "failed"
    with background_lock:
        background_results[bg_id] = output
        background_tasks[bg_id]["status"] = status


def collect_background_results() -> list[str]:
    """拿走完成的记录，做成可以放进聊天历史的通知文字。"""
    finished = []
    with background_lock:
        ready_ids = []
        for bg_id, task in background_tasks.items():
            if task["status"] in ("completed", "failed"):
                ready_ids.append(bg_id)
        # 查找和删除是一组操作，不能中途放开锁让另一个收集者也取走同一条。
        for bg_id in ready_ids:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
            finished.append((bg_id, task, output))

    notifications = []
    for bg_id, task, output in finished:
        summary = output[:200]
        if len(output) > 200:
            summary += "... (truncated)"
        # 标签只是文字，不会自动执行操作；escape 防止输出中的 < 等字符破坏标签结构。
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{task['status']}</status>\n"
            f"  <command>{escape(task['command'])}</command>\n"
            f"  <summary>{escape(summary)}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background {task['status']}] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    # 与 s13 一样，长输出没有另外落盘；取出后就删除，没有成功确认后的补发机制。
    return notifications


def inject_background_notifications(messages: list) -> bool:
    """通知必须进入 messages，并在下一次请求时发送，模型才看得到。"""
    notifications = collect_background_results()
    if not notifications:
        return False
    messages.append({"role": "user", "content": "\n\n".join(notifications)})
    # 原工具调用已收到“后台已启动”的 tool 回复，所以不能拿同一个 call_id 再回复一次。
    # 这里另起通知；user 只是承载额外信息，不代表真人新授权，更不代表输出内容可信。
    return True


# ── Cron Scheduler (s14 new) ──

# 文件只保存规则，不保存等待队列、上次触发时间或执行结果。
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    """一条“什么时间，把什么要求交给 Agent”的规则，不是一条已经运行的命令。"""
    # dataclass 自动生成 __init__ 等方法，方便写 CronJob(id=..., cron=..., ...)。
    # 下面的类型标注是说明，Python 不会仅凭 bool 标注自动拒绝字符串 "false"。
    id: str
    cron: str        # 时间表达式，如 "0 9 * * *"：每天 9 点。
    prompt: str      # 到点后交给模型的话，不是 Python 直接执行的 shell 命令。
    recurring: bool  # True：下次还触发；False：第一次入队后就删除规则。
    durable: bool    # True：把规则存文件；False：只在本次程序运行期间保留。


# 三份数据不要混在一起理解：
# scheduled_jobs：规则册，例如“每天 9 点检查测试”。
# cron_queue：今天这次已经到点了，但还没交给模型的请求。
# _last_fired：某条规则最近在哪一分钟入队过，避免同一分钟入队几十次。
scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
_last_fired: dict[str, str] = {}

# cron_queue 是普通 list，不是自带同步功能的 queue.Queue。
# 我们约定读写规则、队列、去重记录时拿同一个 cron_lock，让整组操作互不打断。
# with cron_lock 会拿锁，退出缩进块时自动释放；锁只管同样遵守拿锁约定的代码。
cron_lock = threading.Lock()

# 另一把锁保护的是“运行一轮 Agent”这件事：
# 用户请求和定时请求都使用 session_history，不能同时向里面追加两组工具消息。
# agent_lock 可以一直持有到模型和工具处理完；cron_lock 则只短暂保护共享数据。
# 拿着 agent_lock 时不必拿着 cron_lock，因此等模型回复时，调度器仍能入队。
# 这两把都是线程锁，只在当前 Python 进程内有效，不能协调另一个独立程序。
agent_lock = threading.Lock()


def _cron_field_matches(field: str, value: int) -> bool:
    """只检查一栏，例如“分钟规则是 */5，当前分钟是 10”，答案就是 True。"""
    if field == "*":
        # 星号表示这一栏不限。
        return True
    if field.startswith("*/"):
        # "*/5" 去掉前两个字符得到 "5"，int 再把文本变成整数。
        # % 是取余数；10 % 5 == 0 表示 10 可以被 5 整除。
        # 本 demo 直接用取余判断。日期、月份从 1 起，步长起点不一定与其他 cron 相同。
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        # 如 "9,18"，拆开后任意一个匹配就行；这里调用自己，逐个检查小片段。
        for part in field.split(","):
            if _cron_field_matches(part.strip(), value):
                return True
        return False
    if "-" in field:
        # 如 "1-5"：1、2、3、4、5 都匹配，包含两个端点。
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """检查“某个具体时间是否符合规则”，不会在这里睡眠或执行任务。"""
    # 五栏：分钟、小时、一个月中的第几天、月份、星期几。没有秒，也没有年份。
    # DOM=day of month；DOW=day of week。dt 是调用者传来的时间，方便用固定时间测试。
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # Python 周一是 0；这里采用 cron 周日是 0、周一是 1 的编号，故要转换。
    dow_val = (dt.weekday() + 1) % 7

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    # 分钟、小时、月份必须同时匹配。
    if not (m and h and month_ok):
        return False
    # 日期、星期若只有一栏有限制，就只看那一栏。
    # 两栏都有限制时，满足任意一栏即可："0 9 1 * 1" 表示每月 1 号或每周一的 9 点。
    # 不是“只有 1 号恰好为周一才触发”。
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """检查这一栏能不能被我们的解析器理解；出错返回文字，正常返回 None。"""
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """注册前先检查格式和数字范围，不是判断此刻该不该触发。"""
    if not isinstance(cron_expr, str):
        return "cron must be a string"
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    # zip 把同一位置的三项放在一起，如 ("0", (0, 59), "minute")。
    # 这只是分栏校验，不会验证所有实际日历组合，例如“2 月 31 日”仍可能通过格式校验。
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def validate_job(cron, prompt, recurring, durable) -> str | None:
    """模型参数和磁盘记录都可能写错，所以两条入口共用相同检查。"""
    error = validate_cron(cron)
    if error:
        return error
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt must be a non-empty string"
    if not isinstance(recurring, bool) or not isinstance(durable, bool):
        return "recurring and durable must be booleans"
    return None


def save_durable_jobs():
    """只保存 durable 规则。调用者必须已经持有 cron_lock。"""
    # 本函数不再次拿锁：普通 Lock 不允许同一线程重复获取，否则会等自己释放而卡住。
    # 新增、取消、触发一次性规则时都在同一把锁内保存，避免本进程的写入互相覆盖。
    durable = []
    for job in scheduled_jobs.values():
        if job.durable:
            durable.append(asdict(job))
    text = json.dumps(durable, indent=2, ensure_ascii=False)
    DURABLE_PATH.write_text(text, encoding="utf-8")
    # 仍是简化的直接写文件：没有跨进程锁，也不保证写到一半断电时文件完整。


def load_durable_jobs():
    """启动线程前读回规则；不会补执行关闭期间错过的请求。"""
    if not DURABLE_PATH.exists():
        return
    try:
        records = json.loads(DURABLE_PATH.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("scheduled tasks file must contain a list")
    except (OSError, ValueError) as error:
        # 不静默吞掉损坏文件的错误；提示后继续启动，文件本身不在这里覆盖。
        print(f"[cron load error] {error}")
        return

    loaded = 0
    for record in records:
        try:
            job = CronJob(**record)
            error = validate_job(job.cron, job.prompt, job.recurring, job.durable)
            if error:
                raise ValueError(error)
            if not isinstance(job.id, str) or not job.id:
                raise ValueError("job id must be a non-empty string")
            if not job.durable:
                continue
        except (TypeError, ValueError) as error:
            # 一条坏记录不妨碍下一条好记录加载。
            print(f"[cron] skipping invalid job: {error}")
            continue
        with cron_lock:
            scheduled_jobs[job.id] = job
        loaded += 1
    if loaded:
        print(f"  \033[35m[cron] loaded {loaded} durable job(s)\033[0m")


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """登记规则：成功返回 CronJob，参数不合法返回错误文字。此时没有执行 prompt。"""
    err = validate_job(cron, prompt, recurring, durable)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    with cron_lock:
        # 随机编号也可能碰撞，不能把已有规则悄悄覆盖。
        while job.id in scheduled_jobs:
            job.id = f"cron_{random.randint(0, 999999):06d}"
        scheduled_jobs[job.id] = job
        if durable:
            save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job


def cancel_job(job_id: str) -> str:
    """停止这条规则未来的触发，不移除已经排队的请求，也不终止正在执行的命令。"""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
        if not job:
            return f"Job {job_id} not found"
        _last_fired.pop(job_id, None)
        if job.durable:
            save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


def check_scheduled_jobs(now: datetime | None = None):
    """只检查一次时间；正常由调度线程调用，测试可以直接传入某个固定时间。"""
    if now is None:
        now = datetime.now()
    # now 使用本地时间。规则没有秒，09:00:01 和 09:00:59 都可能匹配同一条。
    # 加上日期，既防止今天同一分钟重复入队，也允许明天 09:00 再入队。
    minute_marker = now.strftime("%Y-%m-%d %H:%M")
    with cron_lock:
        # 先复制成列表，因为后面会删除一次性规则，不能边遍历字典边改变其大小。
        jobs_to_check = list(scheduled_jobs.values())
        for job in jobs_to_check:
            try:
                if not cron_matches(job.cron, now):
                    continue
                if _last_fired.get(job.id) == minute_marker:
                    continue

                # 这里只入队，不调用模型，也不执行 shell。队列处理器稍后负责交付。
                cron_queue.append(job)
                _last_fired[job.id] = minute_marker
                print(f"  \033[35m[cron fire] {job.id} → {job.prompt[:40]}\033[0m")

                if not job.recurring:
                    # 一次性指“只入队一次”，不是“成功执行一次”。
                    # 如果入队后程序崩溃，队列丢失，已删除的规则不会自动恢复。
                    scheduled_jobs.pop(job.id, None)
                    _last_fired.pop(job.id, None)
                    if job.durable:
                        save_durable_jobs()
            except Exception as error:
                # 一条坏规则不让其他规则停止检查；此处仅报告错误，没有自动回滚/重试。
                print(f"  \033[31m[cron error] {job.id}: {error}\033[0m")


def cron_scheduler_loop():
    """调度线程的工作：等一会儿，检查一次，再等一会儿，无需不断调用模型。"""
    while True:
        # sleep 只让当前调度线程暂停，主线程和其他线程仍可执行。
        # 每秒检查叫“轮询”。不是硬实时保证；系统忙、休眠时可能推迟或错过触发。
        time.sleep(1)
        check_scheduled_jobs()


def consume_cron_queue() -> list[CronJob]:
    """由 Agent 取走目前全部已到点请求；复制和清空必须作为同一组加锁操作。"""
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    # 新请求若在释放锁后才入队，会留到下次处理，不会被这一次 clear 误删。
    # 这里取出就删除，没有“执行成功后再确认删除”，进程崩溃仍可能丢请求。
    return fired


def has_cron_queue() -> bool:
    """只问列表里有没有东西，不取走；回答只代表检查这一刻的状态。"""
    with cron_lock:
        return bool(cron_queue)


# ── Cron Tools ──

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    """工具包装：把内部的对象或错误，换成可以返回给模型的文字。"""
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' → {prompt}"


def run_list_crons() -> str:
    # 先拿到规则列表，再在锁外拼文字；这里只列规则，不列已经入队的执行请求。
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# ── Tool Definitions ──

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
    {"type": "function", "function": {
        "name": "schedule_cron",
        "description": "Schedule a prompt for the agent. This registers a rule, not a shell command.",
        "parameters": {"type": "object",
                       "properties": {
                           "cron": {"type": "string", "description": "Five fields: minute hour day month weekday."},
                           "prompt": {"type": "string", "description": "Message to send to the agent when due."},
                           "recurring": {"type": "boolean", "description": "False removes the rule after its first trigger."},
                           "durable": {"type": "boolean", "description": "True saves the rule, not queued runs, to disk."}},
                       "required": ["cron", "prompt"]},
    }},
    {"type": "function", "function": {
        "name": "list_crons", "description": "List registered scheduling rules, not queued or running jobs.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "cancel_cron", "description": "Remove a rule. Does not stop queued requests or running commands.",
        "parameters": {"type": "object",
                       "properties": {"job_id": {"type": "string"}},
                       "required": ["job_id"]},
    }},
]

# 上面的 schema 是给模型看的“参数说明”；下面才是把工具名映射到 Python 函数的执行表。
# function.name/parameters 的格式沿用 s01-s13，实际调用协议是 Chat Completions。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
}


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """重新读取 system 提示词需要的状态，和 s13 一样每次模型请求前更新。"""
    # 这里只读取记忆索引，不自动展开索引提到的所有文件。
    # scheduled_jobs 等本地变量也不会被模型自动看到，必须用工具读取或注入消息。
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


# ── Agent Loop：沿用 s13，额外收取到点的请求 ──


def append_assistant(messages: list, message: dict):
    """保存模型回复并打印其中的文字，包括请求工具时附带的说明。"""
    messages.append(message)
    if message.get("content"):
        print(message["content"])


def inject_scheduled_requests(messages: list) -> bool:
    """把已到点的请求变成消息，不在这里调用模型。"""
    fired = consume_cron_queue()
    for job in fired:
        # user 角色是本 demo 承载自动请求的方式，不是用户刚刚又输入了一句话。
        # [Scheduled] 是普通文字标记；真正的工作仍需模型决定工具，再由 Python 执行。
        messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
        print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")
    return bool(fired)

def agent_loop(messages: list, context: dict) -> dict:
    """一轮 Agent 内部可以多次问模型、运行工具。外层调用者必须持有 agent_lock。"""
    # messages 通常就是 session_history，同一列表传入函数后换了一个参数名。
    # 调度线程不碰这个列表；它只入队。当前这个函数才负责取队列、修改历史。
    # 无论从主线程还是队列处理线程进来，都必须先拿 agent_lock，避免两轮交错。
    while True:
        # 第 1 步：把新到点的要求、已经完成的后台结果加入历史。
        # 只有走到这里才收取，不能打断正在进行的网络请求或同步工具。
        # 上一批 tool_calls 必须已经全部回复，不能把这些通知插进工具回复组中间。
        inject_scheduled_requests(messages)
        inject_background_notifications(messages)

        # 第 2 步：system 放到临时请求列表前面，不在历史里每轮重复添加。
        context = update_context(context, messages)
        system = get_system_prompt(context)
        request_messages = [{"role": "system", "content": system}]
        request_messages.extend(messages)
        try:
            # 到这里才真正发模型请求。此时持有 agent_lock，但不持有 cron_lock，
            # 所以调度线程仍可把新请求排队；这些新请求要等下一次发给模型才可见。
            response = client.chat.completions.create(
                model=MODEL,
                messages=request_messages,
                tools=TOOLS,
                max_tokens=8000,
            )
        except Exception as error:
            # 与 s13 一样只报告最终错误并结束本轮，没有接入 s11 的完整恢复策略。
            append_assistant(messages, {
                "role": "assistant",
                "content": f"[Error] {type(error).__name__}: {error}",
            })
            return context

        # 第 3 步：choices[0] 是第一条候选回复，message.tool_calls 是模型要求的工具列表。
        # 模型返回的是调用要求，不会在服务端直接执行本机 Python 函数。
        choice = response.choices[0]
        assistant_message = choice.message
        if choice.finish_reason == "length":
            # 参数可能被截断了一半，不能尝试执行，也不保存这些未执行的 tool_calls。
            append_assistant(messages, {
                "role": "assistant",
                "content": (assistant_message.content or "") +
                    "\n[Error] Output truncated; tool calls from this response were not executed.",
            })
            return context

        # model_dump 把 SDK 对象变成字典，工具调用要求也要一起保留。
        append_assistant(messages, assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            # 等模型回复时后台可能完成了，结束前再收一次结果。
            # 若有新结果，就多问一次；没有则结束，不为了“等后台”反复请求模型。
            if inject_background_notifications(messages):
                continue
            # 这期间新到点的 cron 请求可留给 queue_processor 在释放锁后自动交付。
            return context

        # 第 4 步：把每个工具要求交给本地函数执行，错误也作为工具结果反馈。
        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            print(f"\033[36m> {tool_name}\033[0m")
            try:
                # arguments 是 JSON 字符串，如 '{"cron":"* * * * *","prompt":"run date"}'。
                # loads 把它变成字典，同时将 JSON 的 true/false 变成 Python True/False。
                tool_input = json.loads(tool_call.function.arguments or "{}")
                if not isinstance(tool_input, dict):
                    raise ValueError("tool arguments must be a JSON object")
                if should_run_background(tool_name, tool_input):
                    bg_id = start_background_task(tool_call.id, tool_name, tool_input)
                    output = (f"[Background task {bg_id} started] "
                              f"Command: {tool_input['command']}. "
                              "Result will be available when complete.")
                else:
                    output = execute_tool(tool_name, tool_input)
            except Exception as error:
                output = f"Error: {type(error).__name__}: {error}"
            print(str(output)[:300])

            # 第 5 步：每个 call 都需要一条匹配的 tool 回复，用原始 tool_call.id 配对。
            # cron_... 是规则编号，bg_... 是后台编号，都不能替代这个模型调用编号。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })
        # 这一批全部回复完，才回到 while 顶部收取通知，再次请求模型。


# 用户请求和定时请求共用一份会话，不是每条规则有独立历史。
# 这两项只存在内存里，和持久化的规则文件不是一回事。
session_history: list = []
session_context: dict = {}


def run_agent_turn_locked(user_query: str | None = None):
    """运行一轮完整 Agent；调用者必须已经拿到 agent_lock。"""
    # _locked 只是命名提醒，不会自动获取锁。函数也不再重复拿锁，避免自己等自己。
    global session_context
    if user_query is not None:
        # 用户入口传入文字；定时入口不传，因为请求已经在 cron_queue 里了。
        session_history.append({"role": "user", "content": user_query})
    session_context = agent_loop(session_history, session_context)
    session_context = update_context(session_context, session_history)
    # 模型文字已由 append_assistant 逐次打印，不在这里重复打印最后一条。
    print()


def process_queued_jobs() -> bool:
    """只尝试交付一次。交付了返回 True，没请求或 Agent 忙则返回 False。"""
    if not has_cron_queue():
        return False
    # blocking=False：如果锁被占用，立即返回 False，不停在这里等。
    # 这里的“Agent 空闲”只代表没人持有 agent_lock，不代表所有后台命令都结束了。
    if not agent_lock.acquire(blocking=False):
        return False
    try:
        # 在第一次看队列和拿到锁之间，另一轮 Agent 可能已经取走请求，再确认一次。
        if not has_cron_queue():
            return False
        print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
        # 本函数在哪个线程执行，agent_loop 就在哪个线程执行；函数自己不会创建线程。
        run_agent_turn_locked()
        return True
    finally:
        # 即使模型/工具处理抛出错误，也要放回执行权，否则后面所有请求都会被挡住。
        agent_lock.release()


def queue_processor_loop():
    """队列处理线程：反复尝试把到点请求交给 Agent，不检查 cron 时间表达式。"""
    while True:
        time.sleep(0.2)
        try:
            process_queued_jobs()
        except Exception as error:
            # 避免一次本地异常让自动交付线程永久退出；这不等于失败请求已经被重试。
            print(f"[queue processor error] {type(error).__name__}: {error}")


if __name__ == "__main__":
    # 直接运行文件才加载规则和启动长期循环；import 时不启动调度线程或请求模型。
    # 但顶部仍会读 .env、初始化客户端、创建 .tasks，和前几章的初始化方式一致。
    print("s14: cron scheduler")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    # 先恢复规则，再启动线程。读取的是规则，不是上次未执行完的队列。
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    print("  \033[35m[cron] scheduler thread started\033[0m")
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    print("  \033[35m[queue processor] started\033[0m")
    # daemon=True 表示主线程结束后，不因这些无限循环而阻止进程退出。
    # 不是“程序退出后仍然执行”，也不保证关掉程序前自动完成/保存所有工作。
    while True:
        try:
            # input 不持有 agent_lock。因此主线程等你按回车时，队列处理线程仍能工作。
            # 两边共用终端，自动输出可能打断正在输入的文字，这是简化 UI 的限制。
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        with agent_lock:
            # 用户已经提交，才获取执行权。若定时轮次正在运行，用户请求在这里等它结束。
            run_agent_turn_locked(query)

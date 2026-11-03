#!/usr/bin/env python3
"""
s08_context_compact.py - Context Compact

Four-layer compaction pipeline inserted before LLM calls:

    L1: snip_compact      — trim middle messages when count > 50
    L2: micro_compact     — replace old tool_results with placeholders
    L3: tool_result_budget — persist large results to disk
    L4: compact_history   — LLM full summary (1 API call)

    Emergency: reactive_compact — when API still returns prompt_too_long

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM          │
    │                                      └─ Yes → L4 summary   │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [prompt_too_long?]        │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

Core principle: cheap first, expensive last.
Execution order matches CC source: budget → snip → micro → auto.

Builds on s07 (skill loading). Usage:

    python s08_context_compact/code.py
    Needs: pip install openai python-dotenv + .env 中配置 LLM_API_KEY、
           LLM_BASE_URL 和 LLM_MODEL_ID
"""

import ast
import json
import os
import subprocess
import time
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# 读取项目根目录的 .env；override=True 让本次演示以文件中的配置为准。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

# Agent 所有文件工具都以启动时的当前目录为工作区，safe_path() 会阻止越界。
WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
# L4 会先把原始对话写入这里，便于人在摘要丢失细节时回溯。
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# L3 将过大的工具输出落盘，避免一次 bash/read 结果占满上下文窗口。
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
# OpenAI SDK 也能指向 DeepSeek 等兼容服务：未配置 LLM_BASE_URL 时使用默认 OpenAI 地址。
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]
CURRENT_TODOS: list[dict] = []

# s07: Skill catalog scan (inherited from s07)
# 启动时只读取每个技能的名称和简介；完整 SKILL.md 仍由 load_skill 按需返回。
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """提取 SKILL.md 开头 --- ... --- 中的简单 YAML 字段。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()

SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """建立 name -> {description, content} 注册表，后续不让模型直接拼文件路径。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, _body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    """这是构造 SYSTEM 的内部辅助函数，不是暴露给模型的工具。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    """按注册表名称返回完整技能正文，因此不存在 ../ 形式的路径遍历。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

# s08: SYSTEM includes skill catalog (inherited from s07 build_system)
def build_system() -> str:
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()

# 子 Agent 使用独立 messages[]，只返回最终摘要；它不继承父 Agent 的长历史，
# 也不拥有 task/load_skill/compact，因此不能递归创建 Agent 或自行压缩父对话。
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s07 (unchanged): Basic Tools
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # resolve() 会规范化 ../ 和符号链接；随后检查真实路径仍在 WORKDIR 内。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        # 模型只能请求 bash 工具，真正执行终端命令的是 subprocess.run。
        # capture_output 避免命令输出直接混入终端；timeout 限制单次命令最长运行时间。
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def _normalize_todos(todos):
    """把模型可能传来的 JSON 字符串或 list 统一为经过校验的 todo 列表。"""
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

def run_todo_write(todos: list) -> str:
    """todo 只保存在本进程内存并打印状态，不会写入项目文件。"""
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

def extract_text(content) -> str:
    """OpenAI Chat Completions 的最终文本在 assistant message.content 中。"""
    return content if isinstance(content, str) else ""


# ═══════════════════════════════════════════════════════════
#  FROM s06-s07 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

SUB_TOOLS = [
    # OpenAI function calling schema。模型返回 tool_calls 后，Python 用 SUB_HANDLERS 执行。
    # 子 Agent 故意只有五个文件/终端工具，没有 task、todo_write、load_skill、compact。
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
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}

def spawn_subagent(description: str) -> str:
    """用干净的 messages[] 执行子任务，只把最后的文本摘要返回给父 Agent。"""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    # 不复制父 messages：隔离上下文可减少 token，也防止子任务被父任务细节干扰。
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        # 子 Agent 有自己的“模型 -> tool_calls -> role=tool 结果 -> 模型”循环。
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUB_SYSTEM}, *messages],
            tools=SUB_TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message
        # 必须原样保存 assistant 的 tool_calls；随后 role="tool" 消息通过 tool_call_id 与它配对。
        messages.append(assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            # 未请求工具即为最终回答。
            break
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
                # arguments 是模型生成的 JSON 字符串，先解析后再以 **args 调用本地函数。
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as error:
                output = f"Error: invalid tool arguments: {error}"
            else:
                blocked = trigger_hooks("PreToolUse", name, args)
                if blocked:
                    output = str(blocked)
                else:
                    handler = SUB_HANDLERS.get(name)
                    output = handler(**args) if handler else f"Unknown: {name}"
                    trigger_hooks("PostToolUse", name, args, output)
                    print(f"  \033[90m[sub] {name}: {str(output)[:100]}\033[0m")
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": output})
    # 达到 30 轮上限时末尾可能是工具结果，因此反向寻找最近一次 assistant 文本。
    result = extract_text(messages[-1].get("content"))
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg.get("content"))
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  NEW in s08: Four-Layer Compaction Pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000
# L2 至少保留最近三个工具结果，避免刚读到的文件内容立即被替换。
KEEP_RECENT = 3
# 单条结果超过 30k 字符时才值得持久化到磁盘。
PERSIST_THRESHOLD = 30000

def estimate_size(msgs):
    """教学版的近似估算：字符数不是 token 数，但可触发压缩流程。"""
    return len(str(msgs))

def _message_has_tool_use(msg):
    """OpenAI 中 assistant.tool_calls 表示该消息后必须跟对应 role='tool' 结果。"""
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))


def _is_tool_result_message(msg):
    """与 Anthropic 的 tool_result block 不同，OpenAI 的工具结果是一条独立消息。"""
    return msg.get("role") == "tool"


# L1: snipCompact — trim middle messages
def snip_compact(messages, max_messages=50):
    """超过条数上限时删除中段，但绝不把 tool_calls 与 role='tool' 结果拆开。"""
    if len(messages) <= max_messages: return messages
    # 保留最早的少量上下文与最新任务；中间区域用一条说明消息替代。
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail
    # 若头部最后一条是 assistant tool_calls，把其全部工具结果一并保留。因为如果这个位置的工具结果被裁掉，OpenAI 会收到一个没有对应 tool_call_id 的 role="tool" 消息而报错。
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    # 若尾部从某条工具结果开始，向前退到产生这组结果的 assistant 消息。
    # 否则 OpenAI 会收到一个没有对应 tool_call_id 的 role="tool" 消息而报错。
    if tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        while tail_start > 0 and _is_tool_result_message(messages[tail_start - 1]):
            tail_start -= 1
        if tail_start > 0 and _message_has_tool_use(messages[tail_start - 1]):
            tail_start -= 1
    if head_end >= tail_start:
        return messages # 如果这一步实在没什么可删的，就不删了，避免破坏工具调用的完整性。
    snipped = tail_start - head_end
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[tail_start:]


# L2: microCompact — old result placeholders
def collect_tool_results(messages):
    """收集 OpenAI 的 role='tool' 消息，供 L2 按时间顺序裁剪。"""
    tool_results = []
    for mi, msg in enumerate(messages):
        if msg.get("role") == "tool":
            tool_results.append((mi, msg))
    return tool_results

def micro_compact(messages):
    """L2：保留消息结构和 tool_call_id，只缩短较旧工具结果的 content。"""
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages
    for _, message in tool_results[:-KEEP_RECENT]:
        if len(str(message.get("content", ""))) > 120:
            message["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# L3: toolResultBudget — persist large results to disk
def persist_large_output(tool_use_id, output):
    """将大输出写入工作区，并返回带路径和预览的短文本给模型。"""
    if len(output) <= PERSIST_THRESHOLD: return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists(): path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages, max_bytes=200_000):
    """L3：仅处理当前轮末尾连续的 role='tool' 消息，控制其总字符数。"""
    # 一次 assistant 回复可请求多个工具；它们的结果在历史尾部连续出现。
    start = len(messages)
    while start > 0 and messages[start - 1].get("role") == "tool":
        start -= 1
    results = [(i, messages[i]) for i in range(start, len(messages))]
    total = sum(len(str(message.get("content", ""))) for _, message in results)
    if total <= max_bytes: return messages
    # 优先落盘最大的结果，尽量少动较小且可能重要的结果。
    ranked = sorted(results, key=lambda pair: len(str(pair[1].get("content", ""))), reverse=True)
    for _, message in ranked:
        if total <= max_bytes: break
        content = str(message.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD: continue
        tool_call_id = message.get("tool_call_id", "unknown")
        message["content"] = persist_large_output(tool_call_id, content)
        total = sum(len(str(item.get("content", ""))) for _, item in results)
    return messages


# L4: autoCompact — LLM full summary
def write_transcript(messages):
    """在破坏性摘要前保存完整 JSONL 历史，供人工或后续工具查看。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    """调用模型把历史压缩为仍可继续工作的交接摘要。"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return response.choices[0].message.content or "(empty summary)"

def compact_history(messages):
    """L4：完整摘要后只保留一条用户消息，最大幅度释放上下文。"""
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    """服务端仍判定超长时的兜底策略：摘要旧历史并保留最近五条。"""
    transcript = write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    # 最近五条若从工具结果中间切入，同样退回到对应的 assistant tool_calls。
    if tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        while tail_start > 0 and _is_tool_result_message(messages[tail_start - 1]):
            tail_start -= 1
        if tail_start > 0 and _message_has_tool_use(messages[tail_start - 1]):
            tail_start -= 1
    summary = summarize_history(messages[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  FROM s07: Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # 每项均是 OpenAI 的 {type: "function", function: {...}} schema。
    # 这只是工具说明，不会执行任何代码；真正的执行由下方 TOOL_HANDLERS 完成。
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
    {"type": "function", "function": {
        "name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
        "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
    }},
    {"type": "function", "function": {
        "name": "load_skill", "description": "Load the full content of a skill by name.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "compact", "description": "Summarize earlier conversation to free context space.",
        "parameters": {"type": "object", "properties": {"focus": {"type": "string"}}},
    }},
]

TOOL_HANDLERS = {
    # tool name -> Python 函数。compact 很特殊：它会直接替换整个 messages，
    # 因此在 agent_loop 中单独处理，而不是放进这个普通分发表。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}

# FROM s04 (unchanged): Hooks
HOOKS = {"PreToolUse": [], "PostToolUse": []}
def trigger_hooks(event, *args):
    """按注册顺序执行 hook；任一 hook 返回非 None 就阻止当前工具调用。"""
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None: return r
    return None

DENY_LIST = ["rm -rf /", "sudo", "shutdown"]
def permission_hook(name: str, args: dict):
    """PreToolUse：检查 bash 命令中的危险片段，命中时返回拒绝结果。"""
    if name == "bash":
        for p in DENY_LIST:
            if p in args.get("command", ""): return "Permission denied"
    return None
def log_hook(name: str, args: dict):
    """PreToolUse：只记录工具名，便于观察模型的决策过程。"""
    print(f"\033[90m[HOOK] {name}\033[0m")
    return None

HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s08 core: run compaction pipeline before LLM
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1  # retry limit for reactive compact

def agent_loop(messages: list):
    """主循环：先压缩历史，再请求模型，并把每个工具结果回传到 messages。"""
    reactive_retries = 0
    while True:
        # s08 change: three preprocessors (0 API calls, cheap first)
        # Order matches CC source: budget → snip → micro
        messages[:] = tool_result_budget(messages)    # L3: persist large results first
        messages[:] = snip_compact(messages)          # L1: trim middle
        messages[:] = micro_compact(messages)         # L2: old result placeholders

        # s08 change: tokens still over threshold → LLM summary (1 API call)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        try:
            # system 不存入 history：每轮单独拼入，压缩用户历史时不会把它误删或摘要掉。
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM}, *messages],
                tools=TOOLS,
                max_tokens=8000,
            )
            reactive_retries = 0  # reset on successful API call
        except Exception as e:
            # 近似匹配兼容服务常见的上下文超限错误；最多触发一次兜底摘要再重试。
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise

        assistant_message = response.choices[0].message
        # model_dump 会保留 id、function.arguments、tool_calls 等 API 后续校验所需字段。
        messages.append(assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            # content 是普通文本，主程序会从 history[-1] 打印给用户。
            return

        compact_requested = False
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")

            # compact replaces the entire history, so its tool call cannot be
            # retained with a role="tool" response as normal calls can.
            if name == "compact":
                # compact 后旧 assistant tool_calls 已不在历史中，故不能再追加对应 role="tool"。
                # compact_history 的摘要本身已包含这次请求之前的工作状态。
                messages[:] = compact_history(messages)
                compact_requested = True
                break

            try:
                # 无效 JSON 也要作为工具结果返回给模型，让它下一轮能够修正参数。
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as error:
                output = f"Error: invalid tool arguments: {error}"
            else:
                blocked = trigger_hooks("PreToolUse", name, args)
                if blocked:
                    output = str(blocked)
                else:
                    handler = TOOL_HANDLERS.get(name)
                    output = handler(**args) if handler else f"Unknown: {name}"
                    trigger_hooks("PostToolUse", name, args, output)
            print(str(output)[:200])
            messages.append({
                # OpenAI 要求 tool_call_id 指回本次 assistant tool_calls 中的 id。
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })

        if compact_requested:
            continue


if __name__ == "__main__":
    print("s08: Context Compact — four-layer compaction pipeline")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1].get("content")
        if response_content:
            print(response_content)
        print()

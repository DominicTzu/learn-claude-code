#!/usr/bin/env python3
"""
s10: System Prompt — Runtime prompt assembly with caching.

Run:  python s10_system_prompt/code.py
Need: pip install openai python-dotenv
      .env 中配置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL_ID

Changes from s09:
  - PROMPT_SECTIONS: topic-keyed dict of prompt fragments
  - assemble_system_prompt(context): select + join sections by real state
  - get_system_prompt(context): deterministic cache via json.dumps
  - agent_loop uses get_system_prompt(context) instead of hardcoded SYSTEM

Memory section loads when .memory/MEMORY.md exists (real state, not keywords).

本章聚焦 system prompt 的运行时组装，并非把 s09 的所有功能复制过来：
只保留 bash、read_file、write_file，以及读取记忆索引的逻辑。
主线：读取真实状态 -> 组装/复用 prompt -> 请求模型 -> 执行工具 -> 刷新状态。
"""

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

# 与 s01-s09 一致，使用 OpenAI SDK 连接 OpenAI 兼容服务。
# SDK 是客户端，不决定实际模型；DeepSeek 等服务由 base_url 和 MODEL 指定。
# override=True 表示 .env 中的同名配置会覆盖已有环境变量，不要把密钥写进代码。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

# 工作目录是启动命令时所在的目录，而不是本 code.py 所在目录。
# 从项目根目录运行，才会读取项目根目录下的 .memory/MEMORY.md。
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]


# ── Prompt Sections ──

# 每个 key 对应一个独立主题；模板只负责表达规则，context 提供当前实际值。
# 这不是增加一套更强的提示词，而是把原有提示词拆成可单独维护的段落。
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: {enabled_tools}.",
    "workspace": "Working directory: {workspace}",
    "memory": "Relevant memories:\n{memories}",
}


def assemble_system_prompt(context: dict) -> str:
    """根据 update_context 返回的状态，按固定顺序拼接 system prompt。"""
    sections = []

    # 始终需要身份、工具列表、工作目录；顺序稳定，便于检查实际发送的内容。
    # 工具名称只是说明文字，真正的调用协议仍由请求中的 tools=TOOLS 定义。
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(PROMPT_SECTIONS["tools"].format(
        enabled_tools=", ".join(context["enabled_tools"])))
    sections.append(PROMPT_SECTIONS["workspace"].format(
        workspace=context["workspace"]))

    # 只有索引存在且非空才添加这一段，不依据用户有没有说“记忆”来判断。
    # 这里只注入索引文本，不会自动沿 Markdown 链接读取各条记忆的正文。
    memories = context.get("memories", "")
    if memories:
        sections.append(PROMPT_SECTIONS["memory"].format(memories=memories))

    return "\n\n".join(sections)


# 单条、进程内缓存：只记住最近一次状态和对应字符串，退出程序就消失。
_last_context_key = None
_last_prompt = None


def get_system_prompt(context: dict) -> str:
    """状态相同则复用字符串，状态变化则重新组装。

    这只是本地字符串缓存，不缓存模型答案，也不会跳过模型请求。
    即使打印 cache hit，完整 system prompt 仍然随每次 API 请求发送；
    服务端是否命中 prompt cache、如何计费，由所用服务决定。
    本示例假设 PROMPT_SECTIONS 在进程运行期间不变。
    """
    global _last_context_key, _last_prompt
    # sort_keys=True 消除字典键插入顺序的影响；列表顺序仍然有意义。
    # ensure_ascii=False 保留中文，default=str 为非 JSON 对象提供字符串兜底。
    # 本章 context 只有列表/字符串；default=str 并不是任意对象的可靠缓存方案。
    # 这里直接比较序列化字符串，不使用不能直接处理 dict/list 的 hash()。
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    """将读写路径解析为绝对路径，并限制在工作目录内。"""
    # resolve() 解析 .. 和符号链接；is_relative_to() 检查是否属于 WORKDIR。
    # 这只约束使用本函数的文件工具，不会给 bash 创建操作系统级沙箱。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """由 Python 启动 shell 子进程执行命令，模型本身不直接控制终端。"""
    try:
        # shell=True 让 shell 解析管道、重定向等；cwd 指定初始工作目录。
        # capture_output/text 捕获文本输出，timeout 限制等待时间。
        # 注意：cwd 不是权限隔离，shell 仍能访问当前用户有权限访问的其他路径。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件，可选 limit 限制返回行数；读不到时把错误交给模型。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """创建父目录并写入文件；已有文件会被覆盖，本示例没有自动备份。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    # OpenAI Chat Completions 的 function schema：描述名称、用途和参数，
    # 不包含 Python 函数体。required 列出必填参数，read_file.limit 可省略。
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
]

# 本地分发表：模型返回名称后，由程序查找并执行函数，名称须与 TOOLS 一致。
TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """从真实运行状态生成 prompt 所需的配置，而不是复制整个对话历史。

    context 是工具/目录/记忆的状态快照，messages 才是用户、助手、工具的历史。
    本章每次从当前状态重建快照，暂不使用传入的旧 context 和 messages；
    保留这两个参数，方便后续增加依赖历史或已有状态的 section。
    """
    memories = ""
    if MEMORY_INDEX.exists():
        # 只读 MEMORY.md，不扫描所有记忆正文，也不执行 s09 的检索、提取和合并。
        # 索引里的链接需要后续调用 read_file 才能展开为模型可见的文本。
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """反复执行“请求模型 -> 执行工具 -> 回传结果”，直到模型不再调用工具。"""
    while True:
        # 每次模型调用前刷新：工具写入记忆或用户在两轮间手动编辑索引，
        # 下一次请求就会读取新内容；状态没变则复用已组装的 prompt。
        context = update_context(context, messages)
        system = get_system_prompt(context)
        response = client.chat.completions.create(
            model=MODEL,
            # system 临时加在请求最前面，不写入 history，避免累积过期的 system。
            # 模型直接看到的是 system + 对话历史 + tools schema，而不是磁盘本身。
            messages=[{"role": "system", "content": system}, *messages],
            tools=TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message
        # 原样保留 assistant 的 tool_calls；后面的工具结果必须能回指这些调用。
        messages.append(assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            # 不再请求工具就结束当前用户回合；最外层负责打印最终文本回复。
            return

        # 一个 assistant 响应可能包含多个工具调用；本示例按返回顺序逐个执行。
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")
            try:
                # arguments 是 JSON 字符串，需解析成字典才能用 **args 展开为参数。
                args = json.loads(tool_call.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
                handler = TOOL_HANDLERS.get(name)
                output = handler(**args) if handler else f"Unknown: {name}"
            except (ValueError, TypeError) as error:
                # 包括无效 JSON、非对象参数、缺少必填参数；错误也回传，允许模型修正。
                # 网络限流、上下文超限等请求级恢复仍留给 s11，不在这里实现。
                output = f"Error: invalid tool arguments: {error}"
            print(str(output)[:200])
            # 屏幕只预览前 200 字符，但历史中保存完整返回值。
            # tool_call_id 匹配具体调用，不是函数名；同一函数可在一轮中被调用多次。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })


if __name__ == "__main__":
    print("s10: system prompt — runtime assembly")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # history 跨用户回合保留；本章没有上下文压缩，长会话仍可能超过模型限制。
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        # Chat Completions 的文本位于 message.content，不再是 Anthropic 的内容块。
        print(history[-1].get("content") or "")
        print()

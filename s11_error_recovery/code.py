#!/usr/bin/env python3
"""
s11: Error Recovery — three recovery paths + exponential backoff.

Run:  python s11_error_recovery/code.py
Need: pip install openai python-dotenv
      .env 中配置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL_ID

Changes from s10:
  - LLM call wrapped in try/except with three recovery paths
  - Path 1: finish_reason=length -> optional token escalation -> continuation
  - Path 2: context too long -> reactive compact -> retry (once)
  - Path 3: connection/timeout/429/5xx -> backoff with jitter (max 10 retries),
            fallback model on consecutive 503/529
  - with_retry wrapper for transient errors
  - RecoveryState tracks escalation / compact / overload / model

可选配置：
  LLM_FALLBACK_MODEL_ID：同一个接口下可用的备用模型；兼容旧 FALLBACK_MODEL_ID。
  LLM_MAX_TOKENS：初始输出预算，默认 8000。
  LLM_ESCALATED_MAX_TOKENS：截断后的升档预算，默认等于初始预算，即不升档。
  只有确认服务支持后才把升档预算设大，例如原教程的 64000；不自动猜模型上限。

本章仍是三个工具的独立教学 demo，没有完整复制 s08/s09 的压缩、记忆系统。
主线：组装 prompt -> 请求模型（可重试）-> 检查截断 -> 执行工具 -> 回传结果。
"""

import json
import math
import os
import random
import subprocess
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI

# 与前几章一致：OpenAI 是客户端，实际服务和模型由 .env 指定。
# override=True 让 .env 的同名配置覆盖进程环境变量；不把真实密钥写入代码。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

# 从项目根目录启动，才能读取该项目的 .memory/MEMORY.md。
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
    # 本章自己演示重试，关闭 SDK 内置重试，避免两层重试叠加而难以控制次数。
    max_retries=0,
)
PRIMARY_MODEL = os.environ["LLM_MODEL_ID"]
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL_ID") or os.getenv("FALLBACK_MODEL_ID")

# ── Constants ──

DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8000"))
ESCALATED_MAX_TOKENS = int(os.getenv("LLM_ESCALATED_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
if DEFAULT_MAX_TOKENS <= 0 or ESCALATED_MAX_TOKENS < DEFAULT_MAX_TOKENS:
    raise ValueError("输出预算必须为正数，且 LLM_ESCALATED_MAX_TOKENS 不能小于 LLM_MAX_TOKENS")
# max_tokens 是输出预算，不是上下文窗口大小；主模型和备用模型都需要支持该值。
# 两个预算相等时，截断后直接续写；配置更大预算时，先重做一次完整生成。
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_OVERLOAD = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly. "
    "no apology, no recap. Pick up mid-thought."
)

# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: {enabled_tools}.",
    "workspace": "Working directory: {workspace}",
    "memory": "Relevant memories:\n{memories}",
}


def assemble_system_prompt(context: dict) -> str:
    """沿用 s10：固定段落 + 来自运行状态的工具、目录和可选记忆索引。"""
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
    """只缓存最近一次组装的字符串，不缓存模型答案，也不会省略 API 请求。"""
    global _last_context_key, _last_prompt
    # sort_keys 消除字典键顺序差异；值变化时失效。完整 system 仍随每次请求发送。
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


# ── Tools (unchanged) ──

def safe_path(p: str) -> Path:
    """resolve 解析 .. / 符号链接，再检查文件读写是否位于工作目录内。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """Python 启动 shell 子进程，收集标准输出/错误；模型只提出调用请求。"""
    try:
        # cwd 只设置起始目录，不是沙箱。bash 不受 safe_path 的文件路径检查约束。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件；limit 限制返回行数，文件错误作为工具结果交回模型。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """创建父目录并覆盖写入；本 demo 没有撤销/备份，所以不能盲目重放工具。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    # schema 是给模型看的工具说明；真正的 Python 函数由 TOOL_HANDLERS 查找。
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

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ── Error Recovery (s11 new) ──

class RecoveryState:
    """一个用户回合内共享的恢复状态；新用户回合重新创建，不会永久切换模型。"""
    def __init__(self):
        # 升档最多一次、续写最多三次、裁剪最多一次，避免恢复本身陷入无限循环。
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_overload = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """指数退避 + 随机抖动；服务端指定 Retry-After 时优先使用它。"""
    if retry_after is not None:
        return retry_after
    # attempt 从 0 开始：基础等待 0.5、1、2、4...秒，上限 32 秒。
    # 再加最多 25% 的随机延迟，避免多个客户端同时重试；含抖动最多 40 秒。
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def get_retry_after(error: Exception) -> float | None:
    """读取 Retry-After 的秒数或 HTTP 日期；无效值交给指数退避处理。"""
    response = getattr(error, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def with_retry(fn, state: RecoveryState):
    """只重试模型请求的瞬态故障；认证、参数等错误交给外层，不盲目重试工具。"""
    # MAX_RETRIES 是重试次数，不含首次请求，因此最多发出 MAX_RETRIES + 1 次请求。
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = fn()
            state.consecutive_overload = 0
            return result
        except (APIConnectionError, APIStatusError) as error:
            # APITimeoutError 属于 APIConnectionError；HTTP 错误用 status_code 分类，
            # 不在错误字符串里找 "429"，避免把普通错误中的数字误当成状态码。
            status = getattr(error, "status_code", None)
            transient = (isinstance(error, APIConnectionError)
                         or status in (408, 409, 429)
                         or (status is not None and status >= 500))
            # 有些服务把额度耗尽也报成 429；这种情况等待并不能解决。
            if not transient or getattr(error, "code", None) in (
                "insufficient_quota", "billing_hard_limit_reached"
            ):
                raise

            if status in (503, 529):
                state.consecutive_overload += 1
            else:
                state.consecutive_overload = 0
            if attempt == MAX_RETRIES:
                # 最后一次失败后立即停止，不再做无意义的 sleep。
                raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded") from error

            if (state.consecutive_overload >= MAX_CONSECUTIVE_OVERLOAD
                    and FALLBACK_MODEL and state.current_model != FALLBACK_MODEL):
                state.current_model = FALLBACK_MODEL
                state.consecutive_overload = 0
                print(f"  \033[31m[overloaded] switching to {FALLBACK_MODEL}\033[0m")
                # 只换模型名，仍使用相同 base_url/key；备用模型须支持相同工具协议。

            delay = retry_delay(attempt, get_retry_after(error))
            reason = str(status) if status is not None else type(error).__name__
            print(f"  \033[33m[{reason}] retry {attempt+1}/{MAX_RETRIES},"
                  f" wait {delay:.1f}s\033[0m")
            time.sleep(delay)


def is_prompt_too_long_error(e: Exception) -> bool:
    """兼容不同服务的上下文超限错误；并非所有 400 都能靠压缩修复。"""
    if not isinstance(e, APIStatusError) or e.status_code not in (400, 413, 422):
        return False
    msg = str(e).lower()
    return any(marker in msg for marker in (
        "prompt is too long", "prompt_too_long", "prompt_is_too_long",
        "context_length_exceeded", "max_context_window", "maximum context length",
        "context window", "too many tokens",
    ))


def reactive_compact(messages: list) -> list:
    """应急裁剪：尽量保留最后五条，但不能拆开工具调用与结果。

    这是有损裁剪，不是 s08/s09 的模型摘要；没有自动落盘，被删信息无法从列表恢复。
    如果最后几条或 system 本身仍然太大，裁剪也可能失败，所以每回合只尝试一次。
    """
    start = max(0, len(messages) - 5)
    # 一次 assistant 可请求多个工具；切点若落在 tool 消息内，回退到发起调用处。
    # 因而实际可能保留超过五条，协议完整性优先于严格满足条数。
    if start < len(messages) and messages[start].get("role") == "tool":
        while start > 0 and messages[start - 1].get("role") == "tool":
            start -= 1
        if start > 0 and messages[start - 1].get("tool_calls"):
            start -= 1
    tail = messages[start:]
    print(f"  \033[31m[reactive compact] keeping last {len(tail)} messages\033[0m")
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """重建 prompt 状态；旧 context/messages 是预留参数，本章不据此推测能力。"""
    memories = ""
    if MEMORY_INDEX.exists():
        # 与 s10 一致，只把索引放入 system，不自动读取链接中的记忆正文。
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop ──

def append_assistant(messages: list, message: dict):
    """保存助手消息并立即显示文本，避免压缩历史后旧下标失效而漏打印答案。"""
    messages.append(message)
    if message.get("content"):
        print(message["content"])


def agent_loop(messages: list, context: dict):
    """s10 的工具循环 + 有界恢复；重试只包围模型请求，不重放已经执行的工具。"""
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # 每次请求前刷新磁盘状态，工具或用户修改 MEMORY.md 后下一轮即可看见。
        context = update_context(context, messages)
        system = get_system_prompt(context)
        # 内层 with_retry 处理临时故障，外层处理上下文超限和不可恢复错误。
        try:
            response = with_retry(
                # lambda 是“稍后执行的无参函数”，with_retry 每重试一次就调用一次。
                # 每次执行都读取 state.current_model；不能用默认参数 mdl=... 固定旧值，
                # 否则恢复代码虽然切了备用模型，真正请求的仍然是原来的模型。
                lambda: client.chat.completions.create(
                    model=state.current_model,
                    messages=[{"role": "system", "content": system}, *messages],
                    tools=TOOLS,
                    max_tokens=max_tokens,
                ),
                state)
        except Exception as e:
            # 路径 2：上下文超限 -> 原地替换历史 -> 重试一次。
            # messages[:] 保留列表对象，因此最外层的 history 也能看到裁剪结果。
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                append_assistant(messages, {"role": "assistant", "content":
                    "[Error] Context too large, cannot continue."})
                return

            # 密钥无效、参数不支持、重试耗尽等：返回可见错误，而不是伪装成正常完成。
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            append_assistant(messages, {"role": "assistant", "content":
                f"[Error] {name}: {str(e)[:200]}"})
            return

        choice = response.choices[0]
        assistant_message = choice.message
        # 路径 1：HTTP 请求成功也不等于回答完整。OpenAI 用 finish_reason="length"
        # 表示输出触及限制，不再使用 Anthropic 的 stop_reason="max_tokens"。
        if choice.finish_reason == "length":
            if not state.has_escalated:
                state.has_escalated = True
                if ESCALATED_MAX_TOKENS > max_tokens:
                    max_tokens = ESCALATED_MAX_TOKENS
                    print(f"  \033[33m[max_tokens] escalating"
                          f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                    # 不把第一次截断内容写入历史，增大预算后重做同一请求。
                    continue

            if assistant_message.tool_calls:
                # 截断可能发生在 JSON 参数中间。不能执行，也不能把缺少结果的
                # tool_calls 写入历史；丢弃本次调用，让模型重新生成完整调用。
                continuation = (
                    "The tool call output was truncated and no tools were executed. "
                    "Return complete tool calls with valid JSON arguments."
                )
            else:
                # 普通文本可以保留并续写；每个片段立即打印，避免只展示最后一段。
                append_assistant(messages, assistant_message.model_dump(exclude_none=True))
                continuation = CONTINUATION_PROMPT
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": continuation})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            append_assistant(messages, {"role": "assistant", "content":
                "[Error] Output remains truncated; recovery limit reached."})
            return

        # 正常响应必须原样保存 tool_calls，工具结果再通过 tool_call_id 与其配对。
        append_assistant(messages, assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            return

        # 模型只是请求调用，真正的参数解析、函数查找、执行都发生在本地。
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")
            try:
                args = json.loads(tool_call.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
                handler = TOOL_HANDLERS.get(name)
                output = handler(**args) if handler else f"Unknown: {name}"
            except (ValueError, TypeError) as error:
                output = f"Error: invalid tool arguments: {error}"
            print(str(output)[:200])
            # 工具失败也返回配对结果，下一次请求让模型决定是否换参数或换方案。
            # 不把工具执行放入 with_retry，避免一次网络故障导致文件被重复修改。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })


if __name__ == "__main__":
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        # 回答和续写片段已由 append_assistant 打印，不再扫描可能被压缩的 history。
        print()
